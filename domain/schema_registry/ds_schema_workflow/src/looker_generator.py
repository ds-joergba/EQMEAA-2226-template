import logging
import os
import pathlib
import re
import shutil
import uuid
from copy import deepcopy
from typing import List

import looker_sdk
from looker_sdk import models40 as models
from looker_sdk.error import SDKError
from lookmlgen.base_generator import GeneratorFormatOptions
from lookmlgen.field import DEFAULT_TYPE, DimensionGroup, Field, FieldType, Measure
from lookmlgen.view import View

logging.basicConfig(level=logging.INFO)

ML_TYPE_MAP = {
    "INTEGER": "number",
    "FLOAT64": "number",
    "STRING": "string",
    "BOOLEAN": "yesno",
    "TIMESTAMP": "TIMESTAMP",
}

VIEW_FILE_PATH = "views"
MODEL_FILE_PATH = "models"
DASHBOARD_FILE_PATH = "dashboards"

CONNECTION = "dwh"
REPLACE_ENV_WITH_GCP_PROJECT = (
        """{% if _user_attributes['env']=="dev" %}prj-eqmeaa-dev-agkt"""
        + """{% elsif _user_attributes['env']=="staging" %}prj-eqmeaa-staging-glgc"""
        + """{% elsif _user_attributes['env'] =="prod" %}prj-eqmeaa-prod-yate{% endif %}"""
)


def to_snake_case(string):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", string).strip("_").lower()


def snake_to_upper_case(snake_case_string: str):
    """
    Converts a snake to a space-seperated capitalized string.
    :rtype: Converted string
    """
    return " ".join(x.capitalize() for x in snake_case_string.lower().split("_"))


# Driver code
class Dimension(Field):
    def __init__(
            self,
            name,
            primary_key=None,
            type=DEFAULT_TYPE,
            label=None,
            sql=None,
            hidden=None,
            file=None,
            group_label=None,
            description=None,
            **kwargs,
    ):
        super(Dimension, self).__init__(
            FieldType.DIMENSION, name, type, label, sql, hidden, file, group_label, description,
            **kwargs
        )
        self.primary_key = primary_key
        self.field = {}
        for key, value in kwargs.items():
            self.field[key] = value

    def _generate(self, f, fo):
        """
        `       Overwrite method from base class to generate custom output fields
                :param f: File handle for view output
                :param fo: Format options from base class
        """
        if self.primary_key:
            f.write("{indent}primary_key: yes\n".format(indent=" " * 2 * fo.indent_spaces))
        for key, value in self.field.items():
            if value:
                f.write(
                    "{indent}{key}: {value}\n".format(indent=" " * 2 * fo.indent_spaces, key=key,
                                                      value=value))


