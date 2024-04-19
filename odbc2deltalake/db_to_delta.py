from dataclasses import dataclass
import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping, Sequence, TypeVar, cast
import sqlglot as sg
from .utils import concat_seq
from odbc2deltalake.destination.destination import (
    Destination,
)
from odbc2deltalake.reader import DataSourceReader
from .query import sql_quote_name
from .metadata import (
    get_primary_keys,
    get_columns,
    table_name_type,
    InformationSchemaColInfo,
)
import json
import time
import sqlglot.expressions as ex
from .sql_glot_utils import table_from_tuple, union, count_limit_one
import logging
import pydantic
from .delta_logger import DeltaLogger
from .write_init import (
    WriteConfig,
    WriteConfigAndInfos,
    is_pydantic_2,
    IS_DELETED_COL_INFO,
    IS_DELETED_COL_NAME,
    IS_FULL_LOAD_COL_INFO,
    IS_FULL_LOAD_COL_NAME,
    IS_DELETED_COL_NAME,
    VALID_FROM_COL_NAME,
    VALID_FROM_COL_INFO,
    DBDeltaPathConfigs,
)

T = TypeVar("T")


def _not_none(v: T | None) -> T:
    if v is None:
        raise ValueError("Value is None")
    return v


def _cast(
    name: str,
    data_type: str,
    *,
    table_alias: str | None = None,
    type_map: Mapping[str, ex.DATA_TYPE] | None = None,
):
    mapped_type = type_map.get(data_type) if type_map else None
    if mapped_type:
        return ex.cast(ex.column(name, table_alias, quoted=True), mapped_type)
    return ex.column(name, table_alias, quoted=True)


valid_from_expr = ex.cast(
    ex.func("GETUTCDATE", dialect="tsql"), ex.DataType(this="datetime2(6)")
).as_(VALID_FROM_COL_NAME)


def _get_cols_select(
    cols: Sequence[InformationSchemaColInfo],
    *,
    is_deleted: bool | None = None,
    is_full: bool | None = None,
    with_valid_from: bool = False,
    table_alias: str | None = None,
    source_uses_compat: bool,
    data_type_map: Mapping[str, ex.DATA_TYPE] | None = None,
    get_target_name: Callable[[InformationSchemaColInfo], str] | None,
) -> Sequence[ex.Expression]:
    if get_target_name is None:
        get_target_name = lambda c: c.column_name
    return (
        [
            _cast(
                get_target_name(c) if source_uses_compat else c.column_name,
                c.data_type,
                table_alias=table_alias,
                type_map=data_type_map,
            ).as_(get_target_name(c), quoted=True)
            for c in cols
        ]
        + ([valid_from_expr] if with_valid_from else [])
        + (
            [ex.cast(ex.convert(int(is_deleted)), "bit").as_(IS_DELETED_COL_NAME)]
            if is_deleted is not None
            else []
        )
        + (
            [ex.cast(ex.convert(int(is_full)), "bit").as_(IS_FULL_LOAD_COL_NAME)]
            if is_full is not None
            else []
        )
    )


def _vacuum(source: DataSourceReader, dest: Destination):
    if source.local_delta_table_exists(dest):
        source.get_local_delta_ops(dest).vacuum()


