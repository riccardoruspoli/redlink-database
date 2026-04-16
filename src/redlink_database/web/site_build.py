import json
import os
import shutil
import time
from datetime import UTC, datetime
from importlib.resources import files
from typing import TextIO
from xml.sax.saxutils import escape

import duckdb
from jinja2 import Environment, PackageLoader

from redlink_database.pipeline.config import (
    GroupedRedlinkExportState,
    RuntimeConfig,
    RuntimePaths,
    TemplateRenderJob,
)
from redlink_database.pipeline.paths import get_files_from_directory
from redlink_database.web.search_index import (
    SEARCH_PAGE_SHARD_SIZE,
    SEARCH_PROGRESSIVE_STOP_CANDIDATE_STRINGS,
    SEARCH_STRING_SHARD_SIZE,
    export_search_index,
    iter_cursor_batches,
    iter_grouped_page_redlinks,
)
from redlink_database.web.text_normalization import get_initial, normalize_nfc

SQL_GROUPED_REDLINKS_BY_PAGE = """
    SELECT page_title, link AS redlink
    FROM redlink
    ORDER BY page_title, link
"""

SQL_WANTED_LINKS = """
    SELECT link AS {value_alias}, COUNT(*) AS count
    FROM redlink
    WHERE link_namespace = {link_namespace}
    GROUP BY link
    HAVING COUNT(*) >= 10
    ORDER BY count DESC, {value_alias} ASC
"""

GROUPED_EXPORT_PROGRESS_EVERY_PAGES = 100_000
PUBLIC_SITE_BASE_URL = "https://redlink.riccardoruspoli.com"
ROBOTS_TXT_LINES = (
    "User-agent: *",
    "Disallow: /data/",
    "Allow: /",
    "",
    f"Sitemap: {PUBLIC_SITE_BASE_URL}/sitemap.xml",
)


def _write_group_json_record(handle, is_first: bool, payload: dict) -> bool:
    if not is_first:
        handle.write(",")
    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return False


def _get_grouped_redlinks_handle(
    state: GroupedRedlinkExportState, initial: str
) -> TextIO:
    if initial not in state.handles:
        output_path = os.path.join(state.web_data_path, f"{initial}.json")
        handle = open(output_path, "w", encoding="utf-8", newline="")
        handle.write("[")
        state.handles[initial] = handle
        state.first_record[initial] = True
    return state.handles[initial]


def _flush_grouped_redlinks_page(
    state: GroupedRedlinkExportState, page_title: str | None, redlinks: list[str]
) -> None:
    if page_title is None:
        return
    normalized_title = normalize_nfc(page_title)
    normalized_redlinks = [normalize_nfc(redlink) for redlink in redlinks]
    initial = get_initial(normalized_title)
    handle = _get_grouped_redlinks_handle(state, initial)
    state.first_record[initial] = _write_group_json_record(
        handle,
        state.first_record[initial],
        {"page_title": normalized_title, "redlinks": normalized_redlinks},
    )
    state.page_count += 1
    state.redlink_count += len(normalized_redlinks)


def _close_grouped_redlinks_handles(state: GroupedRedlinkExportState) -> None:
    for handle in state.handles.values():
        handle.write("]")
        handle.close()


def _export_grouped_redlinks_json(
    conn: duckdb.DuckDBPyConnection, web_data_path: str, config: RuntimeConfig
) -> None:
    batch_size = config.web_batch_size
    state = GroupedRedlinkExportState(web_data_path=web_data_path)
    next_progress_page_count = GROUPED_EXPORT_PROGRESS_EVERY_PAGES

    cursor = conn.execute(SQL_GROUPED_REDLINKS_BY_PAGE)
    for page_title, redlinks in iter_grouped_page_redlinks(cursor, batch_size):
        _flush_grouped_redlinks_page(state, page_title, redlinks)
        if state.page_count >= next_progress_page_count:
            print(
                f"ℹ️ [grouped] {state.page_count:,} pages exported, "
                f"{state.redlink_count:,} redlinks written"
            )
            next_progress_page_count += GROUPED_EXPORT_PROGRESS_EVERY_PAGES
    _close_grouped_redlinks_handles(state)
    print(
        f"✅ Saved {len(state.handles)} grouped redlink JSON files ({state.page_count:,} pages, {state.redlink_count:,} redlinks)"
    )


def _export_wanted_links_json(
    conn: duckdb.DuckDBPyConnection,
    output_path: str,
    config: RuntimeConfig,
    *,
    link_namespace: int,
    value_alias: str,
    output_key: str,
) -> None:
    batch_size = config.web_batch_size
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("[")
        first_record = True
        cursor = conn.execute(
            SQL_WANTED_LINKS.format(
                link_namespace=link_namespace,
                value_alias=value_alias,
            )
        )
        for rows in iter_cursor_batches(cursor, batch_size):
            for value, count in rows:
                first_record = _write_group_json_record(
                    handle,
                    first_record,
                    {output_key: normalize_nfc(value), "count": int(count)},
                )
        handle.write("]")


