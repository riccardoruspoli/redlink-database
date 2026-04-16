import os

import duckdb

from redlink_database.pipeline.config import (
    CONTENT_NAMESPACES,
    TABLE_CONFIG,
    RuntimeConfig,
    RuntimePaths,
)
from redlink_database.pipeline.paths import (
    get_parquet_part_paths,
    get_parquet_table_paths,
)

SQL_CREATE_REDLINK = """
    CREATE OR REPLACE TEMP TABLE redlink AS
    WITH candidate_links AS (
        SELECT
            pl.pl_from AS source_page_id,
            lt.lt_namespace AS target_namespace,
            lt.lt_title AS target_title
        FROM pagelinks pl
        JOIN linktarget lt
            ON lt.lt_id = pl.pl_target_id
        WHERE lt.lt_namespace IN (0, 14)

        UNION ALL

        SELECT
            cl.cl_from AS source_page_id,
            14 AS target_namespace,
            lt.lt_title AS target_title
        FROM categorylinks cl
        JOIN linktarget lt
            ON lt.lt_id = cl.cl_target_id
        WHERE lt.lt_namespace = 14
    )
    SELECT DISTINCT
        source_page.page_title,
        candidate.target_namespace AS link_namespace,
        candidate.target_title AS link
    FROM candidate_links candidate
    JOIN page source_page
        ON source_page.page_id = candidate.source_page_id
    ANTI JOIN page existing_page
        ON existing_page.page_namespace = candidate.target_namespace
        AND existing_page.page_title = candidate.target_title
"""


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def init_duckdb_connection(
    duckdb_path: str, config: RuntimeConfig
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB and apply runtime PRAGMAs for the current dump directory."""

    conn = duckdb.connect(duckdb_path)
    temp_directory = os.path.join(os.path.dirname(duckdb_path), "duckdb_tmp")
    os.makedirs(temp_directory, exist_ok=True)

    conn.execute(f"PRAGMA threads={config.duckdb_threads}")
    conn.execute(f"PRAGMA memory_limit='{_sql_quote(str(config.duckdb_memory_limit))}'")
    conn.execute(f"PRAGMA temp_directory='{_sql_quote(temp_directory)}'")
    conn.execute("PRAGMA preserve_insertion_order=false")
    conn.execute("PRAGMA enable_progress_bar=false")
    return conn


def _create_parquet_source_view(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    paths: RuntimePaths,
) -> None:
    table_config = TABLE_CONFIG[table_name]
    column_names = ", ".join(column["name"] for column in table_config["columns"])
    parquet_parts_dir = get_parquet_table_paths(table_name, paths.parquet).parts_dir
    part_paths = get_parquet_part_paths(parquet_parts_dir, table_name)
    if not part_paths:
        raise SystemExit(f"❌ No Parquet part files found for {table_name}")

    namespace_column = table_config.get("namespace_column")
    namespace_filter_sql = ""
    if namespace_column and CONTENT_NAMESPACES:
        allowed_namespaces_sql = ", ".join(
            str(int(namespace)) for namespace in CONTENT_NAMESPACES
        )
        namespace_filter_sql = (
            f"\n        WHERE {namespace_column} IN ({allowed_namespaces_sql})"
        )

    parquet_glob = os.path.join(parquet_parts_dir, f"{table_name}_part_*.parquet")
    conn.execute(f"""
        CREATE OR REPLACE TEMP VIEW {table_name} AS
        SELECT {column_names}
        FROM read_parquet('{_sql_quote(parquet_glob)}'){namespace_filter_sql}
    """)
    print(
        f"✅ Registered temp view {table_name} from {len(part_paths)} Parquet part files"
    )


def _ensure_parquet_source_views(
    conn: duckdb.DuckDBPyConnection,
    paths: RuntimePaths,
) -> None:
    for table_name in TABLE_CONFIG:
        _create_parquet_source_view(conn, table_name, paths)


def _create_redlink_temp_table(conn: duckdb.DuckDBPyConnection) -> None:
    # The ANTI JOIN keeps only missing targets while preserving namespace-aware
    # existence checks, which is the core semantic of the generated redlink set.
    conn.execute(SQL_CREATE_REDLINK)


def build_redlink_table(conn: duckdb.DuckDBPyConnection, paths: RuntimePaths) -> None:
    """Register Parquet-backed temp views and materialize the `redlink` temp table."""

    _ensure_parquet_source_views(conn, paths)
    _create_redlink_temp_table(conn)
