"""``BaseTableDataset`` implementation used to add the base for
``ManagedTableDataset`` and ``ExternalTableDataset``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, List

import pandas as pd
from kedro.io.core import (
    AbstractVersionedDataset,
    DatasetError
)
from pyspark.sql import DataFrame
from pyspark.sql.types import StructType
from pyspark.sql.utils import AnalysisException, ParseException

from kedro_datasets.spark.spark_dataset import _get_spark

logger = logging.getLogger(__name__)
pd.DataFrame.iteritems = pd.DataFrame.items


@dataclass(frozen=True, kw_only=True)
class BaseTable:
    """Stores the definition of a base table.
    
    Acts as a base class for `ManagedTable` and `ExternalTable`.
    """
    # regex for tables, catalogs and schemas
    _NAMING_REGEX: ClassVar[str] = r"\b[0-9a-zA-Z_-]{1,}\b"
    _VALID_WRITE_MODES: ClassVar[List[str]] = field(default=["overwrite", "append"])
    _VALID_DATAFRAME_TYPES: ClassVar[List[str]] = field(default=["spark", "pandas"])
    _VALID_FORMATS: ClassVar[List[str]] = field(default=["delta", "parquet", "csv"])

    format: str
    database: str
    catalog: str | None
    table: str
    write_mode: str | None
    dataframe_type: str
    owner_group: str | None
    partition_columns: str | list[str] | None
    json_schema: dict[str, Any] | None = None

    def __post_init__(self):
        """Run validation methods if declared.

        The validation method can be a simple check
        that raises DatasetError.

        The validation is performed by calling a function with the signature
        `validate_<field_name>(self, value) -> raises DatasetError`.
        """
        for name in self.__dataclass_fields__.keys():
            method = getattr(self, f"_validate_{name}", None)
            if method:
                method()

    def _validate_format(self):
        """Validates the format of the table.

        Raises:
            DatasetError: If an invalid `format` is passed.
        """
        if self.format not in self._VALID_FORMATS:
            valid_formats = ", ".join(self._VALID_FORMATS)
            raise DatasetError(
                f"Invalid `format` provided: {self.format}. "
                f"`format` must be one of: {valid_formats}"
            )

    def _validate_table(self):
        """Validates table name.

        Raises:
            DatasetError: If the table name does not conform to naming constraints.
        """
        if not re.fullmatch(self._NAMING_REGEX, self.table):
            raise DatasetError("table does not conform to naming")

    def _validate_database(self):
        """Validates database name.

        Raises:
            DatasetError: If the dataset name does not conform to naming constraints.
        """
        if not re.fullmatch(self._NAMING_REGEX, self.database):
            raise DatasetError("database does not conform to naming")

    def _validate_catalog(self):
        """Validates catalog name.

        Raises:
            DatasetError: If the catalog name does not conform to naming constraints.
        """
        if self.catalog:
            if not re.fullmatch(self._NAMING_REGEX, self.catalog):
                raise DatasetError("catalog does not conform to naming")

    def _validate_write_mode(self):
        """Validates the write mode.

        Raises:
            DatasetError: If an invalid `write_mode` is passed.
        """
        if (
            self.write_mode is not None
            and self.write_mode not in self._VALID_WRITE_MODES
        ):
            valid_modes = ", ".join(self._VALID_WRITE_MODES)
            raise DatasetError(
                f"Invalid `write_mode` provided: {self.write_mode}. "
                f"`write_mode` must be one of: {valid_modes}"
            )

    def _validate_dataframe_type(self):
        """Validates the dataframe type.

        Raises:
            DatasetError: If an invalid `dataframe_type` is passed
        """
        if self.dataframe_type not in self._VALID_DATAFRAME_TYPES:
            valid_types = ", ".join(self._VALID_DATAFRAME_TYPES)
            raise DatasetError(f"`dataframe_type` must be one of {valid_types}")

    def _validate_primary_key(self):
        """Validates the primary key of the table.

        Raises:
            DatasetError: If no `primary_key` is specified.
        """
        if self.primary_key is None or len(self.primary_key) == 0:
            if self.write_mode == "upsert":
                raise DatasetError(
                    f"`primary_key` must be provided for"
                    f"`write_mode` {self.write_mode}"
                )

    def full_table_location(self) -> str | None:
        """Returns the full table location.

        Returns:
            str | None : table location in the format catalog.database.table or None if database and table aren't defined
        """
        full_table_location = None
        if self.catalog and self.database and self.table:
            full_table_location = f"`{self.catalog}`.`{self.database}`.`{self.table}`"
        elif self.database and self.table:
            full_table_location = f"`{self.database}`.`{self.table}`"
        return full_table_location

    def schema(self) -> StructType | None:
        """Returns the Spark schema of the table if it exists.

        Returns:
            StructType:
        """
        schema = None
        try:
            if self.json_schema is not None:
                schema = StructType.fromJson(self.json_schema)
        except (KeyError, ValueError) as exc:
            raise DatasetError(exc) from exc
        return schema
    

class BaseTableDataset(AbstractVersionedDataset):
    """``BaseTableDataset`` loads and saves data into managed delta tables or external tables on Databricks.
    Load and save can be in Spark or Pandas dataframes, specified in dataframe_type.

    This dataaset is not meant to be used directly. It is a base class for ``ManagedTableDataset`` and ``ExternalTableDataset``.
    """

    # this dataset cannot be used with ``ParallelRunner``,
    # therefore it has the attribute ``_SINGLE_PROCESS = True``
    # for parallelism within a Spark pipeline please consider
    # using ``ThreadRunner`` instead
    _SINGLE_PROCESS = True

    def __init__(  # noqa: PLR0913
        self,
        *,
        table: str,
        catalog: str | None = None,
        database: str = "default",
        write_mode: str | None = "overwrite",
        dataframe_type: str = "spark",
        # the following parameters are used by project hooks
        # to create or update table properties
        schema: dict[str, Any] | None = None,
        partition_columns: list[str] | None = None,
        owner_group: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Creates a new instance of ``BaseTableDataset``.

        Args:
            table: the name of the table
            catalog: the name of the catalog in Unity.
                Defaults to None.
            database: the name of the database.
                (also referred to as schema). Defaults to "default".
            write_mode: the mode to write the data into the table. If not
                present, the data set is read-only.
                Options are:["overwrite", "append", "upsert"].
                "upsert" mode requires primary_key field to be populated.
                Defaults to None.
            dataframe_type: "pandas" or "spark" dataframe.
                Defaults to "spark".
            schema: the schema of the table in JSON form.
                Dataframes will be truncated to match the schema if provided.
                Used by the hooks to create the table if the schema is provided
                Defaults to None.
            partition_columns: the columns to use for partitioning the table.
                Used by the hooks. Defaults to None.
            owner_group: if table access control is enabled in your workspace,
                specifying owner_group will transfer ownership of the table and database to
                this owner. All databases should have the same owner_group. Defaults to None.
            metadata: Any arbitrary metadata.
                This is ignored by Kedro, but may be consumed by users or external plugins.
        Raises:
            DatasetError: Invalid configuration supplied (through BaseTable validation).
        """

        self._table = self._create_table(
            table=table,
            catalog=catalog,
            database=database,
            write_mode=write_mode,
            dataframe_type=dataframe_type,
            schema=schema,
            partition_columns=partition_columns,
            owner_group=owner_group,
            **kwargs,
        )

        self.metadata = metadata
        self.kwargs = kwargs

        super().__init__(
            filepath=None,  # type: ignore[arg-type]
            version=kwargs.get("version"),
            exists_function=self._exists,  # type: ignore[arg-type]
        )

    def _create_table(self, **kwargs: Any) -> BaseTable:
        """Creates a table object and assign it to the _table attribute.

        Args:
            **kwargs: Arguments to pass to the table object.

        Returns:
            BaseTable: the table object.
        """
        raise NotImplementedError
    
    def _load(self) -> DataFrame | pd.DataFrame:
        """Loads the data from the table location defined in the init.
        (spark|pandas dataframe)

        Returns:
            Union[DataFrame, pd.DataFrame]: Returns a dataframe
                in the format defined in the init
        """
        data = _get_spark().table(self._table.full_table_location())
        if self._table.dataframe_type == "pandas":
            data = data.toPandas()
        return data
    
    def _save(self, data: DataFrame | pd.DataFrame) -> None:
        """Saves the data based on the write_mode and dataframe_type in the init.
        If write_mode is pandas, Spark dataframe is created first.
        If schema is provided, data is matched to schema before saving
        (columns will be sorted and truncated).

        Args:
            data (Any): Spark or pandas dataframe to save to the table location
        """
        if self._table.write_mode is None:
            raise DatasetError(
                "'save' can not be used in read-only mode. "
                f"Change 'write_mode' value to {', '.join(self._table._VALID_WRITE_MODES)}"
            )
        # filter columns specified in schema and match their ordering
        schema = self._table.schema()
        if schema:
            cols = schema.fieldNames()
            if self._table.dataframe_type == "pandas":
                data = _get_spark().createDataFrame(
                    data.loc[:, cols], schema=self._table.schema()
                )
            else:
                data = data.select(*cols)
        elif self._table.dataframe_type == "pandas":
            data = _get_spark().createDataFrame(data)

        method = getattr(self, f"_save_{self._table.write_mode}", None)

        if method is None:
            raise DatasetError(
                f"Invalid `write_mode` provided: {self._table.write_mode}. "
                f"`write_mode` must be one of: {self._table._VALID_WRITE_MODES}"
            )
        
        method(data)
    
    def _save_append(self, data: DataFrame) -> None:
        """Saves the data to the table by appending it
        to the location defined in the init.

        Args:
            data (DataFrame): the Spark dataframe to append to the table.
        """
        data.write.format(self._table.format).mode("append").saveAsTable(
            self._table.full_table_location() or ""
        )

    def _save_overwrite(self, data: DataFrame) -> None:
        """Overwrites the data in the table with the data provided.
        (this is the default save mode)

        Args:
            data (DataFrame): the Spark dataframe to overwrite the table with.
        """
        table = data.write.format(self._table.format)
        if self._table.write_mode == "overwrite":
            table = table.mode("overwrite").option(
                "overwriteSchema", "true"
            )
        table.saveAsTable(self._table.full_table_location() or "")

    def _describe(self) -> dict[str, str | list | None]:
        """Returns a description of the instance of the dataset.

        Returns:
            Dict[str, str]: Dict with the details of the dataset
        """
        return {
            "catalog": self._table.catalog,
            "database": self._table.database,
            "table": self._table.table,
            "write_mode": self._table.write_mode,
            "dataframe_type": self._table.dataframe_type,
            "owner_group": self._table.owner_group,
            "partition_columns": self._table.partition_columns,
            **self.kwargs
        }

    def _exists(self) -> bool:
        """Checks to see if the table exists.

        Returns:
            bool: boolean of whether the table defined
            in the dataset instance exists in the Spark session.
        """
        if self._table.catalog:
            try:
                _get_spark().sql(f"USE CATALOG `{self._table.catalog}`")
            except (ParseException, AnalysisException) as exc:
                logger.warning(
                    "catalog %s not found or unity not enabled. Error message: %s",
                    self._table.catalog,
                    exc,
                )
        try:
            return (
                _get_spark()
                .sql(f"SHOW TABLES IN `{self._table.database}`")
                .filter(f"tableName = '{self._table.table}'")
                .count()
                > 0
            )
        except (ParseException, AnalysisException) as exc:
            logger.warning("error occured while trying to find table: %s", exc)
            return False