def exec_write_db_to_delta(infos: WriteConfigAndInfos):
    write_config = infos.write_config
    cols = infos.col_infos
    pk_cols = infos.pk_cols
    destination = infos.destination
    source = infos.source
    table = infos.table
    delta_path = destination / "delta"
    dest_logger = infos.logger
    delta_col = infos.delta_col
    (destination / "meta").mkdir()
    (destination / "meta/schema.json").upload_str(
        json.dumps(
            [c.model_dump() if is_pydantic_2 else c.dict() for c in cols], indent=4
        )
    )
    if source.local_delta_table_exists(
        destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION
    ):
        try:
            last_version_pk = source.get_local_delta_ops(
                destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION
            ).version()
        except Exception as e:
            import traceback

            dest_logger.warning(
                f"Could not get last version: {e}",
                error_trackback=traceback.format_exc(),
            )
            last_version_pk = None
    else:
        last_version_pk = None
    lock_file_path = destination / "meta/lock.txt"

    try:
        if (
            lock_file_path.exists()
            and (
                datetime.now(tz=timezone.utc) - lock_file_path.modified_time()
            ).total_seconds()
            > 60 * 60
        ):
            lock_file_path.remove()
        lock_file_path.upload_str("")

        if (
            not source.local_delta_table_exists(delta_path)
            or write_config.load_mode == "overwrite"
        ):
            delta_path.mkdir()
            dest_logger.info(f"{table}: Start Full Load")
            do_full_load(infos=infos, mode="overwrite")
        elif write_config.load_mode == "append_inserts":
            if delta_col is None and len(pk_cols) == 1 and pk_cols[0].is_identity:
                delta_col = pk_cols[0]  # identity columns are usually increasing
            assert (
                delta_col is not None
            ), "Must provide delta column for append_inserts load"
            do_append_inserts_load(infos)
        else:
            if (
                delta_col is None
                or len(pk_cols) == 0
                or write_config.load_mode == "force_full"
            ):
                do_full_load(
                    infos=infos,
                    mode="append",
                )
            else:
                do_delta_load(
                    infos=infos,
                    simple=write_config.load_mode == "simple_delta",
                )
        lock_file_path.remove()
        _vacuum(
            source, destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION
        )
        _vacuum(source, destination / "delta_load" / DBDeltaPathConfigs.DELTA_1_NAME)
        _vacuum(source, destination / "delta_load" / DBDeltaPathConfigs.DELTA_2_NAME)
        _vacuum(source, destination / "delta_load" / DBDeltaPathConfigs.PRIMARY_KEYS_TS)
    except Exception as e:
        # restore files
        if last_version_pk is not None:
            o = source.get_local_delta_ops(
                destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION
            )
            if o.version() > last_version_pk:
                o.restore(last_version_pk)
        import traceback

        dest_logger.error(
            "Error during load: {e}", error_trackback=traceback.format_exc()
        )
        raise e
    finally:
        if lock_file_path.exists():
            lock_file_path.remove()
        dest_logger.flush()


def create_replace_view(
    reader: DataSourceReader,
    name: str,
    base_destination: Destination,
    *,
    version: int | None = None,
):
    reader.local_register_update_view(
        base_destination / f"delta_load/{name}", name, version=version
    )


