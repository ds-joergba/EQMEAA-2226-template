import base64
import copy
import datetime
import json
import logging
import re
import time
from typing import Dict, List, Tuple

from google.cloud import bigquery

logging.basicConfig(level=logging.INFO)

NEEDED_KEYS = {"Description", "Type", "Unit", "Hipaa", "Pii"}
TYPE_MAPPING = {
    "string": "STRING",
    "int": "INTEGER",
    "float": "FLOAT64",
    "bool": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "enum": "INTEGER",
}

PERM_MAP = {"Hipaa": "pem_hipaa", "Pii": "pem_pii"}


class BigQueryTableGenerator:

    def __init__(
            self,
            schema_dataset: str,
            schema_name,
            file_name,
            version,
            repo,
            dry_run=True,
            gcp_project=None,
    ):
        """
        The class provides methods to generate raw BigQuery tables based on a YAML definition and
        registers a new schema to the schema table.
        :param schema_dataset: Dataset name to register the schema to
        :param schema_name:  Name of the message defined in the schema
        :param file_name:  File name without extension
        :param version:  Schema version
        :param repo:  Name of the repo
        :param dry_run:  If True no registration will be done
        :param gcp_project: GCP project name
        """
        self.gcp_project = gcp_project
        self.dry_run = dry_run
        if gcp_project and dry_run is False:
            self.bigquery_client = bigquery.Client(project=gcp_project)
        self.schema_dataset = schema_dataset
        self.schema = []
        self.full_package_name = None
        self.base_table_name = self.get_base_table_name(schema_name, file_name, version, repo)
        self.git_repo_name = repo
        self.tables_to_deploy = {}

    def get_base_table_name(self, schema_name: str, file_name: str, version: str, repo: str,
                            dataset: str = "$DATASET"):
        """
        Generate the table id of the main table

        :param dataset:
        :param schema_name: The schema name coming from the yaml file.
        :param file_name:   Schema file name.
        :param version:     The version string used for the table name.
        :param repo:        Name of the bitbucket repository.
        """
        repo_name = re.sub("\\W+", "", repo)
        return (
            f"{self.gcp_project}.{dataset}.{repo_name}_{file_name}_{schema_name}"
            f'_{version.replace(".", "_")}_{schema_name}'
        )

    def create_raw_table_and_view(self, schema, dataset, table_id, deploy_tables):
        """
        Generate views and tables for all non repeating dimension (stored in raw1).
        :param schema:        BQ schema
        :param dataset:       Dataset to create views and tables
        :param table_id:      Name of the output table/view
        :param deploy_tables: Flag wether tables are reserved for deployment or not
        :return:              table and view
        """
        enums = {}
        for enum_schema, enum_name in self.extract_enum_fields(schema=schema):
            enums[enum_name] = enum_schema

        sql_of_view = self.generate_raw_view(enums, table_id)
        table, view = self.create_table_and_view(
            table_schema=schema, sql=sql_of_view, dataset=dataset, table_id=table_id,
            deploy_tables=deploy_tables
        )
        return table, view

    def create_raw2_views(self, schema, dataset, deploy_tables):
        """
        Generate views and tables for all repeating dimensions (stored in raw2)
        :param schema:        BQ schema
        :param dataset:       Dataset to create views and tables
        :param deploy_tables: Flag wether tables are reserved for deployment or not
        :return:              tables and views for raw 2
        """
        tables = []
        views = []
        for repeated_schema, repeated_table_id in self.extract_repeated_fields(schema=schema):
            table, view = self.create_raw_table_and_view(
                schema=repeated_schema, dataset=dataset, table_id=repeated_table_id,
                deploy_tables=deploy_tables
            )
            tables.append(table)
            views.append(view)

        return tables, views

    def create_table_and_view(self, table_schema, sql, dataset, table_id, deploy_tables):
        """
        Add a new table and view based on a yaml file. The table name will be yaml_file_version

        :param table_id:        Table id template
        :param sql:             Sql code
        :param dataset:         Dataset name
        :param table_schema:    The schema coming from the yaml file + header.
        :param deploy_tables:   Flag wether tables are reserved for deployment or not
        :return:                table and view
        """

        table_id = table_id.replace("$DATASET", dataset)
        table = bigquery.Table(table_id, schema=table_schema)

        view_id = table_id + "_view"
        if deploy_tables:
            sql = sql.replace("$DATASET", dataset)
        view = bigquery.Table(view_id)
        view.view_query = sql

        if deploy_tables:
            self.tables_to_deploy[f"{table.project}.{table.dataset_id}.{table.table_id}"] = table
            self.tables_to_deploy[f"{view.project}.{view.dataset_id}.{view.table_id}"] = view
        return table, view

    def deploy_tables(self):
        for table_id, table in self.tables_to_deploy.items():
            table = self.bigquery_client.create_table(table, exists_ok=True)
            print("Created {}.{}.{}".format(table.project, table.dataset_id, table.table_id))
        if not self.tables_to_deploy:
            print("Dry run, will not touch any GCP resource")

    @staticmethod
    def get_header_columns():
        header_info = [
            {
                "name": "DwhLogId",
                "type": "STRING",
                "mode": "REQUIRED",
                "description": "Primary Key - Pipeline wide unique ID attached to every log.",
            },
            {"name": "header_UserId", "type": "STRING", "mode": "NULLABLE",
             "description": "Id of the User."},
            {"name": "header_AccountId", "type": "STRING", "mode": "NULLABLE",
             "description": "Id of the Account."},
            {"name": "header_AssetId", "type": "STRING", "mode": "NULLABLE",
             "description": "Id of the Asset."},
            {"name": "header_WorkflowId", "type": "STRING", "mode": "NULLABLE",
             "description": "Id of the workflow."},
            {
                "name": "header_CloudTimestamp",
                "type": "TIMESTAMP",
                "mode": "REQUIRED",
                "description": "The timestamp when log was received.",
            },
            {
                "name": "header_ProcessingTimestamp",
                "type": "TIMESTAMP",
                "mode": "REQUIRED",
                "description": "The timestamp when the log was started to be processed.",
            },
            {
                "name": "header_CreationTimestamp",
                "type": "TIMESTAMP",
                "mode": "NULLABLE",
                "description": "The timestamp when the log was created by the sender.",
            },
        ]
        return header_info

    @staticmethod
    def create_bq_schema(yaml_schema: Dict) -> List:
        """
        Create a BigQuery schema based on the loaded yaml (provided as python dictionary).

        :param yaml_schema: Yaml schema content as python dictionary.
        :return: Valid BigQuery schema as list.
        """
        schema = []
        for entry_name, entry_data in yaml_schema["Content"].items():
            if isinstance(entry_data, dict):
                schema = process_dict_type(entry_name, entry_data, schema)

            elif isinstance(entry_data, list):
                schema = process_list_type(entry_name, entry_data, schema)

        return BigQueryTableGenerator.get_header_columns() + schema

    def extract_repeated_fields(self, schema):
        """
        Extract all entries which are of mode REPEATED including foreign key and primary key
        :param schema: The BQ schema to find repeated entries
        :return: The list of a entries including a repeated record, foreign key and the primary key.
        """
        schema_copy = copy.deepcopy(schema)
        for element in schema_copy:
            if element.get("mode") == "REPEATED":
                element["mode"] = "NULLABLE"
                repeated = [
                    {
                        "name": "DwhLogId",
                        "type": "STRING",
                        "mode": "REQUIRED",
                        "description": "Foreign Key of base table",
                    },
                    {
                        "name": "_Id",
                        "type": "STRING",
                        "mode": "REQUIRED",
                        "description": "Primary Key - Unique ID attached to every entry of " "repeated part of a log.",
                    },
                    element,
                ]

                yield repeated, f'{self.base_table_name}_{element["name"]}'

    def extract_enum_fields(self, schema):
        """
        Extract all entries which are of type enum including foreign key and primary key
        :param schema: The BQ schema to find enum entries
        :return: Yields entries including a enum record, foreign key and the primary key.
        """
        for element in copy.deepcopy(schema):
            if element.get("mode") != "REPEATED":
                if element.get("Enum"):
                    yield element.get("Enum"), element["name"]
                if element.get("type") == "RECORD":
                    for enum_schema, enum_name in self.extract_enum_fields(
                            schema=element.get("fields")):
                        enum_schema["RecordName"] = element["name"]
                        yield enum_schema, enum_name

    @staticmethod
    def generate_raw_view(enums: dict, table_id: str):
        """
        Generates a raw view which optionally contains a join to various enum mapping CTEs.
        :param enums:       Dictionary with all enums (as inner dicts containing the mapping) of the
                            schema. If empty no join will be present.
        :param table_id:    Id of the raw table

        :return: SQL statement of the raw view.
        """
        cte = ""

        cte_joins = []
        enum_selects = []
        enum_ctes = []
        for name, enum in enums.items():
            cte_name, bq_name = get_full_name(enum, name)

            cte_joins.append(sql_raw_cte_join(cte_name=cte_name, bq_view_field_name=bq_name))
            enum_selects.append(sql_enum_select(cte_name=cte_name))
            enum_ctes.append(sql_enum_cte(cte_name=cte_name, enum=enum))

        if enum_ctes:
            cte = "WITH " + ",\n".join(enum_ctes) + "\n"

        cte += (
                "SELECT \n"
                + "\traw.*"
                + ("," if enums else "")
                + "\n"
                + "\t"
                + ",\n".join(enum_selects)
                + "\n"
                + "FROM "
                + table_id
                + " as raw\n"
                + "\n".join(cte_joins)
        )
        return cte

    def publish_schema_to_bq(
            self, table: str, git_tag: str, yaml_content, processing_rules, regional_info,
            central_info, output_path=None
    ):
        """
        Write a new schema entry to the schema table.

        :param table:               Name of the table to add entry.
        :param git_tag:             The git tag of the schema. Assumes complete URL as input.
        :param yaml_content:        Base64 encoded content of the yaml file
        :param processing_rules:    Dumped json object how to handle sensitive fields
        :paramn regional_info:      Dict containing the following
                    - file_name:        Proto file name for identifying the file in bitbucket.
                    - package_name:     Full name of the proto package
                    - serialized_proto: Serialized proto file
                    - views:            Dumped json object of views to be generated in each region
                    - tables:           Dumped json object of tables to be generated in each region
                    - bigquery_schema:  Json object containing bigquery schema (not used in this method)
        :param central_info:        Same structure as regional_info, but for central schema.
        """

        payload = {
            "SchemaName": regional_info["package_name"],
            "Schema": base64.b64encode(regional_info["serialized_proto"]).decode("utf-8"),
            "InsertTimestamp": datetime.datetime.fromtimestamp(time.time()).strftime(
                "%Y-%m-%d %H:%M:%S.%f"),
            "GitRepoName": self.git_repo_name.split("/")[-1].split(".")[0],
            "GitTag": git_tag,
            "ProtoContent": base64.b64encode(regional_info["proto_content"].encode()).decode(
                "utf-8"),
            "ProtoFileName": regional_info["file_name"],
            "EnumIdx": True,
            "YamlContent": base64.b64encode(yaml_content.encode()).decode("utf-8"),
            "ProcessingRules": processing_rules,
            "Views": json.dumps(regional_info["views"]),
            "Tables": json.dumps(regional_info["tables"]),
            "CentralSchemaName": central_info["package_name"],
            "CentralSchema": base64.b64encode(central_info["serialized_proto"]).decode("utf-8"),
            "CentralViews": json.dumps(central_info["views"]),
            "CentralTables": json.dumps(central_info["tables"]),
            "CentralProtoContent": base64.b64encode(central_info["proto_content"].encode()).decode(
                "utf-8"),
            "CentralProtoFileName": central_info["file_name"],
        }

        if not self.dry_run:
            errors = self.bigquery_client.insert_rows_json(f"{self.schema_dataset}.{table}",
                                                           [payload])

            if errors != []:
                logging.error(f"Encountered errors while inserting rows {payload} : {errors}")
                raise Exception(errors)

            print(
                f"Inserted schema {regional_info['package_name']} into table {table} "
                f"from repo {self.git_repo_name} with tag {git_tag}."
            )
        else:
            print(f"Dry run, will not touch any GCP resource. Payload is {payload}")
            with open(f"{output_path}/bigquery_schema.txt", "w") as f:
                f.write(json.dumps(payload))


