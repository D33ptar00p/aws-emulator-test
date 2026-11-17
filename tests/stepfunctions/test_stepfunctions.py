"""Step Functions: ASL execution (Pass/Choice/Map/Fail), Lambda + DynamoDB integrations, Express sync."""

from __future__ import annotations

import json

import pytest

from emulator_poc.testing import wait_for

SCORE = """
def handler(event, context):
    return {"capture_id": event["capture_id"], "score": len(event["capture_id"]) * 10}
"""


@pytest.fixture
def sfn(aws):
    return aws.client("stepfunctions")


@pytest.fixture
def make_state_machine(sfn, name, make_role, cleanup):
    counter = iter(range(100))

    def _make(definition: dict, actions: list[str] | None = None, sm_type: str = "STANDARD") -> str:
        role = make_role(["states.amazonaws.com"], actions)
        arn = sfn.create_state_machine(
            name=f"{name}-sm{next(counter)}", definition=json.dumps(definition), roleArn=role, type=sm_type
        )["stateMachineArn"]
        cleanup.append(lambda: sfn.delete_state_machine(stateMachineArn=arn))
        return arn

    return _make


def _run(sfn, sm_arn: str, payload: dict) -> dict:
    ex = sfn.start_execution(stateMachineArn=sm_arn, input=json.dumps(payload))["executionArn"]

    def done() -> dict | None:
        d = sfn.describe_execution(executionArn=ex)
        return d if d["status"] != "RUNNING" else None

    return wait_for(done)


ROUTER = {
    "StartAt": "Tag",
    "States": {
        "Tag": {"Type": "Pass", "Result": "demo", "ResultPath": "$.product", "Next": "Route"},
        "Route": {
            "Type": "Choice",
            "Choices": [{"Variable": "$.cameras", "NumericGreaterThanEquals": 2, "Next": "MultiCam"}],
            "Default": "TooFew",
        },
        "MultiCam": {"Type": "Pass", "Parameters": {"mode": "multi", "product.$": "$.product"}, "End": True},
        "TooFew": {"Type": "Fail", "Error": "NotEnoughCameras", "Cause": "need >= 2 cameras"},
    },
}


def test_pass_choice_succeeds(sfn, make_state_machine) -> None:
    result = _run(sfn, make_state_machine(ROUTER), {"cameras": 4})
    assert result["status"] == "SUCCEEDED"
    assert json.loads(result["output"]) == {"mode": "multi", "product": "demo"}


def test_fail_state(sfn, make_state_machine) -> None:
    result = _run(sfn, make_state_machine(ROUTER), {"cameras": 1})
    assert result["status"] == "FAILED"
    assert result["error"] == "NotEnoughCameras"


def test_execution_history_records_states(sfn, make_state_machine) -> None:
    result = _run(sfn, make_state_machine(ROUTER), {"cameras": 2})
    events = sfn.get_execution_history(executionArn=result["executionArn"])["events"]
    entered = [e["stateEnteredEventDetails"]["name"] for e in events if "stateEnteredEventDetails" in e]
    assert entered == ["Tag", "Route", "MultiCam"]


def test_map_state_over_lambda(sfn, make_function, make_state_machine) -> None:
    fn_arn = make_function(SCORE)["FunctionArn"]
    definition = {
        "StartAt": "Each",
        "States": {
            "Each": {
                "Type": "Map",
                "ItemsPath": "$.captures",
                "ItemSelector": {"capture_id.$": "$$.Map.Item.Value"},
                "ItemProcessor": {
                    "ProcessorConfig": {"Mode": "INLINE"},
                    "StartAt": "Score",
                    "States": {
                        "Score": {
                            "Type": "Task",
                            "Resource": "arn:aws:states:::lambda:invoke",
                            "Parameters": {"FunctionName": fn_arn, "Payload.$": "$"},
                            "OutputPath": "$.Payload",
                            "End": True,
                        }
                    },
                },
                "End": True,
            }
        },
    }
    sm = make_state_machine(definition, actions=["lambda:InvokeFunction"])
    result = _run(sfn, sm, {"captures": ["a", "bb", "ccc"]})
    assert result["status"] == "SUCCEEDED", result.get("cause")
    assert [r["score"] for r in json.loads(result["output"])] == [10, 20, 30]


def test_task_fails_without_role_permission(sfn, make_function, make_state_machine) -> None:
    """Notable: the state machine's execution role policy IS evaluated by the emulator."""
    fn_arn = make_function(SCORE)["FunctionArn"]
    definition = {
        "StartAt": "Score",
        "States": {
            "Score": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": fn_arn, "Payload.$": "$"},
                "End": True,
            }
        },
    }
    result = _run(sfn, make_state_machine(definition, actions=None), {"capture_id": "x"})
    assert result["status"] == "FAILED"
    assert "not authorized" in result.get("cause", "")


def test_retry_and_catch(sfn, make_function, make_state_machine) -> None:
    fn_arn = make_function("def handler(e, c):\n    raise RuntimeError('flaky')\n")["FunctionArn"]
    definition = {
        "StartAt": "Call",
        "States": {
            "Call": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": fn_arn, "Payload.$": "$"},
                "Retry": [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 2, "IntervalSeconds": 1}],
                "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.err", "Next": "Recovered"}],
                "End": True,
            },
            "Recovered": {"Type": "Pass", "End": True},
        },
    }
    result = _run(sfn, make_state_machine(definition, actions=["lambda:InvokeFunction"]), {"k": 1})
    assert result["status"] == "SUCCEEDED"
    assert "err" in json.loads(result["output"])


def test_dynamodb_put_item_integration(sfn, dynamodb, name, make_state_machine) -> None:
    table = dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    table.wait_until_exists()
    definition = {
        "StartAt": "Save",
        "States": {
            "Save": {
                "Type": "Task",
                "Resource": "arn:aws:states:::dynamodb:putItem",
                "Parameters": {"TableName": name, "Item": {"id": {"S.$": "$.id"}, "status": {"S": "queued"}}},
                "End": True,
            }
        },
    }
    result = _run(sfn, make_state_machine(definition, actions=["dynamodb:PutItem"]), {"id": "cap-7"})
    assert result["status"] == "SUCCEEDED", result.get("cause")
    assert table.get_item(Key={"id": "cap-7"})["Item"] == {"id": "cap-7", "status": "queued"}
    table.delete()


def test_express_start_sync_execution(sfn, make_state_machine) -> None:
    definition = {
        "StartAt": "P",
        "States": {"P": {"Type": "Pass", "Parameters": {"ok.$": "$.a"}, "End": True}},
    }
    sm = make_state_machine(definition, sm_type="EXPRESS")
    resp = sfn.start_sync_execution(stateMachineArn=sm, input=json.dumps({"a": 1}))
    assert resp["status"] == "SUCCEEDED"
    assert json.loads(resp["output"]) == {"ok": 1}


def test_invalid_definition_rejected(sfn, name, make_role) -> None:
    with pytest.raises(sfn.exceptions.InvalidDefinition):
        sfn.create_state_machine(
            name=name,
            definition=json.dumps({"StartAt": "Missing", "States": {}}),
            roleArn=make_role(["states.amazonaws.com"]),
        )