def write_latest_pk(
    reader: DataSourceReader,
    destination: Destination,
    pks: Sequence[InformationSchemaColInfo],
    delta_col: InformationSchemaColInfo,
    write_config: WriteConfig,
):
    reader.local_register_update_view(
        destination / f"delta_load/{DBDeltaPathConfigs.DELTA_1_NAME}",
        DBDeltaPathConfigs.DELTA_1_NAME,
    )

    reader.local_register_update_view(
        destination / f"delta_load/{DBDeltaPathConfigs.DELTA_2_NAME}",
        DBDeltaPathConfigs.DELTA_2_NAME,
    )
    reader.local_register_update_view(
        destination / f"delta_load/{DBDeltaPathConfigs.PRIMARY_KEYS_TS}",
        DBDeltaPathConfigs.PRIMARY_KEYS_TS,
    )

    latest_pk_query = union(
        [
            ex.select(
                *_get_cols_select(
                    cols=concat_seq(pks, [delta_col]),
                    table_alias="au",
                    source_uses_compat=True,
                    get_target_name=write_config.get_target_name,
                )
            ).from_(table_from_tuple("delta_2", alias="au")),
            (
                ex.select(
                    *_get_cols_select(
                        cols=concat_seq(pks, [delta_col]),
                        table_alias="d1",
                        source_uses_compat=True,
                        get_target_name=write_config.get_target_name,
                    )
                )
                .from_(ex.table_(DBDeltaPathConfigs.DELTA_1_NAME, alias="d1"))
                .join(
                    ex.table_("delta_2", alias="au2"),
                    ex.and_(
                        *[
                            ex.column(
                                write_config.get_target_name(c), "d1", quoted=True
                            ).eq(
                                ex.column(
                                    write_config.get_target_name(c), "au2", quoted=True
                                )
                            )
                            for c in pks
                        ]
                    ),
                    join_type="anti",
                )
            ),
            (
                ex.select(
                    *_get_cols_select(
                        cols=concat_seq(pks, [delta_col]),
                        table_alias="cpk",
                        source_uses_compat=True,
                        get_target_name=write_config.get_target_name,
                    )
                )
                .from_(ex.table_(DBDeltaPathConfigs.PRIMARY_KEYS_TS, alias="cpk"))
                .join(
                    ex.table_("delta_2", alias="au3"),
                    ex.and_(
                        *[
                            ex.column(
                                write_config.get_target_name(c), "cpk", quoted=True
                            ).eq(
                                ex.column(
                                    write_config.get_target_name(c), "au3", quoted=True
                                )
                            )
                            for c in pks
                        ]
                    ),
                    join_type="anti",
                )
                .join(
                    ex.table_(DBDeltaPathConfigs.DELTA_1_NAME, alias="au4"),
                    ex.and_(
                        *[
                            ex.column(
                                write_config.get_target_name(c), "cpk", quoted=True
                            ).eq(
                                ex.column(
                                    write_config.get_target_name(c), "au4", quoted=True
                                )
                            )
                            for c in pks
                        ]
                    ),
                    join_type="anti",
                )
            ),
        ],
        distinct=False,
    )
    reader.local_execute_sql_to_delta(
        latest_pk_query,
        destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION,
        mode="overwrite",
    )


def _temp_table(table: table_name_type):
    if isinstance(table, str):
        return "temp_" + table
    return "temp_" + "_".join(table)


def do_delta_load(
    infos: WriteConfigAndInfos,
    simple=False,  # a simple delta load assumes that there are no deletes and no additional updates (eg, when soft-delete is implemented in source properly)
):
    destination = infos.destination
    logger = infos.logger
    delta_col = infos.delta_col
    write_config = infos.write_config
    assert delta_col is not None, "Must have a delta_col for delta loads"
    reader = infos.source
    table = infos.table
    last_pk_path = (
        destination / f"delta_load/{DBDeltaPathConfigs.LATEST_PK_VERSION}"
        if not simple
        else None
    )
    logger.info(
        f"{table}: Start { 'SIMPLE ' if simple else '' }Delta Load with Delta Column {delta_col.column_name} and pks: {', '.join((c.column_name for c in infos.pk_cols))}"
    )

    if last_pk_path and not reader.local_delta_table_exists(
        last_pk_path
    ):  # or do a full load?
        logger.warning(f"{table}: Primary keys missing, try to restore")
        try:
            from .write_utils.restore_pk import restore_last_pk

            restore_success = restore_last_pk(infos=infos)
        except Exception as e:
            logger.warning(f"{table}: Could not restore primary keys: {e}")
            restore_success = False
        if not restore_success:
            logger.warning(f"{table}: No primary keys found, do a full load")
            do_full_load(infos=infos, mode="append")
            return
    old_pk_version = (
        reader.get_local_delta_ops(
            destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION
        ).version()
        if not simple
        else None
    )
    delta_path = destination / "delta"
    delta_load_value = _get_latest_delta_value(
        reader, delta_path, table, delta_col, write_config
    )

    if delta_load_value is None:
        logger.warning(f"{table}: No delta load value, do a full load")
        do_full_load(
            infos=infos,
            mode="append",
        )
        return
    logger.info(
        f"{table}: Start delta step 1, get primary keys and timestamps. MAX({delta_col.column_name}): {delta_load_value}"
    )
    if not simple:
        _retrieve_primary_key_data(
            reader=reader,
            table=table,
            delta_col=delta_col,
            pk_cols=infos.pk_cols,
            destination=destination,
            write_config=write_config,
        )

    criterion = _cast(
        delta_col.column_name,
        delta_col.data_type,
        table_alias="t",
        type_map=write_config.data_type_map,
    ) > ex.convert(delta_load_value)
    logger.info(f"{table}: Start delta step 2, load updates by timestamp")
    upds_sql = _get_update_sql(
        cols=infos.col_infos,
        criterion=criterion,
        table=table,
        write_config=write_config,
    )
    logger.info("execute sql", load="delta", sub_load="delta_1", sql=upds_sql)
    _load_updates_to_delta(
        logger,
        reader,
        sql=upds_sql,
        delta_path=delta_path,
        delta_name="delta_1",
        write_config=write_config,
    )
    if not simple:
        assert old_pk_version is not None
        _handle_additional_updates(
            logger,
            reader=reader,
            table=table,
            delta_path=delta_path,
            pk_cols=infos.pk_cols,
            delta_col=delta_col,
            cols=infos.col_infos,
            write_config=write_config,
            old_pk_version=old_pk_version,
        )
        reader.local_register_update_view(delta_path, _temp_table(table))

        logger.info(f"{table}: Start delta step 3.5, write meta for next delta load")

        write_latest_pk(
            reader, destination, infos.pk_cols, delta_col, write_config=write_config
        )

        logger.info(f"{table}: Start delta step 4.5, write deletes")
        do_deletes(
            reader=infos.source,
            destination=infos.destination,
            cols=infos.col_infos,
            pk_cols=infos.pk_cols,
            old_pk_version=old_pk_version,
            write_config=infos.write_config,
        )
        logger.info(f"{table}: Done delta load")
    else:
        if (destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION).exists():
            (destination / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION).remove(
                True
            )


