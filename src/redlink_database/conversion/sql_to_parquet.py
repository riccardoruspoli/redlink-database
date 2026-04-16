import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TextIO

from redlink_database.pipeline.config import (
    TABLE_CONFIG,
    RuntimeConfig,
    RuntimePaths,
    SharedCounter,
)
from redlink_database.pipeline.paths import (
    get_parallel_table_paths,
    get_parquet_part_paths,
    get_parquet_table_paths,
)


def _count_sql_insert_lines(sql_path: str) -> int:
    insert_lines = 0
    with open(
        sql_path, encoding="utf-8", errors="replace", buffering=1024 * 1024
    ) as source:
        for raw_line in source:
            if raw_line.startswith("INSERT INTO "):
                insert_lines += 1
    return insert_lines


def _open_sql_chunk_file(
    chunks_dir: str, table_name: str, chunk_index: int
) -> tuple[TextIO, str]:
    chunk_path = os.path.join(chunks_dir, f"{table_name}_chunk_{chunk_index:04d}.sql")
    return open(chunk_path, "w", encoding="utf-8", newline="\n"), chunk_path


def _rotate_sql_chunk_file(
    current_handle: TextIO | None,
    chunk_paths: list[str],
    chunks_dir: str,
    table_name: str,
    chunk_index: int,
) -> tuple[TextIO, int]:
    if current_handle is not None:
        current_handle.close()
    current_handle, chunk_path = _open_sql_chunk_file(
        chunks_dir, table_name, chunk_index
    )
    chunk_paths.append(chunk_path)
    return current_handle, chunk_index + 1


def _split_sql_insert_file(
    sql_path: str, chunks_dir: str, chunk_insert_lines: int, table_name: str
) -> list[str]:
    os.makedirs(chunks_dir, exist_ok=True)

    chunk_paths: list[str] = []
    insert_lines = 0
    current_chunk_index = 0
    current_chunk_line_count = 0
    current_handle = None

    with open(
        sql_path, encoding="utf-8", errors="replace", buffering=1024 * 1024
    ) as source:
        for raw_line in source:
            if not raw_line.startswith("INSERT INTO "):
                continue

            if current_handle is None or current_chunk_line_count >= chunk_insert_lines:
                current_handle, current_chunk_index = _rotate_sql_chunk_file(
                    current_handle,
                    chunk_paths,
                    chunks_dir,
                    table_name,
                    current_chunk_index,
                )
                current_chunk_line_count = 0

            current_handle.write(raw_line)
            current_chunk_line_count += 1
            insert_lines += 1

            if insert_lines % 1_000 == 0:
                print(f"ℹ️ [{table_name} split] {insert_lines:,} INSERT lines chunked")

    if current_handle is not None:
        current_handle.close()

    return chunk_paths


def _run_sql_chunk_parquet_worker(
    chunk_path: str,
    part_parquet_path: str,
    columns: Sequence[dict],
) -> dict:
    cmd = [
        sys.executable,
        "-m",
        "redlink_database.conversion.sql_to_parquet_worker",
        "--sqlfile",
        chunk_path,
        "--parquetfile",
        part_parquet_path,
        "--columns-json",
        json.dumps(list(columns)),
    ]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    start_time = time.time()
    subprocess.run(
        cmd,
        check=True,
        env=env,
    )
    duration = time.time() - start_time

    return {
        "seconds": duration,
        "chunk_path": chunk_path,
    }


def _get_parallel_conversion_settings(
    config: RuntimeConfig,
) -> tuple[int, int, int]:
    worker_count = config.parallel_workers
    chunk_insert_lines = config.parallel_chunk_insert_lines
    target_chunk_multiplier = max(0, config.parallel_target_chunk_multiplier)
    return worker_count, chunk_insert_lines, target_chunk_multiplier


def _prepare_parallel_parquet_paths(
    table_name: str, paths: RuntimePaths
) -> tuple[str, str, str]:
    parallel_paths = get_parallel_table_paths(table_name, paths.tabular)
    parquet_paths = get_parquet_table_paths(table_name, paths.parquet)
    return (
        parallel_paths.work_root,
        parallel_paths.chunks_dir,
        parquet_paths.parts_dir,
    )


def _should_skip_parallel_parquet_conversion(
    table_name: str, parquet_parts_dir: str, config: RuntimeConfig
) -> bool:
    parquet_part_paths = get_parquet_part_paths(parquet_parts_dir, table_name)
    if parquet_part_paths and not config.force:
        print(
            f"⚠️ Skipping {table_name} parallel conversion, {len(parquet_part_paths)} Parquet part files already available in {parquet_parts_dir}"
        )
        return True
    return False


def _reset_parallel_parquet_directories(
    work_root: str, chunks_dir: str, parquet_parts_dir: str, force: bool
) -> None:
    if os.path.exists(work_root) and force:
        shutil.rmtree(work_root, ignore_errors=True)
    if os.path.exists(chunks_dir):
        shutil.rmtree(chunks_dir, ignore_errors=True)
    if os.path.exists(parquet_parts_dir) and force:
        shutil.rmtree(parquet_parts_dir, ignore_errors=True)

    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(parquet_parts_dir, exist_ok=True)


def _resolve_parallel_chunk_insert_lines(
    sql_path: str,
    table_name: str,
    worker_count: int,
    chunk_insert_lines: int,
    target_chunk_multiplier: int,
) -> tuple[int, int | None]:
    if target_chunk_multiplier <= 0:
        return chunk_insert_lines, None

    total_insert_lines = _count_sql_insert_lines(sql_path)
    if total_insert_lines == 0:
        raise SystemExit(f"❌ No {table_name} INSERT lines found to parallelize")
    target_chunks = max(1, worker_count * target_chunk_multiplier)
    resolved_chunk_insert_lines = max(
        1,
        (total_insert_lines + target_chunks - 1) // target_chunks,
    )
    return resolved_chunk_insert_lines, total_insert_lines


