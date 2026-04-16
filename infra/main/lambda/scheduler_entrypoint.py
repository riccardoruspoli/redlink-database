from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping

import boto3

STATE_MACHINE_RUNNING_STATUSES = {"RUNNING"}
REQUIRED_TABLES = ("page", "pagelinks", "linktarget", "categorylinks")
DATE_PATTERN = re.compile(r'href="(\d{8})/"')
SKIPPED_NOTIFICATION_SUBJECT = "[redlink] Scheduled run skipped"


s3_client = boto3.client("s3")
sns_client = boto3.client("sns")
step_functions_client = boto3.client("stepfunctions")


def _publish(message: str, subject: str) -> None:
    sns_client.publish(
        TopicArn=os.environ["NOTIFICATIONS_ARN"],
        Subject=subject,
        Message=message,
    )


def _get_latest_processed_dump_version(bucket: str, wiki_name: str) -> str | None:
    paginator = s3_client.get_paginator("list_objects_v2")
    versions: set[str] = set()

    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"{wiki_name}/", Delimiter="/"
    ):
        for prefix in page.get("CommonPrefixes", []):
            value = prefix["Prefix"].split("/")[1]
            if re.fullmatch(r"\d{8}", value):
                versions.add(value)

    return max(versions) if versions else None


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "redlink-database-cloud-bootstrap/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def _get_latest_remote_dump_version(wiki_name: str) -> str | None:
    html = _fetch_text(f"{os.environ['WIKIMEDIA_DUMPS_URL'].rstrip('/')}/{wiki_name}/")
    versions = set(DATE_PATTERN.findall(html))
    return max(versions) if versions else None


def _iter_done_filenames(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        status = value.get("status")
        files = value.get("files")
        if status == "done" and isinstance(files, Mapping):
            for filename in files:
                if isinstance(filename, str):
                    yield filename

        for key, nested in value.items():
            if (
                isinstance(nested, Mapping)
                and nested.get("status") == "done"
                and isinstance(key, str)
            ):
                yield key
            name = nested.get("name") if isinstance(nested, Mapping) else None
            if isinstance(name, str) and nested.get("status") == "done":
                yield name
            yield from _iter_done_filenames(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_done_filenames(item)


def _dump_is_ready(wiki_name: str, dump_version: str) -> bool:
    payload = _fetch_text(
        f"{os.environ['WIKIMEDIA_DUMPS_URL'].rstrip('/')}/{wiki_name}/{dump_version}/dumpstatus.json"
    )
    dump_status = json.loads(payload)
    required_filenames = {
        f"{wiki_name}-{dump_version}-{table_name}.sql.gz"
        for table_name in REQUIRED_TABLES
    }
    done_filenames = set(_iter_done_filenames(dump_status))
    return required_filenames.issubset(done_filenames)


def _has_running_execution(state_machine_arn: str) -> bool:
    response = step_functions_client.list_executions(
        stateMachineArn=state_machine_arn,
        statusFilter="RUNNING",
        maxResults=1,
    )
    return any(
        execution.get("status") in STATE_MACHINE_RUNNING_STATUSES
        for execution in response.get("executions", [])
    )


def _parse_force(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def handler(event: dict, context: object) -> dict:
    del context

    state_machine_arn = os.environ["STATE_MACHINE_ARN"]
    language = os.environ["LANGUAGE"]
    wiki_name = f"{language}wiki"
    artifacts_bucket = os.environ["ARTIFACTS_BUCKET"]
    force = _parse_force(event.get("force"))
    requested_dump_version = event.get("dump_version")

    if _has_running_execution(state_machine_arn):
        _publish(
            f"Skipped scheduled run for {wiki_name}: a pipeline execution is already running.",
            SKIPPED_NOTIFICATION_SUBJECT,
        )
        return {"started": False, "reason": "execution_running"}

    current_dump = _get_latest_processed_dump_version(artifacts_bucket, wiki_name)

    if requested_dump_version is not None:
        if not isinstance(requested_dump_version, str) or not re.fullmatch(
            r"\d{8}", requested_dump_version
        ):
            _publish(
                f"Skipped scheduled run for {wiki_name}: invalid dump_version override {requested_dump_version!r}.",
                SKIPPED_NOTIFICATION_SUBJECT,
            )
            return {"started": False, "reason": "invalid_dump_version"}
        target_dump_version = requested_dump_version
    else:
        try:
            latest_remote_dump = _get_latest_remote_dump_version(wiki_name)
        except (TimeoutError, urllib.error.URLError) as error:
            _publish(
                f"Skipped scheduled run for {wiki_name}: failed to discover the latest remote dump ({error}).",
                SKIPPED_NOTIFICATION_SUBJECT,
            )
            return {"started": False, "reason": "dump_discovery_failed"}

        if latest_remote_dump is None:
            _publish(
                f"Skipped scheduled run for {wiki_name}: no remote dump directory was found.",
                SKIPPED_NOTIFICATION_SUBJECT,
            )
            return {"started": False, "reason": "no_remote_dump"}

        target_dump_version = latest_remote_dump

    if not force and current_dump is not None and target_dump_version <= current_dump:
        _publish(
            f"Skipped scheduled run for {wiki_name}: latest processed dump is already {current_dump}.",
            SKIPPED_NOTIFICATION_SUBJECT,
        )
        return {
            "started": False,
            "reason": "already_processed",
            "dump_version": current_dump,
        }

    try:
        ready = _dump_is_ready(wiki_name, target_dump_version)
    except (
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as error:
        _publish(
            f"Skipped scheduled run for {wiki_name}: failed to verify dump {target_dump_version} readiness ({error}).",
            SKIPPED_NOTIFICATION_SUBJECT,
        )
        return {
            "started": False,
            "reason": "readiness_check_failed",
            "dump_version": target_dump_version,
        }

    if not ready:
        _publish(
            f"Skipped scheduled run for {wiki_name}: latest dump {target_dump_version} is not ready yet.",
            SKIPPED_NOTIFICATION_SUBJECT,
        )
        return {
            "started": False,
            "reason": "dump_not_ready",
            "dump_version": target_dump_version,
        }

    execution_name = f"{wiki_name}-{target_dump_version}-{int(time.time())}"
    step_functions_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(
            {
                "dump_version": target_dump_version,
                "language": language,
                "wiki_name": wiki_name,
                "force": "1" if force else "0",
            }
        ),
    )

    return {
        "started": True,
        "dump_version": target_dump_version,
        "wiki_name": wiki_name,
        "force": force,
    }
