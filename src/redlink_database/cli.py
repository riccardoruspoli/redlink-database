import argparse
import os
import re
import time
from collections.abc import Sequence
from typing import Any

import duckdb
import psutil

from redlink_database.conversion.sql_to_parquet import (
    convert_sql_file_to_parquet_parallel,
)
from redlink_database.pipeline.config import (
    ACTIVE_TABLES,
    DumpContext,
    RuntimeConfig,
    RuntimePaths,
)
from redlink_database.pipeline.dumps import (
    decompress_gz_files,
    download_files,
    find_required_sql_files,
    get_files_from_url,
    get_subfolders,
)
from redlink_database.pipeline.paths import (
    build_paths,
    create_dump_folders,
    get_files_from_directory,
    get_parquet_part_paths,
    get_parquet_table_paths,
)
from redlink_database.pipeline.redlink_builder import (
    build_redlink_table,
    init_duckdb_connection,
)
from redlink_database.web.site_build import export_web_data_files, render_web_pages


def _enable_default_steps(args: argparse.Namespace) -> None:
    steps = ["download", "decompress", "import", "web"]
    if all(not getattr(args, step) for step in steps):
        for step in steps:
            setattr(args, step, True)


def _timed_step(step_label: str, action, *args, **kwargs) -> Any:
    start_time = time.time()
    result = action(*args, **kwargs)
    duration = time.time() - start_time
    print(f"🕒 [STEP] {step_label} duration: {duration:.2f} seconds")
    return result


def _get_local_versions(base_root: str) -> list[str]:
    if not os.path.exists(base_root):
        return []
    return [
        entry
        for entry in os.listdir(base_root)
        if os.path.isdir(os.path.join(base_root, entry))
    ]


def _resolve_local_dump_version(local_base_root: str) -> str | None:
    local_versions = _get_local_versions(local_base_root)
    return max(local_versions) if local_versions else None


def _resolve_dump_context(config: RuntimeConfig) -> DumpContext:
    data_root = os.path.abspath(config.data_root)
    language = config.language
    base_path = language + "wiki"
    print(f"ℹ️ Processing dump from {base_path}")

    mirror = f"https://mirror.accum.se/mirror/wikimedia.org/dumps/{base_path}/?C=N;O=A"
    target_dump_link = get_subfolders(mirror)[-1]
    remote_dump_path = re.search(rf"{base_path}/\d{{8}}", target_dump_link).group(0)
    remote_dump_version = remote_dump_path.split("/")[1]
    local_base_root = os.path.join(data_root, base_path)
    local_dump_root = os.path.join(local_base_root, remote_dump_version)
    local_dump_version = _resolve_local_dump_version(local_base_root)

    print(f"ℹ️ Remote dump version: {remote_dump_version} (local: {local_dump_version})")
    if os.path.exists(local_dump_root):
        if config.force:
            print("⚠️ Forcing re-processing of the Wikipedia dump...")
        else:
            raise SystemExit(
                f"⚠️ The local dump ({local_dump_version}) is already the latest available version."
            )

    print(f"ℹ️ Processing latest dump: {remote_dump_version}")
    paths = build_paths(
        data_root,
        base_path,
        remote_dump_version,
    )
    return DumpContext(
        language=language,
        base_path=base_path,
        target_dump_link=target_dump_link,
        remote_dump_version=remote_dump_version,
        paths=paths,
    )


def _create_runtime_directories(paths: RuntimePaths) -> None:
    create_dump_folders(
        [
            paths.compressed,
            paths.decompressed,
            paths.tabular,
            paths.parquet,
            paths.web,
            paths.web_data,
            paths.web_search,
            paths.web_style,
            paths.web_script,
        ]
    )


def _execute_download_step(
    target_dump_link: str, base_path: str, paths: RuntimePaths, config: RuntimeConfig
) -> None:
    files_to_download = get_files_from_url(target_dump_link, base_path)
    print("ℹ️ Starting download...")
    print("ℹ️ Files to download:")
    for file_url in files_to_download:
        print(f"- {file_url}")
    download_files(files_to_download, paths.compressed, config)


def _run_download_step(
    target_dump_link: str, base_path: str, paths: RuntimePaths, config: RuntimeConfig
) -> None:
    _timed_step(
        "Download", _execute_download_step, target_dump_link, base_path, paths, config
    )


def _execute_decompress_step(paths: RuntimePaths, config: RuntimeConfig) -> None:
    compressed_files = get_files_from_directory(paths.compressed)
    print("ℹ️ Starting decompression...")
    print(f"ℹ️ Found {len(compressed_files)} compressed files")
    decompress_gz_files(compressed_files, paths.decompressed, config)


def _run_decompress_step(paths: RuntimePaths, config: RuntimeConfig) -> None:
    _timed_step("Decompression", _execute_decompress_step, paths, config)


