import argparse
import json
import logging
import os
import shutil
from copy import deepcopy

import yaml
from google.cloud import pubsub_v1

from big_query_table_generator import BigQueryTableGenerator
from looker_generator import LookerDashboardGenerator, LookmlViewGenerator
from proto_generator import ProtoGenerator
from yaml_converter import YamlConverter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    force=True)


def read_file(file_name):
    with open(file_name, "r") as f:
        return f.read().splitlines()


def find_yaml_from_schema_name(schema_name, yaml_list):
    for entry in yaml_list:
        if os.path.split(entry)[1].split(".")[0] == schema_name:
            return entry
    return None


def load_yaml_to_dict(file_name: str) -> dict:
    """
    Load a yaml file to a dict.
    :param file_name: Path to the yaml file.
    :return: Dict of the yaml content.
    """
    try:
        with open(file_name, "r") as stream:
            try:
                return yaml.load(stream, Loader=yaml.CLoader)
            except yaml.YAMLError as exception:
                raise exception
    except FileNotFoundError as exception:
        raise exception


def process_sensitive_fields_entity(data, param, param_data, processing_rules):
    processing_mode = param_data.get("ProcessingMode")
    sensitive = param_data.get("Hipaa") or param_data.get("Pii")
    if processing_mode:
        processing_rules[param] = processing_mode
        del data[param]["ProcessingMode"]
        if processing_mode == "hash":
            data[param]["Type"] = "string"
            data[param]["Description"] = data[param]["Description"] + " as SHA256 hash"
            data[param]["Unit"] = "SHA256 hash"
            data[param]["Hipaa"] = False
            data[param]["Pii"] = False
        elif processing_mode == "drop":
            del data[param]
        elif processing_mode == "truncate":
            data[param]["Hipaa"] = False
            data[param]["Pii"] = False

    elif sensitive:
        # del data[param]  reactivate as soon as default changes back to drop
        processing_rules[param] = "pass"


def process_sensitive_fields(data):
    processing_rules = {}
    for param, param_data in list(data.items()):
        processing_rules[param] = None
        if isinstance(param_data, list):
            data[param][0], processing_rules[param] = process_sensitive_fields(param_data[0])
        elif not param_data.get("Type"):
            data[param], processing_rules[param] = process_sensitive_fields(data[param])
        else:
            process_sensitive_fields_entity(data, param, param_data, processing_rules)

        if not processing_rules[param]:
            del processing_rules[param]

    return data, processing_rules if processing_rules else None


def serialize_proto(proto_file):
    proto_creator = ProtoGenerator()
    full_package_name, serialized_proto = proto_creator.create_serialized_proto(proto_file=proto_file)
    proto_creator.check_descriptor()
    return full_package_name, serialized_proto


def convert_to_dict(bigquery_table_schema):
    schema = []
    for schema_field in bigquery_table_schema:
        column = {
            "name": schema_field.name,
            "type": schema_field.field_type,
            "mode": schema_field.mode,
            "description": schema_field.description,
        }
        if schema_field.field_type == "RECORD":
            column["fields"] = convert_to_dict(schema_field.fields)

        schema.append(column)
    return schema


def get_raw_schemas(bq_creator, raw_dataset, raw2_dataset, schema, deploy_tables):
    table, view = bq_creator.create_raw_table_and_view(
        schema=schema,
        dataset=raw_dataset,
        table_id=bq_creator.base_table_name,
        deploy_tables=deploy_tables,
    )
    tables = {table.dataset_id: {table.table_id: convert_to_dict(table.schema)}}
    views = {view.dataset_id: {view.table_id: view.view_query}}

    raw2_tables, raw2_views = bq_creator.create_raw2_views(schema, raw2_dataset,
                                                           deploy_tables=deploy_tables)

    for raw2_view in raw2_views:
        views.setdefault(raw2_view.dataset_id, {}).update(
            {raw2_view.table_id: raw2_view.view_query})

    for raw2_table in raw2_tables:
        tables.setdefault(raw2_table.dataset_id, {}).update(
            {raw2_table.table_id: convert_to_dict(raw2_table.schema)})

    return tables, views


def publish_message(project_id, topic_id, message):
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(project_id, topic_id)
        logging.info(f"Trigger {message} to topic {topic_path}")
        message_data = message.encode("utf-8")
        future = publisher.publish(topic_path, message_data)
        logging.info(f"Published message: {future.result()}")
    except Exception as e:
        logging.error(f"Publish failed to topic: {topic_path}: error{e}")


def overwrite_proto_file_content(git_repo_name, proto_file, version, central_yaml_schema):
    """
    Overwrites the content of a .proto file by converting a central YAML schema to protobuf format.

    Args:
        git_repo_name (str):           Name of the Git repository the schema belongs to.
        proto_file (str):              Path to the .proto file to overwrite.
        version (str):                 Version identifier for the schema.
        central_yaml_schema (dict):    Parsed YAML schema to convert into protobuf format.
    """
    with open(proto_file, "w") as f:
        f.write(
            YamlConverter().yaml_to_proto(
                data=central_yaml_schema,
                version=version,
                repo=git_repo_name,
                file_name=os.path.split(proto_file)[1].split(".")[0].rsplit("_", 1)[0],
            )
        )