def _export_wanted_json(
    conn: duckdb.DuckDBPyConnection, wanted_path: str, config: RuntimeConfig
) -> None:
    _export_wanted_links_json(
        conn,
        wanted_path,
        config,
        link_namespace=0,
        value_alias="redlink",
        output_key="redlink",
    )


def _export_wanted_categories_json(
    conn: duckdb.DuckDBPyConnection,
    wanted_categories_path: str,
    config: RuntimeConfig,
) -> None:
    _export_wanted_links_json(
        conn,
        wanted_categories_path,
        config,
        link_namespace=14,
        value_alias="category",
        output_key="category",
    )


def _copy_template_directory(source_dir: str, output_dir: str, force: bool) -> None:
    template_dir = files("redlink_database.web").joinpath("templates", source_dir)
    for file in sorted(template_dir.iterdir(), key=lambda item: item.name):
        if not file.is_file():
            continue
        output_filename = os.path.join(output_dir, file.name)
        if os.path.exists(output_filename) and not force:
            print(f"⚠️ Skipping {file.name}, already exists")
            continue
        with (
            file.open("rb") as source_handle,
            open(output_filename, "wb") as target_handle,
        ):
            shutil.copyfileobj(source_handle, target_handle)


def _render_template_file(
    template_env: Environment,
    template_name: str,
    output_path: str,
    force: bool,
    **context,
) -> None:
    if os.path.exists(output_path) and not force:
        print(f"⚠️ Skipping {os.path.basename(output_path)}, already exists")
        return
    rendered_html = template_env.get_template(template_name).render(**context)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(rendered_html)


def _render_template_job(
    template_env: Environment,
    web_path: str,
    force: bool,
    job: TemplateRenderJob,
) -> None:
    _render_template_file(
        template_env,
        job.template_name,
        os.path.join(web_path, job.output_filename),
        force,
        **job.context,
    )


GROUPED_INITIAL_OPTIONS = [
    *[
        {"value": chr(code), "label": chr(code).upper()}
        for code in range(ord("a"), ord("z") + 1)
    ],
    {"value": "number", "label": "#"},
    {"value": "other", "label": "Other"},
]


def _build_footer_context() -> dict[str, str | int]:
    return {
        "current_year": time.localtime().tm_year,
        "author_name": "Riccardo Ruspoli",
        "author_url": "https://riccardoruspoli.com",
    }


def _build_search_template_context() -> dict[str, int]:
    return {
        "search_page_shard_size": SEARCH_PAGE_SHARD_SIZE,
        "search_string_shard_size": SEARCH_STRING_SHARD_SIZE,
        "search_progressive_stop_candidate_strings": SEARCH_PROGRESSIVE_STOP_CANDIDATE_STRINGS,
    }


def _build_page_seo_context(
    title: str, description: str, output_filename: str
) -> dict[str, str]:
    canonical_url = _build_public_page_url(output_filename)
    return {
        "title": title,
        "meta_description": description,
        "canonical_url": canonical_url,
        "og_title": title,
        "og_description": description,
        "og_url": canonical_url,
        "og_type": "website",
    }


def _build_index_template_context(
    language: str,
    dump_version: str,
    footer_context: dict[str, str | int],
) -> dict[str, object]:
    wiki_name = f"{language}wiki"
    return {
        "wiki_name": wiki_name,
        "dump_version": dump_version,
        "page_intro": f"Browse grouped Wikipedia redlinks, search missing pages, and inspect the most requested pages and categories from the current {wiki_name} dump snapshot.",
        **_build_page_seo_context(
            "Redlink Database – Index",
            f"Explore grouped Wikipedia redlinks, search missing pages, and inspect the most requested pages and categories from the latest processed {wiki_name} dump.",
            "index.html",
        ),
        **footer_context,
    }


def _build_search_page_context(
    language: str,
    footer_context: dict[str, str | int],
    search_context: dict[str, int],
) -> dict[str, object]:
    return {
        "language": language,
        "page_intro": "Search page titles and missing linked titles extracted from the current Wikipedia dump snapshot.",
        **_build_page_seo_context(
            "Redlink Database – Search",
            "Search page titles and missing linked titles extracted from the current Wikipedia dump snapshot.",
            "search.html",
        ),
        **footer_context,
        **search_context,
    }


def _build_browse_page_context(
    language: str, footer_context: dict[str, str | int]
) -> dict[str, object]:
    return {
        "language": language,
        "default_initial": "a",
        "initial_options": GROUPED_INITIAL_OPTIONS,
        "page_intro": "Browse grouped pages by initial, including short titles that are not covered by the main search.",
        **_build_page_seo_context(
            "Redlink Database – Browse by initial",
            "Browse grouped pages by initial and inspect the redlinks they contain in the current Wikipedia dump snapshot.",
            "browse.html",
        ),
        **footer_context,
    }


