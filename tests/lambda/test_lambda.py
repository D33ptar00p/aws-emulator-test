"""Lambda: create/invoke, errors, env, async, versions/aliases, runtimes, IAM on in-function SDK calls.

Note: the emulator runs function code in an EMBEDDED interpreter (Python reports
sys.implementation.name == "rustpython"; Node reports v20.0.0), not the real Lambda
runtime images. Pure-Python / pure-JS code works; native extensions will not.
"""

from __future__ import annotations

import json

import pytest

from emulator_poc.testing import zip_code

ECHO = """
import json, os
def handler(event, context):
    if event.get("fail"):
        raise ValueError("bad capture")
    return {"echo": event, "stage": os.environ.get("STAGE"), "fn": context.function_name}
"""

WRITE_TO_DDB = """
import boto3
def handler(event, context):
    boto3.resource("dynamodb").Table(event["table"]).put_item(Item={"id": event["id"]})
    return {"ok": True}
"""


@pytest.fixture
def lam(aws):
    return aws.client("lambda")


def _invoke(lam, fn: str, payload: dict, **kw) -> tuple[dict, dict]:
    resp = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode(), **kw)
    return resp, json.loads(resp["Payload"].read() or b"null")


def test_invoke_returns_handler_result(lam, make_function) -> None:
    fn = make_function(ECHO, env={"STAGE": "local"})["FunctionName"]
    resp, body = _invoke(lam, fn, {"capture_id": "c1"})
    assert resp["StatusCode"] == 200
    assert "FunctionError" not in resp
    assert body == {"echo": {"capture_id": "c1"}, "stage": "local", "fn": fn}


def test_unhandled_exception_sets_function_error(lam, make_function) -> None:
    fn = make_function(ECHO)["FunctionName"]
    resp, body = _invoke(lam, fn, {"fail": True})
    assert resp["FunctionError"] == "Unhandled"
    assert body["errorType"] == "ValueError"
    assert body["errorMessage"] == "bad capture"


def test_async_invoke_is_accepted(lam, make_function) -> None:
    fn = make_function(ECHO)["FunctionName"]
    resp = lam.invoke(FunctionName=fn, InvocationType="Event", Payload=b"{}")
    assert resp["StatusCode"] == 202


def test_dry_run_invoke(lam, make_function) -> None:
    fn = make_function(ECHO)["FunctionName"]
    assert lam.invoke(FunctionName=fn, InvocationType="DryRun")["StatusCode"] == 204


def test_nodejs_runtime(lam, make_function) -> None:
    source = "exports.handler = async (event) => ({ sum: event.a + event.b });"
    fn = make_function(source, runtime="nodejs20.x")["FunctionName"]
    _, body = _invoke(lam, fn, {"a": 2, "b": 3})
    assert body == {"sum": 5}


def test_update_code_publish_version_and_alias(lam, make_function) -> None:
    fn = make_function("def handler(e, c):\n    return 'v1'\n")["FunctionName"]
    v1 = lam.publish_version(FunctionName=fn)["Version"]

    lam.update_function_code(
        FunctionName=fn, ZipFile=zip_code({"index.py": "def handler(e, c):\n    return 'v2'\n"})
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    lam.create_alias(FunctionName=fn, Name="stable", FunctionVersion=v1)

    assert _invoke(lam, fn, {})[1] == "v2"  # $LATEST
    assert _invoke(lam, f"{fn}:stable", {})[1] == "v1"  # alias pins v1


def test_missing_function_raises(lam, name) -> None:
    with pytest.raises(lam.exceptions.ResourceNotFoundException):
        lam.invoke(FunctionName=f"{name}-missing")


def test_function_sdk_calls_reach_emulator_with_role_permission(lam, dynamodb, make_function, name) -> None:
    table = dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    table.wait_until_exists()
    fn = make_function(WRITE_TO_DDB, actions=["dynamodb:PutItem"])["FunctionName"]

    resp, body = _invoke(lam, fn, {"table": name, "id": "from-lambda"})
    assert "FunctionError" not in resp, body
    assert table.get_item(Key={"id": "from-lambda"})["Item"] == {"id": "from-lambda"}
    table.delete()


def test_execution_role_policy_is_enforced(lam, dynamodb, make_function, name) -> None:
    """Notable: identity policies ARE evaluated for calls made with an assumed execution role."""
    table = dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    table.wait_until_exists()
    fn = make_function(WRITE_TO_DDB, actions=["dynamodb:GetItem"])["FunctionName"]  # no PutItem

    resp, body = _invoke(lam, fn, {"table": name, "id": "x"})
    assert resp["FunctionError"] == "Unhandled"
    assert "AccessDeniedException" in body["errorMessage"] or body["errorType"].endswith("ClientError")
    assert "Item" not in table.get_item(Key={"id": "x"})
    table.delete()


def test_list_functions_paginator(lam, make_function) -> None:
    created = {make_function(ECHO)["FunctionName"] for _ in range(3)}
    names = set(lam.get_paginator("list_functions").paginate().search("Functions[].FunctionName"))
    assert created <= names