def get_schema_table_content(args, proto_file_name,
                             yaml_schema, bq_creator,
                             deploy_tables=False):
    with open(proto_file_name, "r") as f:
        proto_content = f.read()

    package_name, serialized_proto = serialize_proto(proto_file_name)
    bq_schema = bq_creator.create_bq_schema(yaml_schema)

    tables, views = get_raw_schemas(
        bq_creator=bq_creator,
        raw_dataset=args.raw_dataset,
        raw2_dataset=args.raw2_dataset,
        schema=bq_schema,
        deploy_tables=deploy_tables,
    )
    return {
        "file_name": os.path.basename(proto_file_name),
        "package_name": package_name,
        "serialized_proto": serialized_proto,
        "proto_content": proto_content,
        "views": views,
        "tables": tables,
        "bigquery_schema": bq_schema,
    }


def main(args):
    proto_files = read_file(file_name=args.proto_list)
    yaml_files = read_file(file_name=args.yaml_list)

    for proto_file in proto_files:
        schema_name, version = os.path.basename(proto_file).split(".")[0].rsplit("_", 1)
        yaml_file = find_yaml_from_schema_name(schema_name=schema_name, yaml_list=yaml_files)
        if not yaml_file:
            logging.error(f"Could not find yaml file for schema {schema_name}")
            exit(1)

        yaml_schema = load_yaml_to_dict(yaml_file)
        yaml_file_name = os.path.split(yaml_file)[1].split(".")[0]

        bq_creator = BigQueryTableGenerator(
            schema_dataset=args.schema_dataset,
            dry_run=bool(args.dry_run),
            gcp_project=args.gcp_project,
            schema_name=yaml_schema["Name"],
            file_name=yaml_file_name,
            version=version,
            repo=args.git_repo_name,
        )

        regional_schema_table_content = get_schema_table_content(args,
                                                                 proto_file,
                                                                 yaml_schema,
                                                                 bq_creator)
        shutil.copy2(proto_file, f"{proto_file}_backup")

        central_yaml_schema = deepcopy(yaml_schema)
        central_yaml_schema["Content"], processing_rules = process_sensitive_fields(
            central_yaml_schema["Content"])
        overwrite_proto_file_content(args.git_repo_name, proto_file, version, central_yaml_schema)

        central_schema_table_content = get_schema_table_content(
            args, proto_file, central_yaml_schema, bq_creator, deploy_tables=True
        )

        os.replace(f"{proto_file}_backup", proto_file)

        if not bool(args.dry_run):
            bq_creator.deploy_tables()

        bq_creator.publish_schema_to_bq(
            table="ds_core_schemas_1",
            git_tag=args.git_tag,
            yaml_content=json.dumps(yaml_schema),
            processing_rules=json.dumps(processing_rules) if processing_rules else None,
            regional_info=regional_schema_table_content,
            central_info=central_schema_table_content,
            output_path=args.output_path,
        )

        if bool(args.execute_looker):
            logging.info('Changing Looker Files')
            looker_creator = LookmlViewGenerator(
                looker_root=args.looker_root,
                raw1_dataset=args.raw_dataset,
                raw2_dataset=args.raw2_dataset,
                looker_access=yaml_schema.get("LookerAccessGroup"),
                table_id=list(central_schema_table_content["tables"][args.raw_dataset].keys())[0],
            )
            explore_name = looker_creator.create_views_and_model(
                schema=central_schema_table_content["bigquery_schema"])

            dashboard = LookerDashboardGenerator(looker_root=args.looker_root).create(
                schema=central_schema_table_content["bigquery_schema"], explore_name=explore_name
            )
            if bool(args.dashboard_folder_id) and args.looker_ini_path:
                logging.info('Deploying looker dashboards')
                LookerDashboardGenerator.deploy(
                    lookml=dashboard,
                    folder_id=args.dashboard_folder_id,
                    looker_ini_path=args.looker_ini_path,
                )

    if args.trigger_topic:
        logging.info(f"Trigger message to {args.trigger_topic}")
        publish_message(args.gcp_project, args.trigger_topic, "Yippie ya yeah")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Workflow to deploy proto schemas to BQ")

    parser.add_argument("--git_repo_name",
                        help="Repo name where protobuf and yaml files are located")
    parser.add_argument("--gcp_project", help="GCP project name")
    parser.add_argument(
        "--dry_run",
        help="Flag indicating whether GCP resources should be changed or not",
        default="",
    )
    parser.add_argument("--yaml_list", help="List of changed yaml files")
    parser.add_argument("--proto_list", help="List of generated proto files")
    parser.add_argument("--schema_dataset", help="Name of schema dataset")
    parser.add_argument("--raw_dataset", help="Name of raw dataset")
    parser.add_argument("--raw2_dataset", help="Name of raw2 dataset")
    parser.add_argument("--looker_root", help="Root path of looker clone")
    parser.add_argument("--git_tag", help="Git tag", default=None)
    parser.add_argument("--dashboard_folder_id", help="Dashboard folder id in Looker", default=None)
    parser.add_argument("--looker_ini_path", help="Path to looker.ini file", default=None)
    parser.add_argument("--trigger_topic", help="Topic id to trigger deployment", default=None)
    parser.add_argument("--output_path",
                        help="path where bigquery entry is printed to file when dry run",
                        default=None)
    parser.add_argument("--execute_looker", help="Flag wether looker should be skipped or not",
                        default=None)
    args = parser.parse_args()

    main(args=args)
