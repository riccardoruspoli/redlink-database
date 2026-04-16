import json
import os
import shutil
from collections import defaultdict
from collections.abc import Iterator

import duckdb

from redlink_database.pipeline.config import (
    RuntimeConfig,
    SearchExportPaths,
    SearchExportState,
)
from redlink_database.web.text_normalization import (
    get_search_ngram_bucket_key,
    get_search_ngrams,
    normalize_nfc,
)

SQL_SEARCH_PAGES = """
    SELECT page_title, link AS redlink
    FROM redlink
    ORDER BY page_title, link_namespace, link
"""

SQL_SEARCH_STRINGS = """
    SELECT page_title, link
    FROM redlink
    ORDER BY link, page_title
"""

SQL_SEARCH_EXPORT_COUNTS = """
    SELECT
        COUNT(DISTINCT page_title) AS total_page_count,
        COUNT(DISTINCT link) AS total_unique_link_count
    FROM redlink
"""

SEARCH_PAGE_SHARD_SIZE = 1_000
SEARCH_STRING_SHARD_SIZE = 2_000
SEARCH_PROGRESSIVE_STOP_CANDIDATE_STRINGS = 500


def _write_json_shard(
    output_dir: str, records: list[dict[str, object]], shard_index: int
) -> None:
    shard_path = os.path.join(output_dir, f"{shard_index:05d}.json")
    with open(shard_path, "w", encoding="utf-8", newline="") as handle:
        json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))


def iter_cursor_batches(
    cursor: duckdb.DuckDBPyConnection, batch_size: int
) -> Iterator[list[tuple]]:
    """Yield cursor rows in bounded batches to keep export memory usage stable."""

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield rows


def iter_grouped_page_redlinks(
    cursor: duckdb.DuckDBPyConnection, batch_size: int
) -> Iterator[tuple[str, list[str]]]:
    """Group ordered `(page_title, redlink)` rows into per-page batches."""

    current_page = None
    current_redlinks: list[str] = []
    for rows in iter_cursor_batches(cursor, batch_size):
        for page_title, redlink in rows:
            if current_page is None:
                current_page = page_title
            if page_title != current_page:
                yield current_page, current_redlinks
                current_page = page_title
                current_redlinks = []
            current_redlinks.append(redlink)
    if current_page is not None:
        yield current_page, current_redlinks


def _log_search_export_progress(
    phase_name: str, completed: int, total: int, unit: str = "json files"
) -> None:
    progress_step = max(1, round(total / 10))
    if completed % progress_step == 0 or completed == total:
        percentage = min(100, int((completed / total) * 100)) if total > 0 else 100
        print(f"ℹ️ [{phase_name}] {percentage}% {unit} saved")


def _flush_search_entry_shard(state: SearchExportState) -> None:
    if not state.current_string_shard:
        return
    _write_json_shard(
        state.strings_path, state.current_string_shard, state.string_shard_index
    )
    state.current_string_shard = []
    state.string_shard_index += 1
    _log_search_export_progress(
        "search strings",
        state.string_shard_index,
        state.total_string_shards,
        "shards",
    )


def _flush_search_page_shard(state: SearchExportState) -> None:
    if not state.current_page_shard:
        return
    _write_json_shard(
        state.pages_path, state.current_page_shard, state.page_shard_index
    )
    _write_json_shard(
        state.page_refs_path, state.current_page_ref_shard, state.page_shard_index
    )
    state.current_page_shard = []
    state.current_page_ref_shard = []
    state.page_shard_index += 1
    _log_search_export_progress(
        "search pages",
        state.page_shard_index,
        state.total_page_shards,
        "shards",
    )
    _log_search_export_progress(
        "search page refs",
        state.page_shard_index,
        state.total_page_shards,
        "shards",
    )


def _add_search_string_to_index(
    state: SearchExportState, kind: str, value: str, page_ids: list[int]
) -> None:
    state.current_string_shard.append(
        {
            "id": state.string_id,
            "kind": kind,
            "value": value,
            "page_ids": page_ids,
        }
    )
    for trigram in get_search_ngrams(value):
        state.trigram_postings[trigram].append(state.string_id)
        state.ngram_counts[trigram] = state.ngram_counts.get(trigram, 0) + 1
    state.string_id += 1
    state.search_string_count += 1
    if len(state.current_string_shard) >= state.string_shard_size:
        _flush_search_entry_shard(state)


