import argparse
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TextIO

ACTIVE_TABLES = ("page", "pagelinks", "linktarget", "categorylinks")
CONTENT_NAMESPACES = (0, 14)

TABLE_CONFIG = {
    "page": {
        "pattern": "{base_path}-\\d{{8}}-page\\.sql$",
        "namespace_column": "page_namespace",
        "columns": [
            {"name": "page_id", "duckdb_type": "BIGINT", "index": 0},
            {"name": "page_namespace", "duckdb_type": "INTEGER", "index": 1},
            {"name": "page_title", "duckdb_type": "VARCHAR", "index": 2},
        ],
        "progress_every": 250_000,
    },
    "pagelinks": {
        "pattern": "{base_path}-\\d{{8}}-pagelinks\\.sql$",
        "namespace_column": "pl_from_namespace",
        "columns": [
            {"name": "pl_from", "duckdb_type": "BIGINT", "index": 0},
            {"name": "pl_from_namespace", "duckdb_type": "INTEGER", "index": 1},
            {"name": "pl_target_id", "duckdb_type": "BIGINT", "index": 2},
        ],
        "progress_every": 1_000_000,
    },
    "linktarget": {
        "pattern": "{base_path}-\\d{{8}}-linktarget\\.sql$",
        "namespace_column": "lt_namespace",
        "columns": [
            {"name": "lt_id", "duckdb_type": "BIGINT", "index": 0},
            {"name": "lt_namespace", "duckdb_type": "INTEGER", "index": 1},
            {"name": "lt_title", "duckdb_type": "VARCHAR", "index": 2},
        ],
        "progress_every": 500_000,
    },
    "categorylinks": {
        "pattern": "{base_path}-\\d{{8}}-categorylinks\\.sql$",
        "columns": [
            {"name": "cl_from", "duckdb_type": "BIGINT", "index": 0},
            {"name": "cl_target_id", "duckdb_type": "BIGINT", "index": 6},
        ],
        "progress_every": 1_000_000,
    },
}


@dataclass
class SharedCounter:
    """Thread-safe integer counter for progress reporting across workers."""

    value: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self) -> int:
        with self.lock:
            self.value += 1
            return self.value


@dataclass
class GroupedRedlinkExportState:
    """Mutable state for grouped redlink JSON export."""

    web_data_path: str
    handles: dict[str, TextIO] = field(default_factory=dict)
    first_record: dict[str, bool] = field(default_factory=dict)
    page_count: int = 0
    redlink_count: int = 0


@dataclass
class SearchExportState:
    """Mutable state for search-index export across page and string shards."""

    strings_path: str
    pages_path: str
    page_refs_path: str
    string_shard_size: int
    page_shard_size: int
    total_string_shards: int
    total_page_shards: int
    trigram_postings: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    ngram_counts: dict[str, int] = field(default_factory=dict)
    current_page_shard: list[dict[str, object]] = field(default_factory=list)
    current_page_ref_shard: list[dict[str, object]] = field(default_factory=list)
    current_string_shard: list[dict[str, object]] = field(default_factory=list)
    page_shard_index: int = 0
    string_shard_index: int = 0
    page_id: int = 0
    string_id: int = 0
    page_count: int = 0
    redlink_count: int = 0
    search_string_count: int = 0


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem layout for one wiki dump version."""

    compressed: str
    decompressed: str
    tabular: str
    parquet: str
    web: str
    web_data: str
    web_search: str
    web_style: str
    web_script: str
    duckdb: str


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated runtime options used across all pipeline phases."""

    download: bool
    decompress: bool
    import_data: bool
    web: bool
    force: bool
    language: str
    data_root: str
    duckdb_memory_limit: str
    duckdb_threads: int
    web_batch_size: int
    decompress_workers: int
    parallel_workers: int
    parallel_chunk_insert_lines: int
    parallel_target_chunk_multiplier: int

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> RuntimeConfig:
        """Build runtime config from parsed CLI arguments."""

        return cls(
            download=args.download,
            decompress=args.decompress,
            import_data=getattr(args, "import"),
            web=args.web,
            force=args.force,
            language=args.language,
            data_root=args.data_root,
            duckdb_memory_limit=args.duckdb_memory_limit,
            duckdb_threads=args.duckdb_threads,
            web_batch_size=args.web_batch_size,
            decompress_workers=args.decompress_workers,
            parallel_workers=args.parallel_workers,
            parallel_chunk_insert_lines=args.parallel_chunk_insert_lines,
            parallel_target_chunk_multiplier=args.parallel_target_chunk_multiplier,
        )


@dataclass(frozen=True)
class SearchExportPaths:
    """Output directories for the generated static search index."""

    pages_path: str
    page_refs_path: str
    strings_path: str
    ngrams_path: str


@dataclass(frozen=True)
class ParallelTablePaths:
    """Temporary working directories for SQL chunking and Parquet parts."""

    work_root: str
    chunks_dir: str
    parts_dir: str


@dataclass(frozen=True)
class ParquetTablePaths:
    """Persistent Parquet output directories for one source table."""

    table_root: str
    parts_dir: str


@dataclass
class DumpContext:
    """Resolved remote/local dump metadata and filesystem paths for one run."""

    language: str
    base_path: str
    target_dump_link: str
    remote_dump_version: str
    paths: RuntimePaths


@dataclass(frozen=True)
class TemplateRenderJob:
    """Renderable HTML template plus its output filename and context."""

    template_name: str
    output_filename: str
    context: dict[str, object]