def do_append_inserts_load(infos: WriteConfigAndInfos):
    logger = infos.logger
    write_config = infos.write_config
    assert infos.delta_col is not None, "must have a delta col"
    logger.info(
        f"{infos.table}: Start Append Only Load with Delta Column {infos.delta_col.column_name}"
    )
    delta_path = infos.destination / "delta"
    delta_load_value = _get_latest_delta_value(
        infos.source, delta_path, infos.table, infos.delta_col, infos.write_config
    )

    criterion = (
        _cast(
            infos.delta_col.column_name,
            infos.delta_col.data_type,
            table_alias="t",
            type_map=write_config.data_type_map,
        )
        > ex.convert(delta_load_value)
        if delta_load_value
        else None
    )
    logger.info(f"{infos.table}: Start delta step 2, load updates by timestamp")
    _load_updates_to_delta(
        logger,
        infos.source,
        sql=_get_update_sql(
            cols=infos.col_infos,
            criterion=criterion,
            table=infos.table,
            write_config=write_config,
        ),
        delta_path=delta_path,
        delta_name="delta_1",
        write_config=write_config,
    )

    logger.info(f"{infos.table}: Done Append only load")


def _get_latest_delta_value(
    reader: DataSourceReader,
    delta_path: Destination,
    table: table_name_type,
    delta_col: InformationSchemaColInfo,
    write_config: WriteConfig,
):
    reader.local_register_update_view(delta_path, _temp_table(table))
    return reader.local_execute_sql_to_py(
        sg.from_(ex.to_identifier(_temp_table(table))).select(
            ex.func(
                "MAX",
                _cast(
                    write_config.get_target_name(delta_col),
                    delta_col.data_type,
                ),
            ).as_("max_ts")
        )
    )[0]["max_ts"]


