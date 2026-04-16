import argparse
import json
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pyarrow as pa
import pyarrow.parquet as pq

from redlink_database.conversion.sql_insert_parser import iter_insert_rows
from redlink_database.pipeline.config import TABLE_CONFIG

PYARROW_TYPE_BY_DUCKDB_TYPE = {
    "BIGINT": pa.int64(),
    "INTEGER": pa.int32(),
    "VARCHAR": pa.string(),
}


def _convert_value(raw_value: str | None, duckdb_type: str) -> int | str | None:
    if raw_value is None:
        return None
    if duckdb_type == "BIGINT":
        return int(raw_value)
    if duckdb_type == "INTEGER":
        return int(raw_value)
    return raw_value


def _flush_batch(
    writer: pq.ParquetWriter,
    schema: pa.Schema,
    buffers: dict[str, list[int | str | None]],
) -> None:
    if not buffers:
        return
    first_key = next(iter(buffers))
    if not buffers[first_key]:
        return
    writer.write_table(pa.Table.from_pydict(buffers, schema=schema))


def _convert_sql_file_to_parquet(
    sql_path: str,
    parquet_path: str,
    columns_json: str | None = None,
    table_name: str | None = None,
    row_group_size: int = 100_000,
    compression: str = "zstd",
) -> int:
    if columns_json is not None:
        projected_columns = json.loads(columns_json)
    elif table_name is not None:
        projected_columns = TABLE_CONFIG[table_name]["columns"]
    else:
        raise SystemExit("❌ Either --columns-json or --table-name is required")
    schema = pa.schema(
        [
            pa.field(
                column["name"],
                PYARROW_TYPE_BY_DUCKDB_TYPE[column["duckdb_type"]],
                nullable=True,
            )
            for column in projected_columns
        ]
    )
    buffers: dict[str, list[int | str | None]] = {
        column["name"]: [] for column in projected_columns
    }
    rows_written = 0
    writer = pq.ParquetWriter(
        parquet_path,
        schema=schema,
        compression=compression,
        use_dictionary=True,
    )

    try:
        for row in iter_insert_rows(sql_path):
            for column in projected_columns:
                raw_value = row[column["index"]] if column["index"] < len(row) else None
                buffers[column["name"]].append(
                    _convert_value(raw_value, column["duckdb_type"])
                )
            rows_written += 1
            if rows_written % row_group_size == 0:
                _flush_batch(writer, schema, buffers)
                buffers = {column["name"]: [] for column in projected_columns}

        _flush_batch(writer, schema, buffers)
    finally:
        writer.close()

    return rows_written


def main() -> None:
    """Parse one SQL chunk and write it as a single Parquet part file."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlfile", required=True)
    parser.add_argument("--parquetfile", required=True)
    parser.add_argument("--columns-json")
    parser.add_argument("--table-name", choices=sorted(TABLE_CONFIG.keys()))
    parser.add_argument(
        "--row-group-size",
        type=int,
        default=100_000,
        help="Approximate number of rows buffered before writing a Parquet row group",
    )
    parser.add_argument(
        "--compression",
        type=str,
        default="zstd",
        help="Parquet compression codec",
    )
    args = parser.parse_args()

    Path(args.parquetfile).parent.mkdir(parents=True, exist_ok=True)
    _convert_sql_file_to_parquet(
        args.sqlfile,
        args.parquetfile,
        columns_json=args.columns_json,
        table_name=args.table_name,
        row_group_size=args.row_group_size,
        compression=args.compression,
    )


if __name__ == "__main__":
    main()
