"""End-to-end walkthrough of CaptureRepository against the local AWS Emulator."""

from __future__ import annotations

import sys
import uuid

import boto3
from botocore.exceptions import ClientError

from emulator_poc.captures import (
    Capture,
    CaptureRepository,
    CaptureStatus,
    InvalidTransitionError,
    QuotaExceededError,
)
from emulator_poc.client import NotLocalEndpointError, assert_local_endpoint, dynamodb_resource


def run() -> None:
    endpoint = assert_local_endpoint()
    ident = boto3.client("sts").get_caller_identity()
    print(f"[1] endpoint={endpoint} account={ident['Account']} arn={ident['Arn']}")

    suffix = uuid.uuid4().hex[:6]
    repo = CaptureRepository(dynamodb_resource(), f"captures-demo-{suffix}", f"quotas-demo-{suffix}")
    repo.create_tables()
    print(f"[2] created tables {repo.captures.name}, {repo.quotas.name}")

    try:
        repo.set_quota("studio-1", limit=3)
        for i in range(3):
            repo.create_with_quota(Capture(f"cap-{i}", "studio-1", f"Stunt take {i}", camera_count=4))
        print(f"[3] 3 captures created transactionally, quota={repo.get_quota('studio-1')}")

        try:
            repo.create_with_quota(Capture("cap-3", "studio-1", "One too many"))
        except QuotaExceededError:
            print(f"[4] 4th capture rejected by transaction, quota={repo.get_quota('studio-1')}")

        repo.update_status("cap-0", CaptureStatus.PROCESSING)
        repo.update_status("cap-0", CaptureStatus.DONE)
        print(f"[5] cap-0 status -> {repo.get_capture('cap-0').status}")
        try:
            repo.update_status("cap-1", CaptureStatus.DONE)
        except InvalidTransitionError as exc:
            print(f"[6] guarded transition rejected: {exc}")

        listed = [(c.capture_id, c.status.value) for c in repo.list_by_user("studio-1", page_size=2)]
        print(f"[7] GSI query by_user (paged by 2): {listed}")

        repo.delete_capture("cap-2")
        print(f"[8] deleted cap-2 -> get returns {repo.get_capture('cap-2')}")
    finally:
        repo.delete_tables()
        print("[9] tables deleted")


def main() -> int:
    try:
        run()
        return 0
    except (ClientError, NotLocalEndpointError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