def do_deletes(
    reader: DataSourceReader,
    destination: Destination,
    # delta_table: DeltaTable,
    cols: Sequence[InformationSchemaColInfo],
    pk_cols: Sequence[InformationSchemaColInfo],
    old_pk_version: int,
    write_config: WriteConfig,
):
    reader.local_register_update_view(
        destination / f"delta_load/{ DBDeltaPathConfigs.LATEST_PK_VERSION}",
        DBDeltaPathConfigs.LATEST_PK_VERSION,
    )
    LAST_PK_VERSION = "LAST_PK_VERSION"
    reader.local_register_update_view(
        destination / f"delta_load/{ DBDeltaPathConfigs.LATEST_PK_VERSION}",
        LAST_PK_VERSION,
        version=old_pk_version,
    )
    delete_query = ex.except_(
        left=ex.select(
            *_get_cols_select(
                pk_cols,
                table_alias="lpk",
                source_uses_compat=True,
                get_target_name=write_config.get_target_name,
            )
        ).from_(table_from_tuple(LAST_PK_VERSION, alias="lpk")),
        right=ex.select(
            *_get_cols_select(
                pk_cols,
                table_alias="cpk",
                source_uses_compat=True,
                get_target_name=write_config.get_target_name,
            )
        ).from_(table_from_tuple(DBDeltaPathConfigs.LATEST_PK_VERSION, alias="cpk")),
    )

    non_pk_cols = [c for c in cols if c not in pk_cols]
    non_pk_select = [
        ex.Null().as_(write_config.get_target_name(c), quoted=True) for c in non_pk_cols
    ]
    deletes_with_schema = union(
        [
            ex.select(
                *_get_cols_select(
                    pk_cols,
                    table_alias="d1",
                    source_uses_compat=True,
                    get_target_name=write_config.get_target_name,
                )
            )
            .select(
                *_get_cols_select(
                    non_pk_cols,
                    table_alias="d1",
                    source_uses_compat=True,
                    get_target_name=write_config.get_target_name,
                ),
                append=True,
            )
            .select(
                ex.AtTimeZone(
                    this=ex.CurrentTimestamp(),
                    zone=ex.Literal(this="UTC", is_string=True),
                ).as_(VALID_FROM_COL_NAME),
                ex.convert(True).as_(IS_DELETED_COL_NAME),
                ex.convert(False).as_(IS_FULL_LOAD_COL_NAME),
            )
            .from_(table_from_tuple("delta_1", alias="d1"))
            .where("1=0"),  # only used to get correct datatypes
            ex.select(
                ex.Column(this=ex.Star(), table=ex.Identifier(this="d", quoted=False))
            )
            .select(*non_pk_select, append=True)
            .select(
                ex.AtTimeZone(
                    this=ex.CurrentTimestamp(),
                    zone=ex.Literal(this="UTC", is_string=True),
                ).as_(VALID_FROM_COL_NAME),
                append=True,
            )
            .select(ex.convert(True).as_(IS_DELETED_COL_NAME), append=True)
            .select(ex.convert(False).as_(IS_FULL_LOAD_COL_NAME), append=True)
            .from_(table_from_tuple("deletes", alias="d")),
        ],
        distinct=False,
    ).with_("deletes", as_=delete_query)
    reader.local_register_view(deletes_with_schema, "deletes_with_schema")
    has_deletes = (
        reader.local_execute_sql_to_py(count_limit_one("deletes_with_schema"))[0]["cnt"]
        > 0
    )
    if has_deletes:
        reader.local_execute_sql_to_delta(
            sg.from_("deletes_with_schema").select("*"),
            destination / "delta",
            mode="append",
        )


def _retrieve_primary_key_data(
    reader: DataSourceReader,
    table: table_name_type,
    delta_col: InformationSchemaColInfo,
    pk_cols: Sequence[InformationSchemaColInfo],
    destination: Destination,
    write_config: WriteConfig,
):
    pk_ts_col_select = ex.select(
        *_get_cols_select(
            is_full=None,
            is_deleted=None,
            cols=concat_seq(pk_cols, [delta_col]),
            with_valid_from=False,
            data_type_map=write_config.data_type_map,
            source_uses_compat=False,
            get_target_name=write_config.get_target_name,
        )
    ).from_(table_from_tuple(table))
    pk_ts_reader_sql = pk_ts_col_select.sql(write_config.dialect)

    pk_path = destination / f"delta_load/{DBDeltaPathConfigs.PRIMARY_KEYS_TS}"

    reader.source_write_sql_to_delta(
        sql=pk_ts_reader_sql, delta_path=pk_path, mode="overwrite"
    )
    return pk_path


T = TypeVar("T")


