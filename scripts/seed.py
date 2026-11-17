"""Create a small, linked set of demo resources on the emulator so the console has something to show.

    source scripts/emulator-env.sh && uv run python scripts/seed.py

Safe to re-run: it skips anything that already exists. Refuses to run against a non-local endpoint.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from emulator_poc.client import NotLocalEndpointError, assert_local_endpoint
from emulator_poc.testing import zip_code

PREFIX = "demo"
TABLE = f"{PREFIX}-books"
BUCKET = f"{PREFIX}-library-assets"
FUNCTION = f"{PREFIX}-checkout-book"
QUEUE = f"{PREFIX}-library-events"
BUS = f"{PREFIX}-library"
MACHINE = f"{PREFIX}-checkout-workflow"
REST_API = f"{PREFIX}-books-api"
GRAPHQL_API = f"{PREFIX}-books-graphql"
EVENT_SOURCE = "demo.library"

# Public-domain titles: (title, author, genre, year)
BOOKS = [
    ("Pride and Prejudice", "Jane Austen", "fiction", 1813),
    ("Moby-Dick", "Herman Melville", "fiction", 1851),
    ("Frankenstein", "Mary Shelley", "science-fiction", 1818),
    ("The Time Machine", "H. G. Wells", "science-fiction", 1895),
    ("The Adventures of Sherlock Holmes", "Arthur Conan Doyle", "mystery", 1892),
    ("The Moonstone", "Wilkie Collins", "mystery", 1868),
    ("The Origin of Species", "Charles Darwin", "science", 1859),
    ("Walden", "Henry David Thoreau", "non-fiction", 1854),
    ("Little Women", "Louisa May Alcott", "fiction", 1868),
    ("Dracula", "Bram Stoker", "horror", 1897),
    ("Treasure Island", "Robert Louis Stevenson", "adventure", 1883),
    ("Twenty Thousand Leagues Under the Seas", "Jules Verne", "science-fiction", 1870),
]

HANDLER = f"""
import json, boto3
def handler(event, context):
    if "httpMethod" in event:  # API Gateway proxy
        items = boto3.resource("dynamodb").Table("{TABLE}").scan(Limit=20)["Items"]
        return {{"statusCode": 200, "headers": {{"content-type": "application/json"}},
                "body": json.dumps(items, default=str)}}
    book_id = event.get("book_id", "unknown")
    boto3.resource("dynamodb").Table("{TABLE}").update_item(
        Key={{"book_id": book_id}},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={{"#s": "status"}},
        ExpressionAttributeValues={{":s": "on_loan"}},
    )
    return {{"book_id": book_id, "status": "on_loan"}}
