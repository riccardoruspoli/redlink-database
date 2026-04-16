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


def _get_html_links(url: str) -> list[str]:
    try:
        response = requests.get(url)
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
        head_resp = requests.head(url, allow_redirects=True, timeout=10)
        return int(head_resp.headers.get("Content-Length", -1))
    except OSError, ValueError:
        return -1


def _get_local_file_size(filepath: str) -> int:
    try:
        return os.path.getsize(filepath)
    except FileNotFoundError:
        return -1


def _stream_download_to_file(url: str, filepath: str) -> None:
    response = requests.get(url, stream=True)
    response.raise_for_status()
    with open(filepath, "wb") as handle:
        for chunk in response.iter_content(chunk_size=8192):
            handle.write(chunk)


def download_files(
    file_urls: Sequence[str], save_dir: str, config: RuntimeConfig
) -> None:
    """Download required dump files, skipping matching local copies unless forced."""

    counter = 0
    total_files = len(file_urls)

    for url in file_urls:
        start_time = time.time()
        filename = os.path.basename(urlparse(url).path)
        filepath = os.path.join(save_dir, filename)

        remote_size = _probe_remote_file_size(url)
        local_size = _get_local_file_size(filepath)

        if local_size != -1 and remote_size == local_size and not config.force:
            counter += 1
            print(
                f"⚠️ [{counter}/{total_files}] [{time.time() - start_time:.2f}s] Skipping {filename}, already downloaded."
            )
            continue

        _stream_download_to_file(url, filepath)

        counter += 1
        print(
            f"✅ [{counter}/{total_files}] [{time.time() - start_time:.2f}s] Saved: {filename}"
        )


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
