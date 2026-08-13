import argparse
import json
import os
import re
import sys
from pathlib import Path

import jsonschema
import yaml

PARSER = argparse.ArgumentParser(description="Validate yaml files against a given json-schema file")
PARSER.add_argument("-js", "--json_schema", help="JSON schema file for validating yamls")
PARSER.add_argument("-srd", "--schema_repo_dir", help="Directory where schema repo is located")

VALIDATION_RESULT = {"success": [], "errors": []}

FILE_NAME_REGEX = r"^[\w]*$"


def main(args):
    validation_schema = read_validation_schema(args.json_schema)

    yaml_files = get_files_from_dir(dir=args.schema_repo_dir)

    for yaml_file in yaml_files:
        validation_steps(validation_schema, yaml_file)

    if VALIDATION_RESULT["errors"]:
        print(json.dumps(VALIDATION_RESULT, ensure_ascii=False, indent=4))
        sys.exit(1)


def validation_steps(validation_schema: dict, yaml_file: str):
    """
    Combines several steps during schema validation starting with checking the yaml file name,
    subsequently the yaml content is read into a python dict, the content is validated and at the
    end all enums are checked.

    If successful no outputs or return values, otherwise errors are written into 'VALIDATION_RESULT'
    """
    validate_file_name(yaml_file)

    yaml_content = read_yaml_file(yaml_file)
    if yaml_content:
        validate_yaml_content(validation_schema, yaml_content=yaml_content,
                              yaml_file_path=yaml_file)
        if not VALIDATION_RESULT["errors"]:
            validate_enums(yaml_content=yaml_content)


def validate_file_name(yaml_file):
    file_name, suffix = os.path.split(yaml_file)[1].split(".")
    if not re.match(FILE_NAME_REGEX, file_name):
        VALIDATION_RESULT["errors"].append(
            f"File name: {yaml_file} does not match regex {FILE_NAME_REGEX}")
    if suffix != "yaml":
        VALIDATION_RESULT["errors"].append(
            f"File name: {yaml_file} has invalid file extension (has to be .yaml)")


def validate_enums(yaml_content: dict):
    """
    Searches the yaml content for enum entries and validates the syntax
    of each enum using the check_enum function below.
    """
    all_enum_items = {}
    known_enums = []
    for param_name, yaml_param in yaml_content.get("Content", {}).items():
        if isinstance(yaml_param, dict) and yaml_param.get("Type", "").lower() == "enum":
            all_enum_items, known_enums = check_enum(all_enum_items, known_enums,
                                                     yaml_param.get("Enum", []))
        elif isinstance(yaml_param, dict) and "Enum" in yaml_param.keys() and yaml_param.get("Type",
                                                                                             "") != "enum":

            VALIDATION_RESULT["errors"].append(
                f'''In "{param_name}" enum items are defined but type is of kind "{yaml_param.get('Type', '')}"'''
            )
        elif isinstance(yaml_param, list):
            for nested_repeated_param in yaml_param[0].values():
                if nested_repeated_param.get("Type", "").lower() == "enum":
                    all_enum_items, known_enums = check_enum(
                        all_enum_items, known_enums, nested_repeated_param.get("Enum", [])
                    )
        elif isinstance(yaml_param, dict) and "Type" not in yaml_param.keys():
            for nested_param in yaml_param.values():
                if nested_param.get("Type", "").lower() == "enum":
                    all_enum_items, known_enums = check_enum(all_enum_items, known_enums,
                                                             nested_param.get("Enum", []))


def get_indexes_of_duplicate_dict_values(data: dict):
    """
    Returns a list of indexes of duplicate values in a dict.
    :param data: Dict to check for duplicate values
    :return: List of indexes of duplicate values
    """
    enum_id_list = [v for _, v in data.items()]
    return list(set([i for i in enum_id_list if enum_id_list.count(i) > 1]))


