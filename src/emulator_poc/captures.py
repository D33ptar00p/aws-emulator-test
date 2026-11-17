"""CaptureRepository: a small example DynamoDB data layer.

Two tables:
  * captures     PK capture_id; GSI by_user (user_id, created_at)
  * user_quotas  PK user_id; tracks captures_used against captures_limit
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from boto3.dynamodb.conditions import Attr, Key

BY_USER_INDEX = "by_user"


class CaptureStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


# Allowed status transitions: current -> {next}
TRANSITIONS: dict[CaptureStatus, frozenset[CaptureStatus]] = {
    CaptureStatus.QUEUED: frozenset({CaptureStatus.PROCESSING, CaptureStatus.FAILED}),
    CaptureStatus.PROCESSING: frozenset({CaptureStatus.DONE, CaptureStatus.FAILED}),
    CaptureStatus.DONE: frozenset(),
    CaptureStatus.FAILED: frozenset(),
}


class CaptureExistsError(Exception):
    """A capture with this id already exists."""


class InvalidTransitionError(Exception):
    """The requested status transition is not allowed from the stored state."""


class QuotaExceededError(Exception):
    """The user has no capture quota left (or has no quota record)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Capture:
    capture_id: str
    user_id: str
    name: str
    camera_count: int = 1
    status: CaptureStatus = CaptureStatus.QUEUED
    created_at: str = field(default_factory=_now)

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item["status"] = self.status.value
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Capture:
        return cls(
            capture_id=item["capture_id"],
            user_id=item["user_id"],
            name=item["name"],
            camera_count=int(item["camera_count"]),
            status=CaptureStatus(item["status"]),
            created_at=item["created_at"],
        )


class CaptureRepository:
    def __init__(self, dynamodb: Any, captures_table: str, quotas_table: str) -> None:
        self._ddb = dynamodb
        self._client = dynamodb.meta.client  # auto-marshalling low-level client
        self.captures = dynamodb.Table(captures_table)
        self.quotas = dynamodb.Table(quotas_table)

    # ---- schema -------------------------------------------------------------

    def create_tables(self) -> None:
        self._ddb.create_table(
            TableName=self.captures.name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "capture_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "capture_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": BY_USER_INDEX,
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        self._ddb.create_table(
            TableName=self.quotas.name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        )
        waiter = self._client.get_waiter("table_exists")
        for name in (self.captures.name, self.quotas.name):
            waiter.wait(TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})

    def delete_tables(self) -> None:
        for table in (self.captures, self.quotas):
            with suppress(self._client.exceptions.ResourceNotFoundException):  # already gone
                table.delete()

    # ---- captures -----------------------------------------------------------

    def put_capture(self, capture: Capture) -> None:
        try:
            self.captures.put_item(
                Item=capture.to_item(),
                ConditionExpression=Attr("capture_id").not_exists(),
            )
        except self._client.exceptions.ConditionalCheckFailedException as exc:
            raise CaptureExistsError(capture.capture_id) from exc

    def get_capture(self, capture_id: str) -> Capture | None:
        item = self.captures.get_item(Key={"capture_id": capture_id}).get("Item")
        return Capture.from_item(item) if item else None

    def list_by_user(self, user_id: str, page_size: int = 25) -> Iterator[Capture]:
        """Yield a user's captures oldest-first, following pagination."""
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self.captures.name,
            IndexName=BY_USER_INDEX,
            KeyConditionExpression=Key("user_id").eq(user_id),
            PaginationConfig={"PageSize": page_size},
        )
        for page in pages:
            for item in page["Items"]:
                yield Capture.from_item(item)

    def update_status(self, capture_id: str, new_status: CaptureStatus) -> Capture:
        """Move a capture to new_status, only if allowed from its current state."""
        allowed_from = [s.value for s, nxt in TRANSITIONS.items() if new_status in nxt]
        if not allowed_from:
            raise InvalidTransitionError(f"nothing may transition to {new_status}")
        try:
            resp = self.captures.update_item(
                Key={"capture_id": capture_id},
                UpdateExpression="SET #s = :new, updated_at = :ts",
                ConditionExpression=Attr("capture_id").exists() & Attr("status").is_in(allowed_from),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new": new_status.value, ":ts": _now()},
                ReturnValues="ALL_NEW",
            )
        except self._client.exceptions.ConditionalCheckFailedException as exc:
            raise InvalidTransitionError(f"{capture_id} cannot move to {new_status}") from exc
        return Capture.from_item(resp["Attributes"])

    def batch_put(self, captures: Iterable[Capture]) -> None:
        with self.captures.batch_writer() as batch:
            for capture in captures:
                batch.put_item(Item=capture.to_item())

    def delete_capture(self, capture_id: str) -> None:
        self.captures.delete_item(Key={"capture_id": capture_id})

    # ---- quotas + transactions ---------------------------------------------

    def set_quota(self, user_id: str, limit: int) -> None:
        self.quotas.put_item(Item={"user_id": user_id, "captures_used": 0, "captures_limit": limit})

    def get_quota(self, user_id: str) -> dict[str, int] | None:
        item = self.quotas.get_item(Key={"user_id": user_id}).get("Item")
        if not item:
            return None
        return {"used": int(item["captures_used"]), "limit": int(item["captures_limit"])}

    def create_with_quota(self, capture: Capture) -> None:
        """Atomically create a capture and consume one unit of the user's quota."""
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.captures.name,
                            "Item": capture.to_item(),
                            "ConditionExpression": "attribute_not_exists(capture_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.quotas.name,
                            "Key": {"user_id": capture.user_id},
                            "UpdateExpression": "SET captures_used = captures_used + :one",
                            "ConditionExpression": "captures_used < captures_limit",
                            "ExpressionAttributeValues": {":one": 1},
                        }
                    },
                ]
            )
        except self._client.exceptions.TransactionCanceledException as exc:
            reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise CaptureExistsError(capture.capture_id) from exc
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                raise QuotaExceededError(capture.user_id) from exc
            raise