def _build_wanted_page_context(
    language: str, footer_context: dict[str, str | int]
) -> dict[str, object]:
    return {
        "language": language,
        "page_intro": "Browse the most requested missing pages that appear at least ten times in the current dump snapshot.",
        **_build_page_seo_context(
            "Redlink Database – Wanted Pages",
            "Browse the most requested missing Wikipedia pages that appear repeatedly in the current processed dump snapshot.",
            "wanted.html",
        ),
        **footer_context,
    }


def _build_wanted_categories_page_context(
    language: str, footer_context: dict[str, str | int]
) -> dict[str, object]:
    return {
        "language": language,
        "page_intro": "Browse the most requested missing categories that appear at least ten times in the current dump snapshot.",
        **_build_page_seo_context(
            "Redlink Database – Wanted Categories",
            "Browse the most requested missing Wikipedia categories that appear repeatedly in the current processed dump snapshot.",
            "wanted_categories.html",
        ),
        **footer_context,
    }


def _get_build_lastmod_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get_public_html_filenames(web_path: str) -> list[str]:
    return [
        os.path.basename(file_path)
        for file_path in get_files_from_directory(web_path)
        if file_path.endswith(".html")
    ]


def _build_public_page_url(filename: str) -> str:
    if filename == "index.html":
        return f"{PUBLIC_SITE_BASE_URL}/"
    return f"{PUBLIC_SITE_BASE_URL}/{filename}"


def _write_robots_txt(web_path: str) -> None:
    robots_path = os.path.join(web_path, "robots.txt")
    with open(robots_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(ROBOTS_TXT_LINES))


def _write_sitemap_xml(web_path: str, lastmod_timestamp: str) -> None:
    sitemap_path = os.path.join(web_path, "sitemap.xml")
    html_filenames = _get_public_html_filenames(web_path)
    with open(sitemap_path, "w", encoding="utf-8", newline="") as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for filename in html_filenames:
            handle.write("  <url>\n")
            handle.write(f"    <loc>{escape(_build_public_page_url(filename))}</loc>\n")
            handle.write(f"    <lastmod>{lastmod_timestamp}</lastmod>\n")
            handle.write("  </url>\n")
        handle.write("</urlset>\n")


def _write_site_metadata_files(web_path: str) -> None:
    lastmod_timestamp = _get_build_lastmod_timestamp()
    _write_robots_txt(web_path)
    _write_sitemap_xml(web_path, lastmod_timestamp)
    print("✅ Saved robots.txt and sitemap.xml")


def _build_web_template_jobs(
    language: str,
    dump_version: str,
    footer_context: dict[str, str | int],
    search_context: dict[str, int],
) -> list[TemplateRenderJob]:
    return [
        TemplateRenderJob(
            template_name="index_template.html",
            output_filename="index.html",
            context=_build_index_template_context(
                language, dump_version, footer_context
            ),
        ),
        TemplateRenderJob(
            template_name="search_template.html",
            output_filename="search.html",
            context=_build_search_page_context(
                language, footer_context, search_context
            ),
        ),
        TemplateRenderJob(
            template_name="browse_template.html",
            output_filename="browse.html",
            context=_build_browse_page_context(language, footer_context),
        ),
        TemplateRenderJob(
            template_name="wanted_template.html",
            output_filename="wanted.html",
            context=_build_wanted_page_context(language, footer_context),
        ),
        TemplateRenderJob(
            template_name="wanted_categories_template.html",
            output_filename="wanted_categories.html",
            context=_build_wanted_categories_page_context(language, footer_context),
        ),
    ]


def export_web_data_files(
    duckdb_conn: duckdb.DuckDBPyConnection,
    paths: RuntimePaths,
    config: RuntimeConfig,
) -> None:
    """Export grouped, wanted, and search JSON assets for the public site."""

    print("ℹ️ [web] exporting grouped redlinks...")
    _export_grouped_redlinks_json(duckdb_conn, paths.web_data, config)
    print("ℹ️ [web] exporting wanted pages...")
    _export_wanted_json(
        duckdb_conn, os.path.join(paths.web_data, "wanted.json"), config
    )
    print("ℹ️ [web] exporting wanted categories...")
    _export_wanted_categories_json(
        duckdb_conn,
        os.path.join(paths.web_data, "wanted_categories.json"),
        config,
    )
    print("✅ Saved JSON data files")
    print("ℹ️ [web] exporting static search index...")
    export_search_index(duckdb_conn, paths.web_search, config)
    print("✅ Saved static search index")


def render_web_pages(
    paths: RuntimePaths,
    config: RuntimeConfig,
    language: str,
    dump_version: str,
) -> None:
    """Render static HTML pages and auxiliary site metadata files."""

    template_env = Environment(
        loader=PackageLoader("redlink_database.web", "templates")
    )
    footer_context = _build_footer_context()
    search_context = _build_search_template_context()
    render_jobs = _build_web_template_jobs(
        language, dump_version, footer_context, search_context
    )

    _copy_template_directory("style", paths.web_style, config.force)
    _copy_template_directory("script", paths.web_script, config.force)

    for render_job in render_jobs:
        _render_template_job(template_env, paths.web, config.force, render_job)
    _write_site_metadata_files(paths.web)