def _list_to_chunks(input: Iterable[T], chunk_size: int):
    chunk: list[T] = list()
    for item in input:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = list()
    if len(chunk) > 0:
        yield chunk


def _handle_additional_updates(
    logger: DeltaLogger,
    reader: DataSourceReader,
    table: table_name_type,
    delta_path: Destination,
    pk_cols: Sequence[InformationSchemaColInfo],
    delta_col: InformationSchemaColInfo,
    cols: Sequence[InformationSchemaColInfo],
    write_config: WriteConfig,
    old_pk_version: int,
):
    """Handles updates that are not logical by their timestamp. This can happen on a restore from backup, for example."""
    folder = delta_path.parent
    pk_ds_cols = concat_seq(pk_cols, [delta_col])
    reader.local_register_update_view(
        folder / f"delta_load/{ DBDeltaPathConfigs.PRIMARY_KEYS_TS}",
        DBDeltaPathConfigs.PRIMARY_KEYS_TS,
    )
    LAST_PK_VERSION = "LAST_PK_VERSION"
    reader.local_register_update_view(
        folder / f"delta_load/{ DBDeltaPathConfigs.LATEST_PK_VERSION}",
        LAST_PK_VERSION,
        version=old_pk_version,
    )

    reader.local_register_view(
        ex.except_(
            left=ex.select(
                *_get_cols_select(
                    cols=pk_ds_cols,
                    table_alias="pk",
                    source_uses_compat=True,
                    get_target_name=write_config.get_target_name,
                )
            ).from_(ex.table_(DBDeltaPathConfigs.PRIMARY_KEYS_TS, alias="pk")),
            right=ex.select(
                *_get_cols_select(
                    cols=pk_ds_cols,
                    table_alias="lpk",
                    source_uses_compat=True,
                    get_target_name=write_config.get_target_name,
                )
            ).from_(table_from_tuple(LAST_PK_VERSION, alias="lpk")),
        ),
        "additional_updates",
    )

    sql_query = ex.except_(
        left=ex.select(
            *_get_cols_select(
                cols=pk_cols,
                table_alias="au",
                source_uses_compat=True,
                get_target_name=write_config.get_target_name,
            )
        ).from_(ex.table_("additional_updates", alias="au")),
        right=ex.select(
            *_get_cols_select(
                cols=pk_cols,
                table_alias="d1",
                source_uses_compat=True,
                get_target_name=write_config.get_target_name,
            )
        ).from_(table_from_tuple("delta_1", alias="d1")),
    )
    reader.local_register_view(sql_query, "real_additional_updates")
    update_count: int = reader.local_execute_sql_to_py(
        sg.from_("real_additional_updates").select(ex.Count(this=ex.Star()).as_("cnt"))
    )[0]["cnt"]

    from .sql_schema import get_sql_type
    from .query import sql_quote_value

    jsd = reader.local_execute_sql_to_py(
        sg.from_("real_additional_updates").select(
            *[
                ex.column(write_config.get_target_name(c)).as_(
                    "p" + str(i), quoted=False
                )
                for i, c in enumerate(pk_cols)
            ]
        )
    )

    def _collate(c: InformationSchemaColInfo):
        if c.data_type.lower() in [
            "char",
            "varchar",
            "nchar",
            "nvarchar",
            "text",
            "ntext",
        ]:
            return "COLLATE Latin1_General_100_BIN "
        return ""

    delta_2_path = folder / "delta_load/delta_2"

    def full_sql(js: str):
        col_defs = ", ".join(
            [
                f"p{i} {get_sql_type(p.data_type, p.character_maximum_length)}"
                for i, p in enumerate(pk_cols)
            ]
        )

        selects = list(
            _get_cols_select(
                cols,
                is_full=False,
                is_deleted=False,
                with_valid_from=True,
                table_alias="t",
                data_type_map=write_config.data_type_map,
                source_uses_compat=False,
                get_target_name=write_config.get_target_name,
            )
        )
        sql = (
            ex.select(*selects)
            .from_(table_from_tuple(table, alias="t"))
            .sql(write_config.dialect)
        )
        pk_map = ", ".join(
            [
                "p" + str(i) + " as " + sql_quote_name(write_config.get_target_name(c))
                for i, c in enumerate(pk_cols)
            ]
        )
        return f"""{sql}
        inner join (SELECT {pk_map} FROM OPENJSON({sql_quote_value(js)}) with ({col_defs}) ) ttt
             on {' AND '.join([f't.{sql_quote_name(c.column_name)} {_collate(c)} = ttt.{sql_quote_name(write_config.get_target_name(c))}' for c in pk_cols])}
        """

    if update_count == 0:
        reader.source_write_sql_to_delta(full_sql("[]"), delta_2_path, mode="overwrite")
    elif (
        update_count > 1000
    ) or write_config.no_complex_entries_load:  # many updates. get the smallest timestamp and do "normal" delta, even if there are too many records then
        reader.source_write_sql_to_delta(
            full_sql("[]"), delta_2_path, mode="overwrite"
        )  # still need to create delta_2_path
        logger.warning(
            f"{table}: Start delta step 3, load {update_count} strange updates via normal delta load"
        )
        delta_load_value = reader.local_execute_sql_to_py(
            ex.select(
                ex.func(
                    "MIN",
                    ex.column(write_config.get_target_name(delta_col), quoted=True),
                ).as_("min_ts")
            ).from_(ex.table_("additional_updates", alias="rau"))
        )[0]["min_ts"]
        criterion = _cast(
            delta_col.column_name,
            delta_col.data_type,
            table_alias="t",
            type_map=write_config.data_type_map,
        ) > ex.convert(delta_load_value)
        logger.info(f"{table}: Start delta step 2, load updates by timestamp")
        upds_sql = _get_update_sql(
            cols=cols, criterion=criterion, table=table, write_config=write_config
        )
        logger.info(
            "execute sql", load="delta", sub_load="delta_1_additional", sql=upds_sql
        )
        _load_updates_to_delta(
            logger,
            reader,
            sql=upds_sql,
            delta_path=delta_path,
            delta_name="delta_1",
            write_config=write_config,
        )
    else:
        # we don't want to overshoot 8000 chars here because of spark. we estimate how much space in json a record of pk's will take

        char_size_pks = sum(
            [
                5
                + (
                    10
                    if p.data_type
                    in ["bit", "int", "bigint", "tinyint", "bool", "smallint"]
                    else 40
                )
                for p in pk_cols
            ]
        )
        batch_size = max(10, int(7000 / char_size_pks))

        logger.warning(
            f"{table}: Start delta step 3, load {update_count} strange updates via batches of size {batch_size}"
        )
        logger.info(
            "execute sql",
            load="delta",
            sub_load="delta_additional",
            sql=full_sql("[]"),
        )
        first = True
        for chunk in _list_to_chunks(jsd, batch_size):
            sql = full_sql(json.dumps(chunk))
            if (
                len(sql) > 7000
            ):  ## oops, spark will not like this (actually the limit is 8000, but spark might use something on it's own)
                ch_split = len(chunk) // 2
                chunk_1 = chunk[:ch_split]
                chunk_2 = chunk[ch_split:]
                reader.source_write_sql_to_delta(
                    full_sql(json.dumps(chunk_1)),
                    delta_2_path,
                    mode="overwrite" if first else "append",
                )
                reader.source_write_sql_to_delta(
                    full_sql(json.dumps(chunk_2)),
                    delta_2_path,
                    mode="append",
                )
            else:
                reader.source_write_sql_to_delta(
                    sql,
                    delta_2_path,
                    mode="overwrite" if first else "append",
                )
            first = False
        reader.local_register_update_view(delta_2_path, "delta_2")
        reader.local_execute_sql_to_delta(
            sg.from_("delta_2").select(ex.Star()),
            delta_path,
            mode="append",
        )


