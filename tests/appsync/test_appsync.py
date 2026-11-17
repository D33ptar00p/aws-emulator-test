"""AppSync: control plane (APIs, schema, keys, data sources, JS resolvers) + code evaluation.

The GraphQL *data plane* is not served locally yet: GraphqlApi.uris point at real
*.appsync-api.<region>.amazonaws.com hosts the emulator does not answer (tracked upstream).
"""

from __future__ import annotations

import json

import pytest

from emulator_poc.testing import http_request, wait_for

SCHEMA = b"""
type Capture { id: ID!  name: String  status: String }
type Query { getCapture(id: ID!): Capture  hello: String }
type Mutation { addCapture(id: ID!, name: String): Capture }
schema { query: Query  mutation: Mutation }
"""

JS_RUNTIME = {"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"}

DDB_GET_RESOLVER = """
import { util } from '@aws-appsync/utils';
export function request(ctx) {
  return { operation: 'GetItem', key: util.dynamodb.toMapValues({ id: ctx.arguments.id }) };
}
export function response(ctx) { return ctx.result; }
"""


@pytest.fixture
def appsync(aws):
    return aws.client("appsync")


@pytest.fixture
def api(appsync, name, cleanup) -> dict:
    api = appsync.create_graphql_api(name=name, authenticationType="API_KEY")["graphqlApi"]
    cleanup.append(lambda: appsync.delete_graphql_api(apiId=api["apiId"]))
    appsync.start_schema_creation(apiId=api["apiId"], definition=SCHEMA)
    status = wait_for(
        lambda: (s := appsync.get_schema_creation_status(apiId=api["apiId"])["status"]) != "PROCESSING" and s
    )
    assert status == "SUCCESS"
    return api


def test_api_is_listed_and_has_uris(appsync, api) -> None:
    ids = [a["apiId"] for a in appsync.list_graphql_apis()["graphqlApis"]]
    assert api["apiId"] in ids
    assert set(api["uris"]) == {"GRAPHQL", "REALTIME"}


def test_api_key_lifecycle(appsync, api) -> None:
    key = appsync.create_api_key(apiId=api["apiId"], description="ci")["apiKey"]
    assert [k["id"] for k in appsync.list_api_keys(apiId=api["apiId"])["apiKeys"]] == [key["id"]]
    appsync.delete_api_key(apiId=api["apiId"], id=key["id"])
    assert appsync.list_api_keys(apiId=api["apiId"])["apiKeys"] == []


def test_introspected_schema_has_types(appsync, api) -> None:
    capture = appsync.get_type(apiId=api["apiId"], typeName="Capture", format="SDL")["type"]
    assert "status: String" in capture["definition"]


def test_dynamodb_data_source_and_js_resolver(appsync, api, dynamodb, name, make_role, cleanup) -> None:
    table = dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    table.wait_until_exists()
    cleanup.append(table.delete)
    role = make_role(["appsync.amazonaws.com"], ["dynamodb:GetItem", "dynamodb:PutItem"])
    appsync.create_data_source(
        apiId=api["apiId"],
        name="captures",
        type="AMAZON_DYNAMODB",
        serviceRoleArn=role,
        dynamodbConfig={"tableName": name, "awsRegion": "us-east-1"},
    )
    appsync.create_resolver(
        apiId=api["apiId"],
        typeName="Query",
        fieldName="getCapture",
        dataSourceName="captures",
        runtime=JS_RUNTIME,
        code=DDB_GET_RESOLVER,
    )
    resolver = appsync.get_resolver(apiId=api["apiId"], typeName="Query", fieldName="getCapture")["resolver"]
    assert resolver["dataSourceName"] == "captures"
    assert resolver["runtime"]["name"] == "APPSYNC_JS"
    sources = [d["name"] for d in appsync.list_data_sources(apiId=api["apiId"])["dataSources"]]
    assert sources == ["captures"]


def test_evaluate_code_runs_resolver_logic(appsync) -> None:
    resp = appsync.evaluate_code(
        runtime=JS_RUNTIME,
        code="export function request(ctx) { return { id: ctx.arguments.id, upper: ctx.arguments.id.toUpperCase() }; }\n"
        "export function response(ctx) { return ctx.result; }",
        context=json.dumps({"arguments": {"id": "cap-1"}}),
        function="request",
    )
    assert "error" not in resp, resp.get("error")
    assert json.loads(resp["evaluationResult"]) == {"id": "cap-1", "upper": "CAP-1"}


# ---- Known gaps (strict xfail: these start failing loudly once the emulator fixes them) -------


@pytest.mark.xfail(strict=True, reason="GraphQL data plane not served locally; uris point at real AWS hosts.")
def test_graphql_query_over_data_plane(appsync, api) -> None:
    appsync.create_data_source(apiId=api["apiId"], name="none", type="NONE")
    appsync.create_resolver(
        apiId=api["apiId"],
        typeName="Query",
        fieldName="hello",
        dataSourceName="none",
        runtime=JS_RUNTIME,
        code="export function request(ctx) { return { payload: 'hi' }; }\n"
        "export function response(ctx) { return ctx.result; }",
    )
    key = appsync.create_api_key(apiId=api["apiId"])["apiKey"]["id"]
    status, body = http_request(
        api["uris"]["GRAPHQL"],
        method="POST",
        body={"query": "{ hello }"},
        headers={"x-api-key": key, "content-type": "application/json"},
    )
    assert (status, json.loads(body)) == (200, {"data": {"hello": "hi"}})


@pytest.mark.xfail(strict=True, reason="ListTypes returns 501 NotImplemented in emulator v1.0.322.")
def test_list_types(appsync, api) -> None:
    names = {t["name"] for t in appsync.list_types(apiId=api["apiId"], format="SDL")["types"]}
    assert {"Capture", "Query", "Mutation"} <= names