def check_enum(all_enum_items: dict, known_enums: list, current_enum: dict):
    """
    Every enum defined in a schema has to be validated according to duplicates and subsets.
    If an enum is already defined including the exactly same items it is accepted, if any item in
    an enum is used in another enum an error will be reported into 'VALIDATION_RESULTS' variable.

    returns a list of all enum items and a list of enum item lists.
    """
    if duplicates := get_indexes_of_duplicate_dict_values(current_enum):
        err = f"Multiple definition of ids {duplicates} in enum {current_enum}"
        VALIDATION_RESULT["errors"].append(err)

    if 0 not in current_enum.values():
        err = f"Item with id 0 missing in enum {current_enum}"
        VALIDATION_RESULT["errors"].append(err)

    current_enum = {k: v for k, v in sorted(current_enum.items(), key=lambda item: item[1])}
    already_known = False
    for known_enum in known_enums:
        if known_enum == current_enum:
            already_known = True
            break
    else:
        known_enums.append(current_enum)

    if not already_known:
        for item_name, id in current_enum.items():
            if id != all_enum_items.get(item_name, id):
                err = (
                    f'ID missmatch: Item "{item_name}" with id "{id}" '
                    f'is already defined with id "{all_enum_items[item_name]}"'
                )
                VALIDATION_RESULT["errors"].append(err)
            if all_enum_items.get(item_name) is not None:
                err = f'Duplicates within Enum entries detected: "{item_name}" ' f"is already defined in another enum"
                VALIDATION_RESULT["errors"].append(err)
            else:
                all_enum_items[item_name] = id

    return all_enum_items, known_enums


def validate_yaml_content(validation_schema, yaml_content: str, yaml_file_path: str):
    """Validate YAML against json schema and fill global variable with validation result."""
    try:
        validator = jsonschema.Draft202012Validator(validation_schema)
        handle_errors(errors=validator.iter_errors(yaml_content), yaml_file=yaml_file_path)
    except jsonschema.SchemaError as err:
        VALIDATION_RESULT["errors"].append(str(err))


def read_validation_schema(validation_schema_file: str) -> dict:
    """Opens validation schema as JSON."""
    with open(validation_schema_file, "r", encoding="utf-8") as stream:
        try:
            return json.load(stream)
        except json.JSONDecodeError as err:
            raise err


def get_files_from_dir(dir=os.path.join("src")) -> list:
    """Returns a list of all yaml files located iwithin directory."""
    return [str(file) for file in Path(dir).rglob("*.yaml") if "example_files" not in str(file)]


def read_yaml_file(yaml_schema_file: str) -> tuple:
    """
    Load YAML file and check for duplicate keys. If duplicate keys are present an AssertError
    is risen.
    """

    # Overload default loader to check for duplicate keys in YAML
    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            mapping = []
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                assert key not in mapping, f'{yaml_schema_file}: Duplicate key "{key}"!'
                mapping.append(key)
            return super().construct_mapping(node, deep)

    with open(yaml_schema_file, "r") as stream:
        try:
            if yaml_content := yaml.load(stream, Loader=UniqueKeyLoader):
                return yaml_content
            else:
                VALIDATION_RESULT["errors"].append(f"File name: {yaml_schema_file} is empty")

        except yaml.YAMLError as err:
            VALIDATION_RESULT["errors"].append(str(err))
        except AssertionError as err:
            VALIDATION_RESULT["errors"].append(str(err))


def print_error(error, yaml_file) -> None:
    error_msg_start = f"{yaml_file}:"

    if error.context == []:

        error_msg = f'{error_msg_start} Node "{error.json_path}": "{error.message}"'

        VALIDATION_RESULT["errors"].append(error_msg)

    else:
        for c in error.context:
            error_msg = error_msg_start

            schema_id = c.schema.get("$id")

            # Filter out irrelevant "errors", e.g., simple parameters are no nested/repeated nested
            # parameters and thus no objects/arrays
            if (
                    not c.validator == "type"
                    or c.validator_value not in ["object", "array"]
                    or (
                    # filter out enum errors
                    c.validator == "type"
                    and c.validator_value == "object"
                    and "Enum" in c.path
                    )
            ):

                error_msg += f' Node "{c.json_path}"'

                if schema_id is not None:
                    error_msg += f" if \"{c.schema['$id']}\":"
                else:
                    error_msg += ":"

                error_msg += f' "{c.message}"'

                VALIDATION_RESULT["errors"].append(error_msg)


def handle_errors(errors, yaml_file):
    validation_failed = False
    for error in errors:
        if error:
            validation_failed = True
            print_error(error, yaml_file)
    if not validation_failed:
        VALIDATION_RESULT["success"].append(f"{yaml_file}: Successfully validated")


if __name__ == "__main__":
    args = PARSER.parse_args(sys.argv[1:])
    main(args)