class LookerDashboardGenerator:
    def __init__(self, looker_root):
        self.explore_name = ""
        self.repeated_header_fields = lambda: (
            f"{self.explore_name}.header_cloud_timestamp_time, " f"{self.explore_name}.dwh_log_id, "
        )

        self.raw_template, self.raw_element_template = self.load_templates(looker_root)

    @staticmethod
    def load_templates(looker_root):
        """
        Load Dashboard templates
        :return: Dashboard and Element template
        """
        with open(f"{looker_root}/raw_template.dashboard.lookml", "r") as f:
            raw_template = f.read()
        with open(f"{looker_root}/raw_element_template.dashboard.lookml", "r") as f:
            raw_element_template = f.read()

        return raw_template, raw_element_template

    def set_tags(self, title, index_fields="", fields=[]):
        """
        Replace place holder for a dashboard element
        :param fields: Table columns from explore
        :param title: Table title
        :param index_fields: Table columns from index explore
        :return: Filled table element block
        """
        element = ""
        if fields:
            element = self.raw_element_template.replace("$DIMS", index_fields + ", ".join(fields))
            element = element.replace("$TITLE", title)
        return element

    def get_element_block(self, schema):
        """
        Method builds element tag for repeated records dimensions
        :param schema: YAML schema

        :return: Dict with element blocks of all types (simple fields, repeated fields, and repeated
        record fields).
        """
        repeated_block = ""
        repeated_record_block = ""
        simple_fields = []  # Collect all simple fields before creating the element block
        for dim in schema:
            fields = []
            field_prefix = ""
            if dim["mode"] == "REPEATED":
                field_prefix = f'{self.explore_name}__{to_snake_case(dim["name"])}'
            if dim["type"] == "RECORD":
                for field in dim["fields"]:
                    fields = self.fill_element_block_fields(
                        fields, field,
                        to_snake_case(dim["name"]) if dim["mode"] != "REPEATED" else field_prefix
                    )
                if field_prefix:
                    repeated_record_block += self.set_tags(
                        fields=fields, title=to_snake_case(dim["name"]),
                        index_fields=self.repeated_header_fields()
                    )
                else:
                    simple_fields.extend(fields)
            else:

                fields = self.fill_element_block_fields(
                    fields, dim, prefix=field_prefix if field_prefix else self.explore_name
                )

                if field_prefix:
                    repeated_block += self.set_tags(
                        fields=fields, title=to_snake_case(dim["name"]),
                        index_fields=self.repeated_header_fields()
                    )
                else:
                    simple_fields.extend(fields)
        return {
            "simple_fields": self.set_tags(fields=simple_fields,
                                           title="Simple Parameters and Records"),
            "repeated_fields": repeated_block,
            "repeated_record_fields": repeated_record_block,
        }

    @staticmethod
    def fill_element_block_fields(fields: list, field: dict, prefix: str = ""):
        """
        Appends (potentially) prefixed string to field list for element blocks.

        :param fields:  List of fields of element block in question. Will be updated in this method.
        :param field:   Field which is appended to output list.
        :param prefix:  Optional prefix which is added to output.

        :returns:
        Updated fields list, depending on field type and if an enum is present.
        """
        prefix = f"{prefix}." if prefix else ""

        field_name = f'{prefix}{to_snake_case(field["name"])}'

        if field["type"] == "TIMESTAMP":
            fields.append(f"{field_name}_time")
        else:
            fields.append(f"{field_name}")
        if field.get("Enum"):
            fields.append(f"{field_name}_label")

        return fields

    def create(self, schema, explore_name):
        """
        Creates a complete dashboard lookml
        :param schema: YAML schema
        :return: Lookml string of dashboard
        """
        self.explore_name = explore_name
        title = self.explore_name.rsplit("_", 1)[0]
        element_block = self.get_element_block(schema)
        return (
            self.raw_template.replace("$DASHBOARD_ID", title.lower())
            .replace("$DASHBOARD_TITLE", title)
            .replace("$SIMPLE_DIMS", element_block["simple_fields"])
            .replace("$REPEATED_DIMS", element_block["repeated_fields"])
            .replace("$REPEATED_RECORDS", element_block["repeated_record_fields"])
            .replace("$EXPLORE", self.explore_name)
        )

    @staticmethod
    def deploy(lookml, folder_id, looker_ini_path):
        """
        Deploys a dashboard lookml string to looker.
        :param lookml: Lookml string of dashboard
        :param folder_id: Folder ID where to deploy the dashboard to
        """
        try:
            looker_handler = looker_sdk.init40(looker_ini_path)
            logging.info("Looker SDK 4 initialized successfully.")
            my_user = looker_handler.me()
            logging.info(f"Logged in as {my_user.first_name} {my_user.last_name}")
            response = looker_handler.import_dashboard_from_lookml(
                body=models.DashboardLookml(folder_id=folder_id, dashboard_id=str(uuid.uuid4()),
                                            lookml=lookml)
            )
            logging.info(f"Uploaded dashboard response: {response}")
        except SDKError as e:
            logging.error(e)


