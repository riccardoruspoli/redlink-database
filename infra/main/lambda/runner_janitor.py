from __future__ import annotations

import datetime as dt
import os

import boto3

ec2_client = boto3.client("ec2")
sns_client = boto3.client("sns")


def _publish(message: str, subject: str) -> None:
    sns_client.publish(
        TopicArn=os.environ["NOTIFICATIONS_ARN"],
        Subject=subject,
        Message=message,
    )


def _deadline() -> dt.datetime:
    timeout_seconds = int(os.environ["STATE_MACHINE_TIMEOUT"])
    grace_seconds = int(os.environ["GRACE_SECONDS"])
    return dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=timeout_seconds + grace_seconds
    )


def handler(event: dict, context: object) -> dict:
    del event, context

    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [os.environ["PROJECT"]]},
            {"Name": "tag:Environment", "Values": [os.environ["ENVIRONMENT"]]},
            {"Name": "tag:Role", "Values": ["runner"]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )

    deadline = _deadline()
    instance_ids: list[str] = []

    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            launch_time = instance.get("LaunchTime")
            instance_id = instance.get("InstanceId")
            if (
                isinstance(instance_id, str)
                and launch_time is not None
                and launch_time <= deadline
            ):
                instance_ids.append(instance_id)

    if not instance_ids:
        return {"terminated": 0}

    ec2_client.terminate_instances(InstanceIds=instance_ids)
    _publish(
        f"Janitor terminated orphaned runner instances: {', '.join(instance_ids)}.",
        "[redlink] Runner janitor cleanup",
    )
    return {"terminated": len(instance_ids), "instance_ids": instance_ids}