def _flush_search_page_to_shards(
    state: SearchExportState,
    page_title: str | None,
    redlinks: list[str],
    page_id_by_title: dict[str, int],
) -> None:
    if page_title is None:
        return

    normalized_title = normalize_nfc(page_title)
    normalized_redlinks = [normalize_nfc(redlink) for redlink in redlinks]
    state.current_page_shard.append(
        {
            "id": state.page_id,
            "page_title": normalized_title,
            "redlinks": normalized_redlinks,
        }
    )
    state.current_page_ref_shard.append(
        {"id": state.page_id, "page_title": normalized_title}
    )
    page_id_by_title[normalized_title] = state.page_id
    state.page_count += 1
    state.redlink_count += len(normalized_redlinks)
    state.page_id += 1

    if len(state.current_page_shard) >= state.page_shard_size:
        _flush_search_page_shard(state)


def _prepare_search_export_directories(web_search_path: str) -> SearchExportPaths:
    if os.path.isdir(web_search_path):
        shutil.rmtree(web_search_path)
    paths = SearchExportPaths(
        pages_path=os.path.join(web_search_path, "pages"),
        page_refs_path=os.path.join(web_search_path, "page_refs"),
        strings_path=os.path.join(web_search_path, "strings"),
        ngrams_path=os.path.join(web_search_path, "ngrams"),
    )
    for path in (
        paths.pages_path,
        paths.page_refs_path,
        paths.strings_path,
        paths.ngrams_path,
    ):
        os.makedirs(path, exist_ok=True)
    return paths


