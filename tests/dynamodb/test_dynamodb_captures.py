from __future__ import annotations

import pytest

from emulator_poc.captures import (
    Capture,
    CaptureExistsError,
    CaptureRepository,
    CaptureStatus,
    InvalidTransitionError,
    QuotaExceededError,
)


def _capture(cid: str = "cap-1", user: str = "user-a", **kw) -> Capture:
    return Capture(capture_id=cid, user_id=user, name=f"take {cid}", **kw)


def test_caller_identity_is_emulated_account(aws) -> None:
    assert aws.client("sts").get_caller_identity()["Account"] == "000000000000"


def test_tables_created_with_gsi(repo: CaptureRepository) -> None:
    desc = repo._client.describe_table(TableName=repo.captures.name)["Table"]
    assert desc["TableStatus"] == "ACTIVE"
    assert desc["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"
    assert [g["IndexName"] for g in desc["GlobalSecondaryIndexes"]] == ["by_user"]


def test_put_and_get_round_trip(repo: CaptureRepository) -> None:
    cap = _capture(camera_count=4)
    repo.put_capture(cap)
    assert repo.get_capture("cap-1") == cap


def test_get_missing_returns_none(repo: CaptureRepository) -> None:
    assert repo.get_capture("nope") is None


def test_put_does_not_overwrite(repo: CaptureRepository) -> None:
    repo.put_capture(_capture())
    with pytest.raises(CaptureExistsError):
        repo.put_capture(_capture())


def test_list_by_user_is_ordered_and_paginated(repo: CaptureRepository) -> None:
    caps = [_capture(f"cap-{i:02d}", created_at=f"2026-01-01T00:00:{i:02d}") for i in range(7)]
    repo.batch_put(reversed(caps))
    repo.put_capture(_capture("other", user="user-b"))

    listed = list(repo.list_by_user("user-a", page_size=3))  # forces 3 pages
    assert [c.capture_id for c in listed] == [c.capture_id for c in caps]


def test_status_transitions(repo: CaptureRepository) -> None:
    repo.put_capture(_capture())
    assert repo.update_status("cap-1", CaptureStatus.PROCESSING).status is CaptureStatus.PROCESSING
    assert repo.update_status("cap-1", CaptureStatus.DONE).status is CaptureStatus.DONE

    with pytest.raises(InvalidTransitionError):
        repo.update_status("cap-1", CaptureStatus.PROCESSING)  # done is terminal


def test_status_transition_rejects_skipping(repo: CaptureRepository) -> None:
    repo.put_capture(_capture())
    with pytest.raises(InvalidTransitionError):
        repo.update_status("cap-1", CaptureStatus.DONE)  # queued -> done not allowed
    assert repo.get_capture("cap-1").status is CaptureStatus.QUEUED


def test_status_update_on_missing_item(repo: CaptureRepository) -> None:
    with pytest.raises(InvalidTransitionError):
        repo.update_status("ghost", CaptureStatus.PROCESSING)
    assert repo.get_capture("ghost") is None  # update must not upsert


def test_batch_put_over_25_items(repo: CaptureRepository) -> None:
    repo.batch_put(_capture(f"b-{i:03d}") for i in range(60))  # > 25 forces chunking
    assert len(list(repo.list_by_user("user-a"))) == 60


def test_delete(repo: CaptureRepository) -> None:
    repo.put_capture(_capture())
    repo.delete_capture("cap-1")
    assert repo.get_capture("cap-1") is None


def test_transaction_consumes_quota(repo: CaptureRepository) -> None:
    repo.set_quota("user-a", limit=2)
    repo.create_with_quota(_capture("t-1"))
    repo.create_with_quota(_capture("t-2"))
    assert repo.get_quota("user-a") == {"used": 2, "limit": 2}

    with pytest.raises(QuotaExceededError):
        repo.create_with_quota(_capture("t-3"))
    # Atomicity: the failed transaction wrote nothing.
    assert repo.get_capture("t-3") is None
    assert repo.get_quota("user-a") == {"used": 2, "limit": 2}


def test_transaction_duplicate_capture_rolls_back(repo: CaptureRepository) -> None:
    repo.set_quota("user-a", limit=5)
    repo.create_with_quota(_capture("t-1"))
    with pytest.raises(CaptureExistsError):
        repo.create_with_quota(_capture("t-1"))
    assert repo.get_quota("user-a") == {"used": 1, "limit": 5}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Emulator fidelity gap: on a missing item, AWS evaluates ConditionExpression first and cancels with "
        "ConditionalCheckFailed; the emulator evaluates the UpdateExpression first and returns "
        "ValidationException ('refers to an attribute that does not exist'). See README > Findings."
    ),
)
def test_transaction_without_quota_record(repo: CaptureRepository) -> None:
    with pytest.raises(QuotaExceededError):
        repo.create_with_quota(_capture("t-1"))
    assert repo.get_capture("t-1") is None


def test_missing_table_raises_resource_not_found(dynamodb) -> None:
    table = dynamodb.Table("does-not-exist")
    with pytest.raises(table.meta.client.exceptions.ResourceNotFoundException):
        table.get_item(Key={"capture_id": "x"})


def test_ttl_round_trip(repo: CaptureRepository) -> None:
    repo._client.update_time_to_live(
        TableName=repo.captures.name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
    )
    desc = repo._client.describe_time_to_live(TableName=repo.captures.name)
    assert desc["TimeToLiveDescription"]["AttributeName"] == "expires_at"
    assert desc["TimeToLiveDescription"]["TimeToLiveStatus"] in {"ENABLED", "ENABLING"}
