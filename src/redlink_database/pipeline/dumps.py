import gzip
import os
import re
import shutil
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from natsort import natsorted

from redlink_database.pipeline.config import TABLE_CONFIG, RuntimeConfig, SharedCounter
from redlink_database.pipeline.paths import get_files_from_directory

DOWNLOAD_WORKERS = 1
DOWNLOAD_START_DELAY_SECONDS = 1
REQUEST_HEADERS = {"User-Agent": "redlink-database/0.1"}


def _get_html_links(url: str) -> list[str]:
    try:
        response = requests.get(url, headers=REQUEST_HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"❌ Failed to retrieve URL: {url} | {exc}") from exc
    soup = BeautifulSoup(response.text, "html.parser")
    return [link.get("href") for link in soup.find_all("a")]


def get_subfolders(url: str) -> list[str]:
    """Return child directory URLs from a simple Wikimedia dump index page."""

    subfolders = []
    for href in _get_html_links(url):
        if href and href.endswith("/") and href != "../":
            subfolders.append(urljoin(url, href))
    return subfolders


def get_files_from_url(url: str, base_path: str) -> list[str]:
    """Return the required SQL dump file URLs for the active table set."""

    file_patterns = [
        rf"{base_path}-\d{{8}}-page\.sql\.gz$",
        rf"{base_path}-\d{{8}}-pagelinks\.sql\.gz$",
        rf"{base_path}-\d{{8}}-linktarget\.sql\.gz$",
        rf"{base_path}-\d{{8}}-categorylinks\.sql\.gz$",
    ]
    files = []
    for href in _get_html_links(url):
        if (
            href
            and not href.endswith("/")
            and not href.startswith("?")
            and not href.startswith("#")
        ):
            files.append(urljoin(url, href))

    filtered_files = [
        file
        for file in set(files)
        if any(re.search(pattern, file) for pattern in file_patterns)
    ]
    return natsorted(filtered_files)


def _probe_remote_file_size(url: str) -> int:
    try:
        head_resp = requests.head(
            url, headers=REQUEST_HEADERS, allow_redirects=True, timeout=10
        )
        return int(head_resp.headers.get("Content-Length", -1))
    except OSError, ValueError:
        return -1


def _get_local_file_size(filepath: str) -> int:
    try:
        return os.path.getsize(filepath)
    except FileNotFoundError:
        return -1


def _stream_download_to_file(url: str, filepath: str) -> None:
    response = requests.get(url, headers=REQUEST_HEADERS, stream=True)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        retry_after = response.headers.get("Retry-After")
        retry_hint = f" Retry-After: {retry_after}." if retry_after else ""
        raise RuntimeError(
            f"Download failed for {url} with HTTP {response.status_code}.{retry_hint}"
        ) from exc
    with open(filepath, "wb") as handle:
        for chunk in response.iter_content(chunk_size=8192):
            handle.write(chunk)


def _download_file(url: str, save_dir: str, force: bool) -> str:
    """Download one dump file or report that its complete local copy is reused."""

    start_time = time.time()
    filename = os.path.basename(urlparse(url).path)
    filepath = os.path.join(save_dir, filename)
    remote_size = _probe_remote_file_size(url)
    local_size = _get_local_file_size(filepath)

    if local_size != -1 and remote_size == local_size and not force:
        return f"⚠️ [{time.time() - start_time:.2f}s] Skipping {filename}, already downloaded."

    _stream_download_to_file(url, filepath)
    return f"✅ [{time.time() - start_time:.2f}s] Saved: {filename}"


def download_files(
    file_urls: Sequence[str], save_dir: str, config: RuntimeConfig
) -> None:
    """Download required dump files, skipping matching local copies unless forced."""

    total_files = len(file_urls)
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        tasks = []
        for index, url in enumerate(file_urls):
            tasks.append(executor.submit(_download_file, url, save_dir, config.force))
            if index < total_files - 1:
                time.sleep(DOWNLOAD_START_DELAY_SECONDS)
        for counter, future in enumerate(as_completed(tasks), start=1):
            print(f"[{counter}/{total_files}] {future.result()}")


def _decompress_gz_file(
    input_path: str,
    output_path: str,
    total_files: int,
    counter: SharedCounter,
    chunk_size: int = 1024 * 100,
) -> None:
    start_time = time.time()
    try:
        with (
            gzip.open(input_path, "rb") as file_in,
            open(output_path, "wb", buffering=chunk_size) as file_out,
        ):
            shutil.copyfileobj(file_in, file_out, length=chunk_size)

        n = counter.inc()
        print(
            f"✅ [{n}/{total_files}] [{time.time() - start_time:.2f}s] [{threading.current_thread().name}] Decompressed: {os.path.basename(input_path)}"
        )
    except OSError as exc:
        print(f"❌ Failed: {input_path} → {output_path} | {exc}")


def decompress_gz_files(
    compressed_files: Sequence[str],
    output_dir: str,
    config: RuntimeConfig,
) -> None:
    """Decompress `.gz` dump files in parallel into the target directory."""

    total_files = len(compressed_files)
    counter = SharedCounter()
    decompressed_files = set(get_files_from_directory(output_dir))

    tasks = []
    with ThreadPoolExecutor(
        max_workers=config.decompress_workers, thread_name_prefix="decompressor"
    ) as executor:
        for input_path in compressed_files:
            filename = os.path.basename(input_path).replace(".gz", "")
            output_path = os.path.join(output_dir, filename)

            if output_path in decompressed_files and not config.force:
                n = counter.inc()
                print(
                    f"⚠️ [{n}/{total_files}] [{threading.current_thread().name}] Skipping {input_path}, already decompressed."
                )
                continue

            tasks.append(
                executor.submit(
                    _decompress_gz_file,
                    input_path,
                    output_path,
                    total_files,
                    counter,
                )
            )

        for future in as_completed(tasks):
            future.result()


def find_required_sql_files(sql_dir: str, base_path: str) -> dict[str, str]:
    """Resolve exactly one decompressed SQL file per required table."""

    files = os.listdir(sql_dir)
    out: dict[str, str] = {}
    for table_name, table_config in TABLE_CONFIG.items():
        pattern = table_config["pattern"].format(base_path=base_path)
        matches = [filename for filename in files if re.match(pattern, filename)]
        if len(matches) != 1:
            raise SystemExit(
                f"❌ Expected exactly 1 match for '{pattern}' in '{sql_dir}', found {len(matches)}."
            )
        out[table_name] = os.path.join(sql_dir, matches[0])
    return out