def process_yaml_entry(yaml_entry: Tuple, parent: str = "", mode: str = "NULLABLE") -> dict:
    """
    Create a BQ schema entry from a yaml node.

    :param yaml_entry:
    :param data:    Tuple containing node key and node values (meta information).
    :param parent:  Parent string
    :param mode:    Mode (allowed values: 'NULLABLE', 'REPEATED', 'REQUIRED')

    :return: BQ schema entry
    """
    name = yaml_entry[0]

    try:
        entry = {
            "name": "_".join([parent, name]) if parent else name,
            "type": TYPE_MAPPING[yaml_entry[1]["Type"]],
            "mode": mode if mode in ["NULLABLE", "REPEATED", "REQUIRED"] else "NULLABLE",
            "description": yaml_entry[1]["Description"],
            "required_access_grants": [
                PERM_MAP[key] for key, value in yaml_entry[1].items() if
                PERM_MAP.get(key) and value is True
            ],
        }
        if yaml_entry[1]["Type"] == "enum":
            entry["Enum"] = yaml_entry[1]["Enum"]

    except KeyError as e:
        logging.error(f"Error in parsing yaml to BQ schema {e}")
        raise e

    return entry


def process_dict_type(key: str, value: Dict, field_list: List) -> List:
    """
    Process a dict of entries used for non-repeated record
    :param key: The key of the repeated field
    :param value: The dict of entries of the repeated record
    :param field_list: The processed BQ schema including the repeated record
    :return: A list of fields representing a non-repeated record
    """
    field_list, is_param = append_if_is_parameter_block(key, value, field_list)
    if not is_param:
        fields = []
        for k, v in value.items():
            fields, _ = append_if_is_parameter_block(k, v, fields)

        if len(fields) > 0:
            field_list.append(create_record(key, fields, "NULLABLE"))

    return field_list