class LookmlViewGenerator:
    def __init__(self, raw1_dataset, raw2_dataset, looker_root, looker_access, table_id):
        """
        The class provides methods to generate a lookml view and explore
        :param raw1_dataset: The name of the dataset to which the raw1 view will be connected
        :param raw2_dataset: The name of the dataset to which the raw2 view will be connected
        :param looker_root: The root path where the genared view and explore will be written
        :param looker_access: Global access to the looker explore
        """
        self.raw1_dataset = raw1_dataset
        self.raw2_dataset = raw2_dataset
        self.looker_root = looker_root
        self.looker_access = looker_access
        self.table_id = table_id

    def create_views_and_model(self, schema) -> str:
        """
        Create a lookml view file and appends the explor to an existing model file.
        :param schema:  YAML schame to generate looker stuff from
        :return The name of explore
        """
        self.schema = schema
        view_path, model_path = self.create_looker_dirs()
        self.add_default_index_table(view_path)
        views, view_file_path = self.generate_view(
            schema=self.schema, raw1_dataset=self.raw1_dataset, raw2_dataset=self.raw2_dataset,
            view_path=view_path
        )
        logging.info("replacing buggy count measure")
        self.manipulate_count_measure(view_file_path)
        return self.generate_explore(views, model_path, model_file_name="ds_core_raw.model.lkml")

    @staticmethod
    def create_new_model_file(model_file_path):
        """
        Creates a new model file with a header if the file does not exist.
        :param model_file_path:
        """
        model_header = (
            f'connection: "{CONNECTION}"\n'
            f'label: "DS-Core Logs"\n'
            f'include: "../{VIEW_FILE_PATH}/*.view"\n\n'
            "access_grant: can_access_ds_core_raw {\n"
            "\tuser_attribute: ds_core_raw\n"
            '\tallowed_values: ["yes"]\n'
            "}\n\n"
        )
        with open(model_file_path, "w") as f:
            f.write(model_header)
        logging.info(f"Created new model file {model_file_path}")

    @staticmethod
    def add_join_to_explore(base_view: str, repeated_view: str) -> str:
        """
        Add a join block to an explore
        :param repeated_view: The name of the repeated view
        :param base_view: The name of the base view.
        :return: The join string.
        """
        return (
            f"\tjoin: {repeated_view}{{\n"
            f'\t\tview_label: "{repeated_view}"\n'
            f"\t\tsql_on: ${{{base_view}.dwh_log_id}} = ${{{repeated_view}.dwh_log_id}};;\n"
            f"\t\trelationship: one_to_many\n"
            f"\t}}\n"
        )

    def generate_explore(self, views: List, model_path: str, model_file_name: str) -> str:
        """
        Generate a explore and appends it to a model file
        :param views: A list of views to generate an explore from
        :param model_path: The file path to the model file
        :param model_file_name: The model file name
        :return The name of the explore
        """
        model_file_path = os.path.join(model_path, model_file_name)
        if not os.path.exists(model_file_path):
            self.create_new_model_file(model_file_path)

        access_grant_name = "allowed_companies, can_access_ds_core_raw"
        access_grant = ""

        if self.looker_access is not None:
            access_grant_name = self.table_id
            access_grant = (
                f"access_grant: {access_grant_name} {{\n"
                f"\tuser_attribute: {self.looker_access}\n"
                '\tallowed_values: ["yes"]\n'
                "}\n\n"
            )

        explore = (
                access_grant + f"explore: {views[0]} {{\n"
                               f"\trequired_access_grants: [{access_grant_name}]\n"
                               f"\tsql_always_where: DATE_DIFF(CURRENT_DATE(), "
                               f"${{{views[0]}.header_cloud_timestamp_date}}, MONTH) <= 6;;\n"
        )

        for view in views[1:]:
            explore += self.add_join_to_explore(base_view=views[0], repeated_view=view)
        explore = f"{explore}\n}}\n"
        with open(model_file_path, "a") as f:
            f.write(explore)

        logging.info(f"Appended explore {views[0]} to model file {model_file_path}")

        return views[0]

    def create_looker_dirs(self):
        """
        Create a view and a model dir if the dirs do not exist.
        """
        view_path = os.path.join(self.looker_root, VIEW_FILE_PATH)
        model_path = os.path.join(self.looker_root, MODEL_FILE_PATH)
        if not os.path.exists(view_path):
            os.makedirs(view_path)
            logging.info(f"Created directory {view_path}")
        if not os.path.exists(model_path):
            os.makedirs(model_path)
            logging.info(f"Created directory {view_path}")

        return view_path, model_path

    @staticmethod
    def get_sql(access_grants: list, default_sql: str):
        """
        Generate the Looker sql statement bases on access grants.
        :param access_grants: List of access grants
        :param default_sql: The default sql when no access
                            grants are set or when on non prod env.
        :return: The generated sql statement for lookml
        """
        if not access_grants:
            return f"{default_sql}"
        else:
            pems = " AND ".join(
                [f"'{{{{_user_attributes['{pem}']}}}}' = 'no'" for pem in access_grants])

            return (
                f"\n\tCASE \n"
                f"\t\tWHEN '{{{{_user_attributes['env']}}}}' = 'prod' "
                f"AND ({pems}) THEN NULL \n"
                f"\t\tELSE {default_sql} \n"
                f"\tEND \n"
            )

    def add_dimension(self, dimension: dict, dim_type: str = "default", default_sql=None):
        """
        Creates a new view dimension block, depending on the type.

        :param dimension:   Dict containing info about the dimension. If the dimension name is equal
                            to 'DwhLogId' then this field set as primary key is returned.
        :param dim_type:    Type of the dimension, resulting in slightly different behavior.
        :param default_sql: Default sql statement. If empty '${TABLE}.dimension_name' is added.

        :return: New dimension. Either standard, timestamp, or enum, depending on dim_type.
        """
        if dim_type.lower() == "enum":
            dimension["name"] += "_label"
            dimension["type"] = "STRING"

        dim_label = ""
        label = dimension["name"].split(".")
        if len(label) > 1:
            dim_label = f"{label[0]}.{snake_to_upper_case(to_snake_case(label[1]))}"

        if dimension["name"] == "DwhLogId":
            return self.get_dwh_log_id_dim(primary_key=True)

        if default_sql is None:
            default_sql = f"${{TABLE}}.{dimension['name']}"

        d = Dimension(
            to_snake_case(dimension["name"]),
            description=dimension["description"],
            sql=self.get_sql(dimension.get("required_access_grants"), default_sql=default_sql),
            label=dim_label,
        )

        if dim_type.lower() == "timestamp":
            d = DimensionGroup(
                name=d.name,
                description=d.description,
                sql=d.sql,
                timeframes=[
                    "yesno",
                    "raw",
                    "time",
                    "hour",
                    "hour_of_day",
                    "date",
                    "day_of_week",
                    "day_of_week_index",
                    "week",
                    "month",
                    "month_num",
                    "month_name",
                    "quarter",
                    "year",
                    "fiscal_year",
                ],
                datatype="",
            )
        elif dim_type.lower() == "enum":
            d.description = d.description + " (enum label)"
        else:
            d.type = ML_TYPE_MAP[dimension["type"]]

        return d

    @staticmethod
    def get_dwh_log_id_dim(primary_key=False):
        """
        Generates Looker dimension for DwhLogId field.

        :param primary_key: Boolean flag indicating if the dimension is the primary key of the view

        :returns:   Looker Dimension class.
        """
        d = Dimension(
            name="dwh_log_id", type="string", sql="${TABLE}.DwhLogId",
            description="Foreign key to base table"
        )
        if primary_key:
            d.primary_key = True
            d.description = "Primary key (DwhLogId)"

        return d

    @staticmethod
    def get_id_dim():
        return Dimension(
            name="_id",
            type="string",
            description="Primary key",
            primary_key=True,
            sql="${TABLE}._Id",
            required_access_grants="[eaa_only]",
            group_label="EAA_ONLY",
        )

    def create_repeated_view(self, dimension: dict, base_view_name: str):
        """
        Creates a new repeated view
        :param dimension:       The dimension that should be added (may be a repeated record with
                                multiple fields)
        :param base_view_name:  The name of the base view

        :return:    Newly created view.
        """
        view_name = f'{self.view_name}__{to_snake_case(dimension["name"])}'
        v = View(view_name, sql_table_name=f'`{base_view_name}_{dimension["name"]}_view`')
        v.add_field(self.get_dwh_log_id_dim())
        v.add_field(self.get_id_dim())

        return self.add_dimension_to_view(view=v, dimension=dimension)

    def add_view_field(self, view: View, dimension, default_sql=None, default_enum_sql=None):
        """
        Adds a new dimension to the view and an additional label dimension, in case of an enum.

        :param view:                The view that needs to be updated.
        :param dimension:           The dimension which is added to the view.
        :param default_sql:         Optional default sql statement for the dimension
        :param default_enum_sql:    Optional default sql statement for the enum label dimension

        :return:    Updated view.
        """
        view.add_field(self.add_dimension(dimension, dimension["type"], default_sql))

        if dimension.get("Enum"):
            view.add_field(self.add_dimension(dimension, "enum", default_enum_sql))

        return view

    def add_dimension_to_view(self, view: View, dimension):
        """
        Adds a given dimension to the provided view and returns the updated view.

        :param view:        The view that is extended
        :param dimension:   The dimension which is added to the view.

        :return:    Updated view.
        """
        dimension = deepcopy(dimension)
        if dimension["type"] == "RECORD":
            for field in dimension["fields"]:
                if dimension["mode"] != "REPEATED":
                    default_enum_sql = f"${{TABLE}}.{dimension['name']}_{field['name']}_label"
                    field["name"] = f"{dimension['name']}.{field['name']}"
                    default_sql = None
                else:
                    default_sql = f"${{TABLE}}.{dimension['name']}.{field['name']}"
                    default_enum_sql = f"${{TABLE}}.{dimension['name']}_{field['name']}_label"

                view = self.add_view_field(
                    view=view, dimension=field, default_sql=default_sql,
                    default_enum_sql=default_enum_sql
                )
        else:
            view = self.add_view_field(view=view, dimension=dimension)
        return view

    @staticmethod
    def manipulate_count_measure(view_file_path):
        find_string = "  measure: count {\n    type: count\n    sql:   ;;"
        replace_string = "  measure: count {\n    type: count\n    drill_fields: []"

        with open(view_file_path, "r") as f:
            content = f.read()

        manipulated_content = content.replace(find_string, replace_string)
        logging.info("manipulated_content")
        with open(view_file_path, "w") as f:
            f.write(manipulated_content)

    def generate_view(self, schema, raw1_dataset, raw2_dataset, view_path):
        """
        Generate and save views corresponding to a BQ table schema.
        :param schema: Table schema containing view info
        :param raw1_dataset: raw1_dataset name
        :param raw2_dataset: raw2_dataset name
        :param view_path: Path to the view file
        :return: List of generated views
        """
        view_file_path = os.path.join(view_path, f"{to_snake_case(self.table_id)}.view.lkml")
        self.view_name = self.table_id.replace("-", "_")

        base_table_name_raw1 = f"{REPLACE_ENV_WITH_GCP_PROJECT}.{raw1_dataset}.{self.table_id}_view"
        v = View(self.view_name, sql_table_name=f"`{base_table_name_raw1}`")

        base_table_name_raw2 = f"{REPLACE_ENV_WITH_GCP_PROJECT}.{raw2_dataset}.{self.table_id}"

        repeated_views = []
        repeated_views_names = []
        for dim in schema:
            if dim["mode"] != "REPEATED":
                v = self.add_dimension_to_view(view=v, dimension=dim)
            else:
                repeated_v = self.create_repeated_view(dimension=dim,
                                                       base_view_name=base_table_name_raw2)
                repeated_views.append(repeated_v)
                repeated_views_names.append(repeated_v.name)

        v.add_field(Measure(name="count", type="count", sql=" "))
        logging.info(f"Added main view {v.name}")

        self.view_to_lookml(view=v, file_path=view_file_path, writing_disposition="w")

        for repeated_view in repeated_views:
            self.view_to_lookml(view=repeated_view, file_path=view_file_path,
                                writing_disposition="a")

        views = [v.name] + repeated_views_names
        logging.info(f"Saved view to {view_file_path}")
        return views, view_file_path

    @staticmethod
    def view_to_lookml(view: View, file_path: str, writing_disposition: str):
        """
        Writes generated lookml code from view to specified path.

        :param view:                The view for which the LookML should be written.
        :param fiel_path:           Path of the output file
        :param writing_disposition: The writing disposition (e.g., a=append, w=write & truncate,
                                    see https://docs.python.org/3/library/functions.html#open)
        """
        with open(file_path, f"{writing_disposition}") as f:
            view.generate_lookml(
                f,
                GeneratorFormatOptions(
                    view_fields_alphabetical=False, warning_header_comment="",
                    omit_default_field_type=True
                ),
            )

    @staticmethod
    def add_default_index_table(view_path) -> None:
        """
        Checks if the ds_core_index_1 view is already present and adds the file if this is not
        the case.

        :param view_path:   relative path to directory containing Looker views
        """
        index_view = "ds_core_index_1.view.lkml"

        if not os.path.isfile(f"{view_path}/{index_view}"):
            shutil.copy(f"{pathlib.Path(__file__).parent.resolve()}/looker/{index_view}",
                        f"{view_path}/{index_view}")
