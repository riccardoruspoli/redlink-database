import os
from collections.abc import Sequence

from natsort import natsorted

from redlink_database.pipeline.config import (
    ParallelTablePaths,
    ParquetTablePaths,
    RuntimePaths,
)


def create_dump_folders(folders: Sequence[str]) -> None:
    """Create the runtime directories needed for the current dump run."""

    for folder in folders:
        os.makedirs(folder, exist_ok=True)


def get_files_from_directory(directory: str) -> list[str]:
    """Return regular files in one directory using natural sort order."""

    files = [
        os.path.join(directory, filename)
        for filename in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, filename))
    ]
    return natsorted(files)


def build_paths(
    data_root: str,
    base_path: str,
    remote_dump_version: str,
) -> RuntimePaths:
    """Build the canonical filesystem layout for one wiki dump version."""

    dump_root = os.path.join(data_root, base_path, remote_dump_version)
    web_path = os.path.join(dump_root, "web")
    return RuntimePaths(
        compressed=os.path.join(dump_root, "compressed"),
        decompressed=os.path.join(dump_root, "decompressed"),
        tabular=os.path.join(dump_root, "tabular"),
        parquet=os.path.join(dump_root, "parquet"),
        web=web_path,
        web_data=os.path.join(web_path, "data"),
        web_search=os.path.join(web_path, "data", "search"),
        web_style=os.path.join(web_path, "style"),
        web_script=os.path.join(web_path, "script"),
        duckdb=os.path.join(dump_root, f"{base_path}.duckdb"),
    )


def get_parallel_table_paths(table_name: str, tabular_path: str) -> ParallelTablePaths:
    """Return temporary chunk and part directories for parallel SQL conversion."""

    work_root = os.path.join(tabular_path, f"_{table_name}_parallel")
    return ParallelTablePaths(
        work_root=work_root,
        chunks_dir=os.path.join(work_root, "chunks"),
        parts_dir=os.path.join(work_root, "parts"),
    )


def get_parquet_table_paths(table_name: str, parquet_path: str) -> ParquetTablePaths:
    """Return the persistent Parquet output directories for one source table."""

    table_root = os.path.join(parquet_path, table_name)
    return ParquetTablePaths(
        table_root=table_root,
        parts_dir=os.path.join(table_root, "parts"),
    )


def get_parquet_part_paths(parts_dir: str, table_name: str) -> list[str]:
    """Return sorted Parquet part files for one table if they exist."""

    if not os.path.isdir(parts_dir):
        return []
    prefix = f"{table_name}_part_"
    return [
        path
        for path in get_files_from_directory(parts_dir)
        if os.path.basename(path).startswith(prefix) and path.endswith(".parquet")
    ]