def append_if_is_parameter_block(key: str, value: Dict, field_list: List) -> Tuple[List, bool]:
    """
    Check if a node is a fully qualified BigQuery schema 'parameter block' (i.e., all
    required meta information are present) and append to field list if this is the case.
    Otherwise, return an untouched field list.

    :param key:         Name of the Yaml parameter.
    :param value:       Meta information of the yaml parameter.
    :param field_list:  BigQuery schema with all (up to this point) processed yaml entries.

    :return: Tuple with field_list and a boolean indicating if the field_list has been
    updated or not.
    """
    fully_qualified_param_block = False

    if len(NEEDED_KEYS & set(list(value.keys()))) == len(NEEDED_KEYS):
        field_list.append(
            process_yaml_entry(yaml_entry=(key, value), mode=value.get("Mode", "NULLABLE")))
        fully_qualified_param_block = True

    return field_list, fully_qualified_param_block


def create_record(name, fields, mode):
    return {"name": name, "type": "RECORD", "mode": mode, "fields": fields}


def process_list_type(key: str, value: List, field_list: List) -> List:
    """
    Process a list of entries used for repeated record
    :param key: The key of the repeated field
    :param value: The list of entries of the repeated record
    :param field_list: The processed BQ schema including the repeated record
    :return: A list of entries of a repeated record
    """
    fields = []
    for ent in value:
        for k, v in ent.items():
            fields, _ = append_if_is_parameter_block(k, v, fields)

    if len(fields) > 0:
        field_list.append(create_record(key, fields, "REPEATED"))

    return field_list