def _get_update_sql(
    cols: Sequence[InformationSchemaColInfo],
    criterion: str | Sequence[str | ex.Expression] | ex.Expression | None,
    table: table_name_type,
    write_config: WriteConfig,
):
    if isinstance(criterion, ex.Expression):
        criterion = [criterion]
    if isinstance(criterion, ex.Expression):
        criterion = [criterion]
    delta_sql = (
        ex.select(
            *_get_cols_select(
                cols,
                is_full=False,
                is_deleted=False,
                with_valid_from=True,
                table_alias="t",
                data_type_map=write_config.data_type_map,
                source_uses_compat=False,
                get_target_name=write_config.get_target_name,
            )
        )
        .where(
            *(
                criterion
                if criterion is not None and not isinstance(criterion, str)
                else []
            ),
            dialect=write_config.dialect,
        )
        .from_(table_from_tuple(table, alias="t"))
        .sql(write_config.dialect)
    )
    if isinstance(criterion, str):
        delta_sql += " " + criterion
    return delta_sql


def _load_updates_to_delta(
    logger: DeltaLogger,
    reader: DataSourceReader,
    delta_path: Destination,
    sql: str | ex.Query,
    delta_name: str,
    write_config: WriteConfig,
):
    if isinstance(sql, ex.Query):
        sql = sql.sql(write_config.dialect)

    delta_name_path = delta_path.parent / f"delta_load/{delta_name}"
    logger.info("Executing sql", load="delta", sub_load=delta_name, sql=sql)
    reader.source_write_sql_to_delta(sql, delta_name_path, mode="overwrite")
    reader.local_register_update_view(delta_name_path, delta_name)
    count = reader.local_execute_sql_to_py(count_limit_one(delta_name))[0]["cnt"]
    if count == 0:
        return
    reader.local_execute_sql_to_delta(
        sg.from_(delta_name).select(ex.Star()), delta_path, mode="append"
    )