def _print_parallel_conversion_plan(
    table_name: str,
    chunk_count: int,
    worker_count: int,
    chunk_insert_lines: int,
    total_insert_lines: int | None,
) -> None:
    if total_insert_lines is not None:
        print(
            f"ℹ️ [{table_name}] {total_insert_lines:,} INSERT lines, {worker_count} workers, {chunk_count} target chunks, {chunk_insert_lines:,} INSERT lines/chunk"
        )
        return
    print(
        f"ℹ️ [{table_name}] {worker_count} workers, {chunk_count} target chunks, {chunk_insert_lines:,} INSERT lines/chunk"
    )


def _submit_parallel_parquet_workers(
    executor: ThreadPoolExecutor,
    table_name: str,
    chunk_paths: list[str],
    parquet_parts_dir: str,
    table_columns: Sequence[dict],
) -> dict:
    future_to_chunk = {}
    for chunk_index, chunk_path in enumerate(chunk_paths):
        future = executor.submit(
            _run_sql_chunk_parquet_worker,
            chunk_path,
            os.path.join(
                parquet_parts_dir, f"{table_name}_part_{chunk_index:04d}.parquet"
            ),
            table_columns,
        )
        future_to_chunk[future] = chunk_index
    return future_to_chunk


def _print_completed_chunk(
    completed: int, total_chunks: int, seconds: float, chunk_path: str
) -> None:
    print(
        f"✅ [{completed}/{total_chunks}] [{seconds:.2f}s] Completed chunk: {os.path.basename(chunk_path)}"
    )


def _run_parallel_parquet_workers(
    table_name: str,
    chunk_paths: list[str],
    worker_count: int,
    parquet_parts_dir: str,
    table_columns: Sequence[dict],
) -> float:
    worker_start_time = time.time()
    completed_chunks = SharedCounter()
    total_chunks = len(chunk_paths)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_chunk = _submit_parallel_parquet_workers(
            executor,
            table_name,
            chunk_paths,
            parquet_parts_dir,
            table_columns,
        )

        for future in as_completed(future_to_chunk):
            result = future.result()
            completed = completed_chunks.inc()
            _print_completed_chunk(
                completed, total_chunks, result["seconds"], result["chunk_path"]
            )

    return time.time() - worker_start_time


def _finalize_parallel_parquet_conversion(
    table_name: str,
    chunk_paths: list[str],
    chunks_dir: str,
    work_root: str,
    parquet_parts_dir: str,
    split_duration: float,
    worker_duration: float,
    worker_count: int,
) -> None:
    parquet_part_paths = get_parquet_part_paths(parquet_parts_dir, table_name)
    if len(parquet_part_paths) != len(chunk_paths):
        raise SystemExit(
            f"❌ Expected {len(chunk_paths)} Parquet part files for {table_name}, found {len(parquet_part_paths)} in {parquet_parts_dir}"
        )

    if os.path.exists(chunks_dir):
        shutil.rmtree(chunks_dir, ignore_errors=True)
        print(f"🧹 Cleaned temporary SQL chunk files for {table_name}")
    if os.path.isdir(work_root) and not os.listdir(work_root):
        shutil.rmtree(work_root, ignore_errors=True)

    total_duration = split_duration + worker_duration
    print(
        f"✅ Converted {table_name} into {len(parquet_part_paths)} Parquet part files with {worker_count} workers in {total_duration:.2f}s"
    )


def convert_sql_file_to_parquet_parallel(
    sql_path: str, table_name: str, config: RuntimeConfig, paths: RuntimePaths
) -> None:
    """Convert one decompressed SQL dump file into partitioned Parquet parts."""

    table_config = TABLE_CONFIG[table_name]
    worker_count, chunk_insert_lines, target_chunk_multiplier = (
        _get_parallel_conversion_settings(config)
    )

    work_root, chunks_dir, parquet_parts_dir = _prepare_parallel_parquet_paths(
        table_name, paths
    )
    if _should_skip_parallel_parquet_conversion(table_name, parquet_parts_dir, config):
        return

    _reset_parallel_parquet_directories(
        work_root, chunks_dir, parquet_parts_dir, config.force
    )

    chunk_insert_lines, total_insert_lines = _resolve_parallel_chunk_insert_lines(
        sql_path,
        table_name,
        worker_count,
        chunk_insert_lines,
        target_chunk_multiplier,
    )

    split_start_time = time.time()
    chunk_paths = _split_sql_insert_file(
        sql_path, chunks_dir, chunk_insert_lines, table_name
    )
    split_duration = time.time() - split_start_time
    if not chunk_paths:
        raise SystemExit(
            f"❌ No chunk files created for {table_name}; the SQL file may not contain INSERT lines"
        )

    _print_parallel_conversion_plan(
        table_name,
        len(chunk_paths),
        worker_count,
        chunk_insert_lines,
        total_insert_lines,
    )

    worker_duration = _run_parallel_parquet_workers(
        table_name,
        chunk_paths,
        worker_count,
        parquet_parts_dir,
        table_config["columns"],
    )
    _finalize_parallel_parquet_conversion(
        table_name,
        chunk_paths,
        chunks_dir,
        work_root,
        parquet_parts_dir,
        split_duration,
        worker_duration,
        worker_count,
    )