def get_full_name(enum: dict, name: str):
    """
    Create full name for enum labels
    :param enum: Enum data
    :param name: Enum name
    :return: Full names with record name suffix with underscore and point delimiter, respectively.
    """
    record_name = enum.get("RecordName", "")
    return (f"{record_name + '_' if record_name else ''}{name}",
            f"{record_name + '.' if record_name else ''}{name}")


def sql_enum_cte(cte_name: str, enum: dict):
    """
    Generate a enum index string mapping
    :param enums: Enum data
    """
    return (
            cte_name
            + " AS ( \n"
            + "\tSELECT \n"
            + "\t\t*\n"
            + "\tFROM\n"
            + "\t\tUNNEST(\n"
            + "\t\t\tARRAY<STRUCT<name STRING, index INT64>>[\n\t\t\t\t"
            + ",\n\t\t\t\t".join(
                f"('{name}', {index})" for name, index in enum.items() if name != "RecordName")
            + "\n"
            + "\t\t\t]\n"
            + "\t)\n"
            + ")"
    )


def sql_raw_cte_join(cte_name: str, bq_view_field_name: str):
    """Generates SQL join between raw and enum CTE."""
    return f"LEFT JOIN {cte_name} as {cte_name}\nON {cte_name}.index = raw.{bq_view_field_name}"


def sql_enum_select(cte_name: str):
    """Generates SQL enum select."""
    return f"{cte_name}.name AS {cte_name}_label"
