"""EventBridge: custom buses, pattern matching, delivery to SQS / Lambda, input transformers."""

from __future__ import annotations

import json

import pytest

from emulator_poc.testing import wait_for

SOURCE = "com.example.capture"


@pytest.fixture
def events(aws):
    return aws.client("events")


@pytest.fixture
def sqs(aws):
    return aws.client("sqs")


@pytest.fixture
def bus(events, name, cleanup) -> str:
    events.create_event_bus(Name=name)
    cleanup.append(lambda: events.delete_event_bus(Name=name))
    return name


@pytest.fixture
def make_queue(sqs, name, cleanup):
    counter = iter(range(100))

    def _make() -> tuple[str, str]:
        url = sqs.create_queue(QueueName=f"{name}-q{next(counter)}")["QueueUrl"]
        cleanup.append(lambda: sqs.delete_queue(QueueUrl=url))
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        return url, arn

    return _make


def _allow_rule_to_send(sqs, queue_url: str, queue_arn: str, rule_arn: str) -> None:
    """Same queue resource policy AWS requires for an EventBridge -> SQS target."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": queue_arn,
                "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
            }
        ],
    }
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy)})


def _put_rule(events, cleanup, bus: str, rule: str, pattern: dict) -> str:
    arn = events.put_rule(Name=rule, EventBusName=bus, EventPattern=json.dumps(pattern))["RuleArn"]

    def _remove() -> None:
        ids = [t["Id"] for t in events.list_targets_by_rule(Rule=rule, EventBusName=bus)["Targets"]]
        if ids:
            events.remove_targets(Rule=rule, EventBusName=bus, Ids=ids)
        events.delete_rule(Name=rule, EventBusName=bus)

    cleanup.append(_remove)
    return arn


def _drain(sqs, url: str, expect: int) -> list[dict]:
    got: list[dict] = []

    def poll() -> list[dict] | None:
        for m in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1).get(
            "Messages", []
        ):
            got.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
        return got if len(got) >= expect else None

    return wait_for(poll, timeout=10)


def _event(detail_type: str, detail: dict, bus: str) -> dict:
    return {"Source": SOURCE, "DetailType": detail_type, "Detail": json.dumps(detail), "EventBusName": bus}


def test_test_event_pattern(events) -> None:
    pattern = {"source": [SOURCE], "detail": {"cameras": [{"numeric": [">=", 2]}]}}
    base = {
        "id": "1",
        "account": "000000000000",
        "source": SOURCE,
        "time": "2026-01-01T00:00:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail-type": "CaptureCompleted",
    }
    ok = events.test_event_pattern(
        EventPattern=json.dumps(pattern), Event=json.dumps({**base, "detail": {"cameras": 4}})
    )
    no = events.test_event_pattern(
        EventPattern=json.dumps(pattern), Event=json.dumps({**base, "detail": {"cameras": 1}})
    )
    assert (ok["Result"], no["Result"]) == (True, False)


def test_rule_routes_only_matching_events_to_sqs(events, sqs, bus, name, make_queue, cleanup) -> None:
    url, qarn = make_queue()
    rule_arn = _put_rule(
        events, cleanup, bus, f"{name}-done", {"source": [SOURCE], "detail-type": ["CaptureCompleted"]}
    )
    _allow_rule_to_send(sqs, url, qarn, rule_arn)
    events.put_targets(Rule=f"{name}-done", EventBusName=bus, Targets=[{"Id": "q", "Arn": qarn}])

    resp = events.put_events(
        Entries=[
            _event("CaptureCompleted", {"capture_id": "c1"}, bus),
            _event("CaptureStarted", {"capture_id": "c2"}, bus),  # must not match
        ]
    )
    assert resp["FailedEntryCount"] == 0

    msgs = _drain(sqs, url, expect=1)
    assert [m["detail"] for m in msgs] == [{"capture_id": "c1"}]
    assert msgs[0]["detail-type"] == "CaptureCompleted"
    assert msgs[0]["source"] == SOURCE


def test_input_transformer(events, sqs, bus, name, make_queue, cleanup) -> None:
    url, qarn = make_queue()
    rule = f"{name}-xf"
    rule_arn = _put_rule(events, cleanup, bus, rule, {"source": [SOURCE]})
    _allow_rule_to_send(sqs, url, qarn, rule_arn)
    events.put_targets(
        Rule=rule,
        EventBusName=bus,
        Targets=[
            {
                "Id": "q",
                "Arn": qarn,
                "InputTransformer": {
                    "InputPathsMap": {"id": "$.detail.capture_id", "type": "$.detail-type"},
                    "InputTemplate": '{"capture": <id>, "kind": <type>}',
                },
            }
        ],
    )
    events.put_events(Entries=[_event("CaptureCompleted", {"capture_id": "c9"}, bus)])
    assert _drain(sqs, url, expect=1) == [{"capture": "c9", "kind": "CaptureCompleted"}]


def test_content_filtering_prefix_and_numeric(events, sqs, bus, name, make_queue, cleanup) -> None:
    url, qarn = make_queue()
    rule = f"{name}-filter"
    pattern = {
        "source": [SOURCE],
        "detail": {"studio": [{"prefix": "studio-"}], "cameras": [{"numeric": [">", 3]}]},
    }
    rule_arn = _put_rule(events, cleanup, bus, rule, pattern)
    _allow_rule_to_send(sqs, url, qarn, rule_arn)
    events.put_targets(Rule=rule, EventBusName=bus, Targets=[{"Id": "q", "Arn": qarn}])

    events.put_events(
        Entries=[
            _event("C", {"studio": "studio-alpha", "cameras": 8}, bus),  # match
            _event("C", {"studio": "studio-alpha", "cameras": 2}, bus),  # too few cameras
            _event("C", {"studio": "lab-beta", "cameras": 8}, bus),  # wrong prefix
        ]
    )
    msgs = _drain(sqs, url, expect=1)
    assert [m["detail"]["cameras"] for m in msgs] == [8]


def test_rule_invokes_lambda_target(aws, events, dynamodb, bus, name, make_function, cleanup) -> None:
    """EventBridge -> Lambda -> DynamoDB, observed via the item the function writes."""
    table = dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    table.wait_until_exists()
    cleanup.append(table.delete)
    fn = make_function(
        "import boto3\n"
        "def handler(event, context):\n"
        f"    boto3.resource('dynamodb').Table('{name}').put_item(Item={{'id': event['detail']['capture_id']}})\n",
        actions=["dynamodb:PutItem"],
    )
    rule = f"{name}-lambda"
    rule_arn = _put_rule(events, cleanup, bus, rule, {"source": [SOURCE]})
    aws.client("lambda").add_permission(
        FunctionName=fn["FunctionName"],
        StatementId="events",
        Action="lambda:InvokeFunction",
        Principal="events.amazonaws.com",
        SourceArn=rule_arn,
    )
    events.put_targets(Rule=rule, EventBusName=bus, Targets=[{"Id": "fn", "Arn": fn["FunctionArn"]}])

    events.put_events(Entries=[_event("CaptureCompleted", {"capture_id": "via-eb"}, bus)])
    assert wait_for(lambda: table.get_item(Key={"id": "via-eb"}).get("Item"), timeout=10) == {"id": "via-eb"}


def test_disabled_rule_does_not_deliver(events, sqs, bus, name, make_queue, cleanup) -> None:
    url, qarn = make_queue()
    rule = f"{name}-off"
    rule_arn = _put_rule(events, cleanup, bus, rule, {"source": [SOURCE]})
    _allow_rule_to_send(sqs, url, qarn, rule_arn)
    events.put_targets(Rule=rule, EventBusName=bus, Targets=[{"Id": "q", "Arn": qarn}])
    events.disable_rule(Name=rule, EventBusName=bus)

    events.put_events(Entries=[_event("C", {"n": 1}, bus)])
    assert sqs.receive_message(QueueUrl=url, WaitTimeSeconds=2).get("Messages", []) == []