"""


def exists(fn) -> bool:
    try:
        fn()
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in {
            "ResourceNotFoundException",
            "NoSuchEntity",
            "404",
            "NoSuchBucket",
            "AWS.SimpleQueueService.NonExistentQueue",
            "StateMachineDoesNotExist",
        }:
            return False
        raise


def step(label: str) -> None:
    print(f"  - {label}")


def seed(session: boto3.Session) -> None:
    iam, ddb, s3 = session.client("iam"), session.resource("dynamodb"), session.client("s3")
    lam, sqs, events = session.client("lambda"), session.client("sqs"), session.client("events")
    sfn, apigw, appsync = (
        session.client("stepfunctions"),
        session.client("apigateway"),
        session.client("appsync"),
    )

    # IAM role shared by Lambda / Step Functions / AppSync (emulator enforces role policies).
    role_name = f"{PREFIX}-service-role"
    if not exists(lambda: iam.get_role(RoleName=role_name)):
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": ["lambda.amazonaws.com", "states.amazonaws.com", "appsync.amazonaws.com"]
                    },
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust))
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:Scan",
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                    ],
                    "Resource": "*",
                },
                {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": "*"},
            ],
        }
        iam.put_role_policy(RoleName=role_name, PolicyName="demo", PolicyDocument=json.dumps(policy))
    role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    step(f"IAM role {role_name}")

    # DynamoDB
    if not exists(lambda: ddb.meta.client.describe_table(TableName=TABLE)):
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "book_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "book_id", "KeyType": "HASH"}],
        ).wait_until_exists()
    table = ddb.Table(TABLE)
    for i, (title, author, genre, year) in enumerate(BOOKS):
        table.put_item(
            Item={
                "book_id": f"book-{i:03d}",
                "title": title,
                "author": author,
                "genre": genre,
                "published_year": year,
                "copies_available": i % 4,
                "rating": Decimal(str(round(3.5 + (i % 4) * 0.4, 1))),
                "status": ["available", "on_loan", "reserved"][i % 3],
                "tags": ["classic", "public-domain"] if i % 2 else ["classic"],
            }
        )
    step(f"DynamoDB table {TABLE} ({len(BOOKS)} items)")

    # S3
    if not exists(lambda: s3.head_bucket(Bucket=BUCKET)):
        s3.create_bucket(Bucket=BUCKET)
    for i, (title, author, _genre, _year) in enumerate(BOOKS[:4]):
        bid = f"book-{i:03d}"
        s3.put_object(
            Bucket=BUCKET,
            Key=f"catalog/{bid}/excerpt.txt",
            Body=f"{title} by {author}\n\n".encode() + b"Lorem ipsum dolor sit amet. " * 40,
            ContentType="text/plain",
        )
        s3.put_object(
            Bucket=BUCKET,
            Key=f"catalog/{bid}/meta.json",
            Body=json.dumps({"book_id": bid, "title": title, "author": author}).encode(),
            ContentType="application/json",
        )
    s3.put_object(
        Bucket=BUCKET, Key="README.txt", Body=b"Demo library assets bucket", ContentType="text/plain"
    )
    step(f"S3 bucket {BUCKET} (9 objects)")

    # Lambda
    if not exists(lambda: lam.get_function(FunctionName=FUNCTION)):
        lam.create_function(
            FunctionName=FUNCTION,
            Runtime="python3.12",
            Role=role_arn,
            Handler="index.handler",
            Timeout=15,
            Environment={"Variables": {"TABLE": TABLE}},
            Code={"ZipFile": zip_code({"index.py": HANDLER})},
        )
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
    fn_arn = lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]
    step(f"Lambda {FUNCTION}")

    # SQS + EventBridge (custom bus -> rule -> queue)
    try:
        queue_url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist:
        queue_url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    if not any(b["Name"] == BUS for b in events.list_event_buses()["EventBuses"]):
        events.create_event_bus(Name=BUS)
    rule_arn = events.put_rule(
        Name=f"{PREFIX}-book-returned",
        EventBusName=BUS,
        EventPattern=json.dumps({"source": [EVENT_SOURCE], "detail-type": ["BookReturned"]}),
    )["RuleArn"]
    sqs.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={
            "Policy": json.dumps(
                {
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
            )
        },
    )
    events.put_targets(
        Rule=f"{PREFIX}-book-returned", EventBusName=BUS, Targets=[{"Id": "q", "Arn": queue_arn}]
    )
    events.put_events(
        Entries=[
            {
                "EventBusName": BUS,
                "Source": EVENT_SOURCE,
                "DetailType": "BookReturned",
                "Detail": json.dumps({"book_id": "book-002", "member_id": "member-017"}),
            }
        ]
    )
    step(f"EventBridge bus {BUS} -> SQS {QUEUE} (1 event sent)")

    # Step Functions
    machines = {m["name"]: m["stateMachineArn"] for m in sfn.list_state_machines()["stateMachines"]}
    if MACHINE not in machines:
        definition = {
            "Comment": "Check a copy is available, then check the book out with Lambda",
            "StartAt": "CheckAvailability",
            "States": {
                "CheckAvailability": {
                    "Type": "Choice",
                    "Choices": [
                        {"Variable": "$.copies_available", "NumericGreaterThanEquals": 1, "Next": "Checkout"}
                    ],
                    "Default": "Rejected",
                },
                "Checkout": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {"FunctionName": fn_arn, "Payload.$": "$"},
                    "OutputPath": "$.Payload",
                    "End": True,
                },
                "Rejected": {"Type": "Fail", "Error": "NoCopiesAvailable"},
            },
        }
        machines[MACHINE] = sfn.create_state_machine(
            name=MACHINE, definition=json.dumps(definition), roleArn=role_arn
        )["stateMachineArn"]
        sfn.start_execution(
            stateMachineArn=machines[MACHINE],
            input=json.dumps({"book_id": "book-001", "copies_available": 2}),
        )
        sfn.start_execution(
            stateMachineArn=machines[MACHINE],
            input=json.dumps({"book_id": "book-004", "copies_available": 0}),
        )
    step(f"Step Functions {MACHINE} (2 executions)")

    # API Gateway REST -> Lambda proxy
    if not any(a["name"] == REST_API for a in apigw.get_rest_apis()["items"]):
        api_id = apigw.create_rest_api(name=REST_API, description="List books")["id"]
        root = next(r["id"] for r in apigw.get_resources(restApiId=api_id)["items"] if r["path"] == "/")
        res = apigw.create_resource(restApiId=api_id, parentId=root, pathPart="books")["id"]
        apigw.put_method(restApiId=api_id, resourceId=res, httpMethod="GET", authorizationType="NONE")
        apigw.put_integration(
            restApiId=api_id,
            resourceId=res,
            httpMethod="GET",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=f"arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/{fn_arn}/invocations",
        )
        lam.add_permission(
            FunctionName=FUNCTION,
            StatementId="apigw",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
        )
        apigw.create_deployment(restApiId=api_id, stageName="dev")
    step(f"API Gateway {REST_API} (GET /books -> Lambda)")

    # AppSync (control plane only - the emulator doesn't serve GraphQL queries)
    if not any(a["name"] == GRAPHQL_API for a in appsync.list_graphql_apis()["graphqlApis"]):
        api = appsync.create_graphql_api(name=GRAPHQL_API, authenticationType="API_KEY")["graphqlApi"]
        appsync.create_api_key(apiId=api["apiId"], description="demo")
        appsync.start_schema_creation(
            apiId=api["apiId"],
            definition=b"type Book { book_id: ID! title: String author: String status: String }\n"
            b"type Query { getBook(book_id: ID!): Book }\nschema { query: Query }",
        )
        appsync.create_data_source(
            apiId=api["apiId"],
            name="books",
            type="AMAZON_DYNAMODB",
            serviceRoleArn=role_arn,
            dynamodbConfig={"tableName": TABLE, "awsRegion": "us-east-1"},
        )
    step(f"AppSync {GRAPHQL_API}")


def main() -> int:
    try:
        endpoint = assert_local_endpoint()
        print(f"Seeding demo resources on {endpoint}")
        seed(boto3.Session())
        print("Done. Open the console: http://127.0.0.1:8080")
        return 0
    except (ClientError, NotLocalEndpointError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