def do_full_load(infos: WriteConfigAndInfos, mode: Literal["overwrite", "append"]):
    logger = infos.logger
    write_config = infos.write_config
    delta_path = infos.destination / "delta"
    reader = infos.source
    logger.info(f"{infos.table}: Start Full Load")
    sql = (
        ex.select(
            *_get_cols_select(
                is_deleted=False,
                is_full=True,
                cols=infos.col_infos,
                with_valid_from=True,
                data_type_map=write_config.data_type_map,
                source_uses_compat=False,
                get_target_name=write_config.get_target_name,
            )
        )
        .from_(table_from_tuple(infos.table))
        .sql(write_config.dialect)
    )
    if reader.local_delta_table_exists(
        delta_path, extended_check=True
    ):  # the extended check checks if there is any column in the table
        reader.local_register_update_view(delta_path, _temp_table(infos.table))
        res = reader.local_execute_sql_to_py(
            sg.from_(ex.to_identifier(_temp_table(infos.table))).select(
                ex.func("max", ex.column(VALID_FROM_COL_NAME)).as_(VALID_FROM_COL_NAME)
            )
        )
        max_valid_from = res[0][VALID_FROM_COL_NAME] if res else None
    else:
        max_valid_from = None
        logger.info("executing sql", sql=sql, load="full")
    reader.source_write_sql_to_delta(sql, delta_path, mode=mode)
    if infos.delta_col is None:
        logger.info(f"{infos.table}: Full Load done")
        return
    logger.info(f"{infos.table}: Full Load done, write meta for delta load")

    reader.local_register_update_view(delta_path, _temp_table(infos.table))
    (delta_path.parent / "delta_load").mkdir()
    query = sg.from_(ex.to_identifier(_temp_table(infos.table))).select(
        *(
            [
                ex.column(write_config.get_target_name(pk), quoted=True)
                for pk in infos.pk_cols
            ]
            + (
                [ex.column(write_config.get_target_name(infos.delta_col), quoted=True)]
                if infos.delta_col
                else []
            )
        )
    )
    if max_valid_from:
        query = query.where(
            ex.column(VALID_FROM_COL_NAME, quoted=True) > ex.convert(max_valid_from)
        )
    reader.local_execute_sql_to_delta(
        query,
        delta_path.parent / "delta_load" / DBDeltaPathConfigs.LATEST_PK_VERSION,
        mode="overwrite",
    )