def _convert_missing_sql_files_to_parquet(
    paths: RuntimePaths, base_path: str, config: RuntimeConfig
) -> None:
    sql_files = find_required_sql_files(paths.decompressed, base_path)
    print("ℹ️ Found SQL files:")
    for table_name in ACTIVE_TABLES:
        sql_path = sql_files[table_name]
        parquet_parts_dir = get_parquet_table_paths(table_name, paths.parquet).parts_dir
        parquet_part_paths = get_parquet_part_paths(parquet_parts_dir, table_name)
        if not parquet_part_paths or config.force:
            convert_sql_file_to_parquet_parallel(sql_path, table_name, config, paths)


def _run_import_step(
    paths: RuntimePaths, base_path: str, config: RuntimeConfig
) -> None:
    print("ℹ️ Starting data import...")
    _timed_step(
        "Import data", _convert_missing_sql_files_to_parquet, paths, base_path, config
    )


def _execute_web_step(
    duckdb_conn: duckdb.DuckDBPyConnection,
    paths: RuntimePaths,
    config: RuntimeConfig,
    language: str,
    dump_version: str,
) -> None:
    print("ℹ️ Rendering web pages...")
    print("ℹ️ Building redlink table from Parquet sources...")
    build_redlink_table(duckdb_conn, paths)
    export_web_data_files(duckdb_conn, paths, config)
    render_web_pages(paths, config, language, dump_version)
    print("✅ Web pages rendered")


def _run_web_step(
    duckdb_conn: duckdb.DuckDBPyConnection,
    paths: RuntimePaths,
    config: RuntimeConfig,
    language: str,
    dump_version: str,
) -> None:
    _timed_step(
        "Web pages rendering",
        _execute_web_step,
        duckdb_conn,
        paths,
        config,
        language,
        dump_version,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="redlink-database",
        description="A tool to process Wikipedia dumps, extract redlinks, and generate web assets for exploring them",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--download", action="store_true", help="Download the Wikipedia dump"
    )
    parser.add_argument(
        "--decompress", action="store_true", help="Decompress the Wikipedia dump"
    )
    parser.add_argument(
        "--import",
        action="store_true",
        help="Convert projected SQL data into Parquet parts",
    )
    parser.add_argument("--web", action="store_true", help="Render web pages")
    parser.add_argument(
        "--force", action="store_true", help="Force overwrite existing files"
    )
    parser.add_argument(
        "--language",
        type=str,
        default="en",
        help="Wikipedia language code, e.g. en, es, it",
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default=".",
        help="Base directory where wiki dump folders and generated outputs are stored",
    )
    parser.add_argument(
        "--duckdb-memory-limit",
        type=str,
        default="8GB",
        help="DuckDB memory limit, e.g. 8GB",
    )
    parser.add_argument(
        "--duckdb-threads",
        type=int,
        default=min(psutil.cpu_count(logical=False) or 1, 8),
        help="DuckDB execution threads",
    )
    parser.add_argument(
        "--web-batch-size",
        type=int,
        default=100000,
        help="Batch size for streaming JSON export from DuckDB",
    )
    parser.add_argument(
        "--decompress-workers",
        type=int,
        default=2,
        help="Number of parallel workers for gzip decompression",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=psutil.cpu_count(logical=True) or 1,
        help="Number of parallel worker subprocesses for SQL to Parquet conversion",
    )
    parser.add_argument(
        "--parallel-chunk-insert-lines",
        type=int,
        default=100,
        help="Fixed INSERT lines per chunk when adaptive sizing is disabled",
    )
    parser.add_argument(
        "--parallel-target-chunk-multiplier",
        type=int,
        default=3,
        help="Target chunk multiplier relative to parallel workers; set 0 to disable adaptive chunk sizing",
    )

    args = parser.parse_args(argv)
    args.prog = parser.prog
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the configured pipeline steps for the selected wiki dump."""

    args = _parse_args(argv)

    try:
        _enable_default_steps(args)
        config = RuntimeConfig.from_args(args)
        dump_context = _resolve_dump_context(config)
        script_start_time = time.time()
        paths = dump_context.paths
        _create_runtime_directories(paths)

        duckdb_conn = init_duckdb_connection(paths.duckdb, config)
        print(f"ℹ️ DuckDB path: {paths.duckdb}")

        if config.download:
            _run_download_step(
                dump_context.target_dump_link, dump_context.base_path, paths, config
            )

        if config.decompress:
            _run_decompress_step(paths, config)

        if config.import_data:
            _run_import_step(paths, dump_context.base_path, config)

        if config.web:
            _run_web_step(
                duckdb_conn,
                paths,
                config,
                dump_context.language,
                dump_context.remote_dump_version,
            )

        total_duration = time.time() - script_start_time
        print(f"🕒 [TOTAL] Script duration: {total_duration:.2f} seconds")
        return 0
    except KeyboardInterrupt:
        print(f"{args.prog}: interrupted")
        return 130
    except Exception as exc:
        print(f"Unhandled error: {exc}")
        return 1
