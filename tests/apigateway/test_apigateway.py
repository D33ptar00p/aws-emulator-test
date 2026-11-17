"""API Gateway: REST API (v1) and HTTP API (v2) fronting Lambda, invoked over the local data plane."""

from __future__ import annotations

import json

import pytest

from emulator_poc.testing import execute_api_url, http_request

HANDLER = """
import json
def handler(event, context):
    if event.get("rawPath"):  # HTTP API payload v2
        path, method = event["rawPath"], event["requestContext"]["http"]["method"]
    else:  # REST API proxy payload v1
        path, method = event["path"], event["httpMethod"]
    return {
        "statusCode": 201 if method == "POST" else 200,
        "headers": {"content-type": "application/json", "x-poc": "1"},
        "body": json.dumps({"path": path, "method": method, "qs": event.get("queryStringParameters")}),
    }
"""


@pytest.fixture
def apigw(aws):
    return aws.client("apigateway")


@pytest.fixture
def function_arn(aws, make_function) -> str:
    conf = make_function(HANDLER)
    aws.client("lambda").add_permission(
        FunctionName=conf["FunctionName"],
        StatementId="apigw",
        Action="lambda:InvokeFunction",
        Principal="apigateway.amazonaws.com",
    )
    return conf["FunctionArn"]


@pytest.fixture
def rest_api(apigw, name, function_arn, cleanup) -> dict:
    """REST API: GET /captures and ANY /captures/{id} -> Lambda proxy, deployed to stage 'dev'."""
    api_id = apigw.create_rest_api(name=name, endpointConfiguration={"types": ["REGIONAL"]})["id"]
    cleanup.append(lambda: apigw.delete_rest_api(restApiId=api_id))
    root = next(r["id"] for r in apigw.get_resources(restApiId=api_id)["items"] if r["path"] == "/")
    captures = apigw.create_resource(restApiId=api_id, parentId=root, pathPart="captures")["id"]
    by_id = apigw.create_resource(restApiId=api_id, parentId=captures, pathPart="{id}")["id"]
    uri = f"arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/{function_arn}/invocations"
    for resource, method in ((captures, "GET"), (by_id, "ANY")):
        apigw.put_method(restApiId=api_id, resourceId=resource, httpMethod=method, authorizationType="NONE")
        apigw.put_integration(
            restApiId=api_id,
            resourceId=resource,
            httpMethod=method,
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=uri,
        )
    apigw.create_deployment(restApiId=api_id, stageName="dev")
    return {"id": api_id, "captures": captures, "by_id": by_id}


def test_rest_api_resources(apigw, rest_api) -> None:
    paths = sorted(r["path"] for r in apigw.get_resources(restApiId=rest_api["id"])["items"])
    assert paths == ["/", "/captures", "/captures/{id}"]


def test_rest_test_invoke_method(apigw, rest_api) -> None:
    resp = apigw.test_invoke_method(
        restApiId=rest_api["id"], resourceId=rest_api["captures"], httpMethod="GET"
    )
    assert resp["status"] == 200
    assert json.loads(resp["body"])["path"] == "/captures"


def test_rest_invoke_over_data_plane(rest_api) -> None:
    status, body = http_request(execute_api_url(rest_api["id"], "dev/captures?user=u1"))
    assert status == 200
    assert json.loads(body) == {"path": "/captures", "method": "GET", "qs": {"user": "u1"}}


def test_rest_path_parameter_and_any_method(rest_api) -> None:
    status, body = http_request(execute_api_url(rest_api["id"], "dev/captures/cap-9"), method="POST", body={})
    assert status == 201
    assert json.loads(body)["path"] == "/captures/cap-9"


def test_rest_unknown_route_is_403_like_aws(rest_api) -> None:
    status, body = http_request(execute_api_url(rest_api["id"], "dev/nope"))
    assert status == 403
    assert b"Missing Authentication Token" in body


def test_rest_mock_integration(apigw, name, cleanup) -> None:
    api_id = apigw.create_rest_api(name=f"{name}-mock")["id"]
    cleanup.append(lambda: apigw.delete_rest_api(restApiId=api_id))
    root = next(r["id"] for r in apigw.get_resources(restApiId=api_id)["items"] if r["path"] == "/")
    res = apigw.create_resource(restApiId=api_id, parentId=root, pathPart="health")["id"]
    apigw.put_method(restApiId=api_id, resourceId=res, httpMethod="GET", authorizationType="NONE")
    apigw.put_integration(
        restApiId=api_id,
        resourceId=res,
        httpMethod="GET",
        type="MOCK",
        requestTemplates={"application/json": '{"statusCode": 200}'},
    )
    apigw.put_method_response(restApiId=api_id, resourceId=res, httpMethod="GET", statusCode="200")
    apigw.put_integration_response(
        restApiId=api_id,
        resourceId=res,
        httpMethod="GET",
        statusCode="200",
        responseTemplates={"application/json": '{"status": "ok"}'},
    )
    apigw.create_deployment(restApiId=api_id, stageName="v1")
    status, body = http_request(execute_api_url(api_id, "v1/health"))
    assert (status, json.loads(body)) == (200, {"status": "ok"})


def test_http_api_quick_create_proxies_to_lambda(aws, name, function_arn, cleanup) -> None:
    v2 = aws.client("apigatewayv2")
    api = v2.create_api(Name=name, ProtocolType="HTTP", Target=function_arn)
    cleanup.append(lambda: v2.delete_api(ApiId=api["ApiId"]))

    status, body = http_request(execute_api_url(api["ApiId"], "captures/c1"))
    assert status == 200
    assert json.loads(body) == {"path": "/captures/c1", "method": "GET", "qs": None}


def test_http_api_explicit_route(aws, name, function_arn, cleanup) -> None:
    v2 = aws.client("apigatewayv2")
    api_id = v2.create_api(Name=name, ProtocolType="HTTP")["ApiId"]
    cleanup.append(lambda: v2.delete_api(ApiId=api_id))
    integ = v2.create_integration(
        ApiId=api_id, IntegrationType="AWS_PROXY", IntegrationUri=function_arn, PayloadFormatVersion="2.0"
    )["IntegrationId"]
    v2.create_route(ApiId=api_id, RouteKey="POST /captures", Target=f"integrations/{integ}")
    v2.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)

    status, body = http_request(execute_api_url(api_id, "captures"), method="POST", body={"a": 1})
    assert status == 201
    assert json.loads(body)["method"] == "POST"
    assert http_request(execute_api_url(api_id, "other"))[0] == 404