def _get_search_export_counts(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    return conn.execute(SQL_SEARCH_EXPORT_COUNTS).fetchone()


def _build_search_export_state(
    conn: duckdb.DuckDBPyConnection,
    web_search_path: str,
) -> tuple[SearchExportState, SearchExportPaths, int, int]:
    search_paths = _prepare_search_export_directories(web_search_path)
    total_page_count, total_unique_link_count = _get_search_export_counts(conn)
    total_page_shards = max(
        1, (total_page_count + SEARCH_PAGE_SHARD_SIZE - 1) // SEARCH_PAGE_SHARD_SIZE
    )
    total_search_strings = total_page_count + total_unique_link_count
    total_string_shards = max(
        1,
        (total_search_strings + SEARCH_STRING_SHARD_SIZE - 1)
        // SEARCH_STRING_SHARD_SIZE,
    )
    state = SearchExportState(
        strings_path=search_paths.strings_path,
        pages_path=search_paths.pages_path,
        page_refs_path=search_paths.page_refs_path,
        string_shard_size=SEARCH_STRING_SHARD_SIZE,
        page_shard_size=SEARCH_PAGE_SHARD_SIZE,
        total_string_shards=total_string_shards,
        total_page_shards=total_page_shards,
    )
    return (
        state,
        search_paths,
        total_page_count,
        total_search_strings,
    )


def _export_search_page_shards(
    conn: duckdb.DuckDBPyConnection,
    state: SearchExportState,
    batch_size: int,
    page_id_by_title: dict[str, int],
    total_page_count: int,
) -> None:
    print(
        f"ℹ️ [search pages] preparing {state.total_page_shards} shards from "
        f"{total_page_count:,} pages"
    )
    cursor = conn.execute(SQL_SEARCH_PAGES)
    for page_title, redlinks in iter_grouped_page_redlinks(cursor, batch_size):
        _flush_search_page_to_shards(state, page_title, redlinks, page_id_by_title)
    _flush_search_page_shard(state)


def _add_page_title_search_strings(
    state: SearchExportState,
    page_id_by_title: dict[str, int],
) -> None:
    for page_title, resolved_page_id in page_id_by_title.items():
        _add_search_string_to_index(state, "page_title", page_title, [resolved_page_id])


def _append_redlink_page_id(
    current_link_page_ids: list[int],
    page_id_by_title: dict[str, int],
    page_title: str,
) -> None:
    resolved_page_id = page_id_by_title.get(normalize_nfc(page_title))
    if resolved_page_id is not None:
        current_link_page_ids.append(resolved_page_id)


def _flush_redlink_search_string(
    state: SearchExportState,
    current_link: str | None,
    current_link_page_ids: list[int],
) -> None:
    if current_link is None:
        return
    _add_search_string_to_index(
        state, "redlink", normalize_nfc(current_link), current_link_page_ids
    )


def _consume_search_string_rows(
    state: SearchExportState,
    rows: list[tuple[str, str]],
    page_id_by_title: dict[str, int],
    current_link: str | None,
    current_link_page_ids: list[int],
) -> tuple[str | None, list[int]]:
    for page_title, link in rows:
        if current_link is None:
            current_link = link
        if link != current_link:
            _flush_redlink_search_string(state, current_link, current_link_page_ids)
            current_link = link
            current_link_page_ids = []
        _append_redlink_page_id(current_link_page_ids, page_id_by_title, page_title)
    return current_link, current_link_page_ids


def _export_search_string_shards(
    conn: duckdb.DuckDBPyConnection,
    state: SearchExportState,
    batch_size: int,
    page_id_by_title: dict[str, int],
    total_search_strings: int,
) -> None:
    print(
        f"ℹ️ [search strings] preparing {state.total_string_shards} shards from "
        f"{total_search_strings:,} unique strings"
    )
    _add_page_title_search_strings(state, page_id_by_title)
    cursor = conn.execute(SQL_SEARCH_STRINGS)
    current_link = None
    current_link_page_ids: list[int] = []
    for rows in iter_cursor_batches(cursor, batch_size):
        current_link, current_link_page_ids = _consume_search_string_rows(
            state,
            rows,
            page_id_by_title,
            current_link,
            current_link_page_ids,
        )
    _flush_redlink_search_string(state, current_link, current_link_page_ids)
    _flush_search_entry_shard(state)


def _build_ngram_buckets(
    trigram_postings: dict[str, list[int]],
) -> dict[str, dict[str, list[int]]]:
    ngram_buckets: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for trigram, posting_list in trigram_postings.items():
        ngram_buckets[get_search_ngram_bucket_key(trigram)][trigram] = posting_list
    return ngram_buckets


def _write_search_ngram_buckets(
    ngrams_path: str, trigram_postings: dict[str, list[int]]
) -> None:
    ngram_buckets = _build_ngram_buckets(trigram_postings)
    total_ngram_buckets = len(ngram_buckets)
    print(f"ℹ️ [search ngrams] preparing {total_ngram_buckets} bucket files")
    for bucket_index, (bucket_key, bucket_payload) in enumerate(
        sorted(ngram_buckets.items()), start=1
    ):
        bucket_path = os.path.join(ngrams_path, f"{bucket_key}.json")
        with open(bucket_path, "w", encoding="utf-8", newline="") as handle:
            json.dump(bucket_payload, handle, ensure_ascii=False, separators=(",", ":"))
        _log_search_export_progress(
            "search ngrams", bucket_index, total_ngram_buckets, "bucket files"
        )


def _write_search_ngram_counts(
    web_search_path: str,
    state: SearchExportState,
) -> None:
    ngram_counts_path = os.path.join(web_search_path, "ngram_counts.json")
    with open(ngram_counts_path, "w", encoding="utf-8", newline="") as handle:
        json.dump(state.ngram_counts, handle, ensure_ascii=False, separators=(",", ":"))


def export_search_index(
    conn: duckdb.DuckDBPyConnection,
    web_search_path: str,
    config: RuntimeConfig,
) -> None:
    """Export the complete static search index under `web/data/search/`."""

    batch_size = config.web_batch_size
    page_id_by_title: dict[str, int] = {}
    state, search_paths, total_page_count, total_search_strings = (
        _build_search_export_state(conn, web_search_path)
    )
    _export_search_page_shards(
        conn, state, batch_size, page_id_by_title, total_page_count
    )
    _export_search_string_shards(
        conn, state, batch_size, page_id_by_title, total_search_strings
    )
    _write_search_ngram_buckets(search_paths.ngrams_path, state.trigram_postings)
    _write_search_ngram_counts(web_search_path, state)
