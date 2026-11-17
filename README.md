# AWS Emulator POC

This POC evaluates [AWS Emulator](https://github.com/aws/aws-emulator), a local emulator of the AWS APIs
([announcement](https://aws.amazon.com/blogs/aws/test-your-applications-against-aws-locally-with-aws-emulator/),
[docs](https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html)), as a way to develop
and test AWS workloads locally and in CI. It needs no AWS account and no real credentials.

This repo runs the **emulator** and the **test suites**.

| Service | Tests | Result | Data plane works locally? |
|---|---|---|---|
| [DynamoDB](#dynamodb) | 16 | 15 pass, 1 xfail | Yes |
| [S3](#s3) | 12 | 12 pass | Yes (incl. presigned URLs) |
| [Lambda](#lambda) | 10 | 10 pass | Yes (embedded interpreter) |
| [API Gateway](#api-gateway) | 8 | 8 pass | Yes, via `/_aws/execute-api/...` |
| [Step Functions](#step-functions) | 9 | 9 pass | Yes |
| [EventBridge](#eventbridge) | 6 | 6 pass | Yes (SQS and Lambda targets) |
| [AppSync](#appsync) | 7 | 5 pass, 2 xfail | **No** (no GraphQL endpoint) |
| **Total** | **68** | **65 pass, 3 xfail** | |

These results are against emulator `v1.0.322`. The full suite runs in about 2 minutes.

## Quick start

Prerequisites: Docker, `uv`, `curl`. The AWS CLI is used by `make smoke` and for manual inspection.

```bash
make up                     # build image (first time), start the emulator on 127.0.0.1:9297, wait for health
make seed                   # create linked demo resources in every POC service (safe to re-run)
make test                   # all suites -> reports/report.html + reports/junit.xml
make test SERVICE=lambda    # one suite: dynamodb | s3 | lambda | apigateway | stepfunctions | eventbridge | appsync
make report                 # open the last HTML report
make smoke                  # AWS CLI check: STS identity + list resources in every POC service
make demo                   # scripted DynamoDB walkthrough (demo.py)
make down                   # stop the emulator(s); all emulated state is discarded
make rebuild                # rebuild the emulator image (latest LTS package); takes effect on next start
```

To use the AWS CLI or an SDK against the emulator from your own shell:

```bash
source scripts/emulator-env.sh            # default emulator (or: source scripts/emulator-env.sh eu-west-2)
aws sts get-caller-identity               # account 000000000000
aws dynamodb list-tables
```

`emulator-env.sh` sets `AWS_ENDPOINT_URL`, the per-run credentials and config files, `AWS_CA_BUNDLE` (a
self-signed cert) and the region. It also **unsets any real AWS credentials or profile** in that shell.

## Emulator state: what keeps it and what wipes it

The emulator keeps everything **in memory**. There's no persistence, snapshot or reset API. Resources live exactly as
long as the emulator container runs, and every new run starts empty.

| Keeps your resources | Wipes them |
|---|---|
| `make up` again (an emulator that's already running is reused) | `make down` |
| `make seed`, `make test`, `make smoke`, `make demo` (tests delete only what they create) | `docker stop aws-emulator-9297`, restarting Docker Desktop, rebooting |
| Starting or stopping other containers on the `aws-emulator` network | The container exceeding its 2 GiB memory limit |
| `make rebuild` (it doesn't restart a running emulator) | `make down && make up` after a rebuild |

To get resources back after a restart, recreate them: `make seed` for the demo set, or your own setup script or
IaC run against the emulator. The emulator has no `--idle-timeout` set here, so it never stops on its own.

**`make seed`** creates a linked set (all prefixed `demo-`):
- a DynamoDB table `demo-books` with 12 library books;
- an S3 bucket with 9 objects;
- a Lambda function;
- a REST API with `GET /books` routed to that Lambda;
- a Step Functions state machine (Choice step, then a Lambda task) with 2 executions;
- an EventBridge bus and rule routing to an SQS queue, with 1 event sent;
- an AppSync API with a DynamoDB data source;
- the IAM role they share.

## Regions

There are two ways to work with more than one region:

| Mode | Command | Behaviour |
|---|---|---|
| **Shared** (default) | `make up` | One emulator on `:9297`. The region is sent on every call. Many services keep resources per region in one emulator; SQS and DynamoDB were verified. AWS documents that this isn't guaranteed for every service, and global services such as IAM, and S3 bucket listings, are the same in every region. |
| **Isolated** | `make up REGIONS=us-east-1,eu-west-2` | **One emulator per region.** Region *i* gets port `9297+i`. They are fully separate, verified with SQS. Regions not in the list use the first emulator. Each emulator can use up to 2 GiB of memory. |

`make up REGIONS=...` writes the region-to-port map to `~/.aws/emulator/regions` (one `region=port` per line).
`source scripts/emulator-env.sh <region>` uses it.

## Containers and network

Each emulator is a standalone container named `aws-emulator-<port>`, labelled `aws-emulator-poc.role=emulator`. It:
- publishes its own `127.0.0.1:<port>`;
- joins the Docker network `aws-emulator`, created on first `make up`, where other containers can reach it as
  `aws-emulator-<port>:<port>`;
- writes its per-run files to `~/.aws/emulator/<port>/`: `emulator_credentials`, `config` and `ca.pem`. The emulator
  deletes them on a clean stop.

`make up` and `make down` only ever start and stop these emulator containers.

## How it works

```
docker/Dockerfile            official AWS Dockerfile (AL2023 + LTS RPM), x86_64/aarch64
scripts/emulator-up.sh       emulator container(s): localhost-only ports, your UID, telemetry off,
                             shared network `aws-emulator`, health wait, region map
scripts/emulator-down.sh     graceful stop of emulator containers only (emulator deletes its per-run creds/cert)
scripts/emulator-env.sh      `source [region]` to point CLI/SDKs at the emulator; unsets real AWS creds
scripts/seed.py              linked demo resources (`make seed`)
demo.py                      DynamoDB CaptureRepository walkthrough (`make demo`)
src/emulator_poc/
  client.py                  env-driven boto3 factory + assert_local_endpoint() guard
  net.py                     http_request (trusts emulator CA), execute_api_url
  testing.py                 zip_code, wait_for (+ re-exports net helpers)
  captures.py                CaptureRepository - sample data layer used by the DynamoDB suite + demo
tests/
  conftest.py                shared fixtures (below)
  <service>/test_<service>.py
```

The shared fixtures live in `tests/conftest.py`, so any suite can use them:

| Fixture | Purpose |
|---|---|
| `aws` | A boto3 session. **The session aborts unless the endpoint is localhost and `/__admin/health` is OK**, so tests can never hit real AWS. |
| `name` | A unique resource-name prefix, so tests can share one emulator without colliding. |
| `cleanup` | Teardown callbacks, run in reverse order on a best-effort basis. |
| `make_role(services, actions)` | Creates an IAM role with a trust policy and an optional inline policy. |
| `make_function(source, runtime=, actions=, env=)` | Creates an ACTIVE Lambda function from inline source. |

Application code gets its endpoint, credentials, region and CA bundle from the standard `AWS_*` environment variables.
The same code therefore runs unchanged against the emulator or against real AWS.

**Test results.** `make test` writes a self-contained HTML report to `reports/report.html` and a JUnit XML file to
`reports/junit.xml` for CI test reporters.

## Services

### DynamoDB
`tests/dynamodb/`. It exercises `CaptureRepository`, a small typed data layer.
- Covers: CreateTable with a GSI, waiters, conditional Put (no overwrite), Get, Delete, and paginated Query on the GSI. Also a conditional status state machine via UpdateItem, `batch_writer` with more than 25 items, TransactWriteItems (capture plus quota), TTL, and typed errors.
- Finding: in a transaction against a **missing** item, the emulator evaluates the UpdateExpression before the ConditionExpression. It returns `ValidationException` where AWS returns `ConditionalCheckFailed`. This is a strict xfail and is not yet confirmed on real AWS.

### S3
`tests/s3/`
- Covers: buckets, objects with metadata, `NoSuchKey`/`NoSuchBucket`, prefix pagination, multipart upload through the transfer manager, presigned PUT/GET, unsigned access rejection, copy/delete, versioning, object tags, and default SSE.
- Presigned URLs are generated path-style on `127.0.0.1:9297` and work as-is.

### Lambda
`tests/lambda/`
- Covers: sync invoke, `FunctionError` on unhandled exceptions, async (202) and DryRun (204) invokes, environment variables, the Node.js runtime, update code, versions and aliases, missing-function errors, and pagination.
- Covers: function code calling DynamoDB through the SDK. Calls inside the function reach the emulator automatically.
- **Finding:** code runs in an **embedded interpreter**, not real Lambda runtimes. Python reports `rustpython` 3.14-alpha and Node reports `v20.0.0`. Pure-Python and pure-JS handlers work. Native extensions (numpy, cv2, compiled wheels) and container-image functions will not behave as they do on AWS.
- **Finding:** the **execution role's identity policy is enforced.** A function without `dynamodb:PutItem` gets `AccessDeniedException`.

### API Gateway
`tests/apigateway/`
- REST API (v1): a Lambda proxy with path parameters and `ANY`, `TestInvokeMethod`, a MOCK integration, and the AWS-style `403 Missing Authentication Token` for unknown routes.
- HTTP API (v2): a quick-create Lambda proxy, and explicit routes with integrations and a `$default` stage with auto-deploy.
- Local invoke URL: `https://127.0.0.1:9297/_aws/execute-api/{api-id}/{stage}/{path}`. `execute_api_url()` builds it. On AWS the URL is `https://{api-id}.execute-api.{region}.amazonaws.com/...`.

### Step Functions
`tests/stepfunctions/`
- Covers: Pass/Choice/Fail, execution history, a Map state over Lambda, Retry/Catch, the optimized integrations `lambda:invoke` and `dynamodb:putItem`, Express `StartSyncExecution`, and `InvalidDefinition`.
- **Finding:** the state machine's role trust policy and inline policy are enforced. A task fails with "not authorized" if the role lacks `lambda:InvokeFunction`.

### EventBridge
`tests/eventbridge/`
- Covers: custom buses, `TestEventPattern`, routing to SQS (only matching events arrive), input transformers, prefix and numeric content filters, disabled rules, and the chain EventBridge -> Lambda -> DynamoDB.
- As on AWS, an SQS target needs a queue resource policy that allows `events.amazonaws.com`. Without it, nothing is delivered.

### AppSync
`tests/appsync/`
- Covers: GraphQL API, schema creation, API key lifecycle, `GetType`, a DynamoDB data source with an `APPSYNC_JS` resolver, and resolver logic run through `EvaluateCode`.
- **Gap:** the GraphQL data plane is not served. `uris.GRAPHQL` points at a real `*.appsync-api.amazonaws.com` host, so queries can't be run locally (strict xfail).
- **Gap:** `ListTypes` returns `501 NotImplemented` (strict xfail).

## What is not supported

### Operations returning `501 NotImplemented`

Source: the emulator's `v1.0.322` fidelity run (`scores.json.gz`) and `issues/things-that-do-not-work/` in the upstream repo.

| Service | Not implemented (501 / stub) |
|---|---|
| DynamoDB | `ImportTable`, `ExportTableToPointInTime`, `CreateGlobalTable` (legacy global tables) |
| S3 | `RenameObject`, `DeleteBucketMetadataConfiguration`, `UpdateBucketMetadataAnnotationTableConfiguration` |
| API Gateway (REST) | `UpdateUsage`, `TestInvokeAuthorizer` |
| API Gateway v2 | `ExportApi`, `TagResource`, `GetTags`, `UpdateDeployment`, `UpdateModel` |
| AppSync | `ListTypes`, `ListTypesByAssociation`, `GetIntrospectionSchema`, `EvaluateMappingTemplate` (VTL), `StartDataSourceIntrospection`, `GetDataSourceIntrospection`, `CreateDomainName` |
| Lambda, Step Functions, EventBridge | No 501s for core operations in the fidelity run |

### Functional gaps verified in this POC

| Service | Gap | Workaround |
|---|---|---|
| AppSync | No local GraphQL endpoint | Test resolvers with `EvaluateCode`. Run end-to-end GraphQL tests in a sandbox account. |
| Lambda | Embedded interpreter, not real runtimes | Keep handlers pure-Python or pure-JS for local tests. Test native dependencies on AWS. |
| DynamoDB | Condition/update evaluation order on a missing item | Make sure the item exists first, or handle both error types. |
| All | The emulator can't be limited to a subset of services; every run serves all 436 | None needed: unused services cost nothing until resources are created. |

### Platform-wide limitations (from the AWS docs)

| Area | Limitation |
|---|---|
| IAM | The emulator doesn't evaluate IAM or resource policies **the way AWS does**. The test caller is unrestricted. Execution-role policies *were* enforced for Lambda and Step Functions in this POC. |
| Accounts / Regions | Single account `000000000000`. In one emulator, region separation varies by service. Use `make up REGIONS=...` for guaranteed isolation (see [Regions](#regions)). |
| Persistence | State is in memory only and is lost on stop (see [Emulator state](#emulator-state-what-keeps-it-and-what-wipes-it)). Not tested for long-running use or load testing. |
| IaC | CloudFormation fidelity is low (18%). CDK, CFN and Terraform deploys against the emulator are unreliable. |

### Fidelity scores (`v1.0.322`, tests passed / total)

"Wire" counts single-service API tests. "All" also includes cross-service scenarios that involve the service.

| Service (in this POC) | Wire | All | | Other AWS services | All |
|---|---|---|---|---|---|
| DynamoDB | 151/243 | 1153/1694 (68%) | | EC2 | 958/2470 (38%) |
| S3 | 202/296 | 1763/2814 (62%) | | EC2 Image Builder | 43/119 (36%) |
| Lambda | 72/171 | 1561/3006 (51%) | | CloudFront | 428/817 (52%) |
| API Gateway REST | 146/200 | 762/1211 (62%) | | WAFv2 | 197/269 (73%) |
| API Gateway v2 | 29/107 | 195/401 (48%) | | RDS | 309/759 (40%) |
| Step Functions | 52/68 | 687/982 (69%) | | CodeArtifact | 81/132 (61%) |
| EventBridge | 63/75 | 690/966 (71%) | | IAM | 709/1223 (57%) |
| EventBridge Scheduler | 4/17 | 96/188 (51%) | | SQS / SNS | 67% / 69% |
| EventBridge Pipes | 4/18 | 132/170 (72%) | | KMS / Secrets Manager | 71% / 68% |
| AppSync | 12/91 | 155/510 (30%) | | CloudFormation | 795/4206 (18%) |

A low score does not mean the service is unusable. It means there are differences in some responses or errors, so check that your specific operations work. Each release publishes fidelity scores on the
[releases page](https://github.com/aws/aws-emulator/releases).

## Adding a service suite

1. Create `tests/<service>/test_<service>.py`. Test file basenames must be unique, because pytest uses `--import-mode=importlib`.
2. Use the shared fixtures `aws`, `name`, `cleanup`, `make_role` and `make_function`. Register teardown in `cleanup`.
3. Mark known emulator gaps with `@pytest.mark.xfail(strict=True, reason=...)`. They will then fail loudly once fixed upstream.
4. Run it with `make test SERVICE=<service>`.

## Security and compliance notes (SOC 2 / ISO 27001)

- Don't use the emulator to test security posture. Test least-privilege policies in a real sandbox account. Role enforcement shows up in some paths, but it isn't AWS-equivalent.
- Ports are bound to `127.0.0.1` only. Never use `-p 9297:9297`, which exposes the emulator to your network. The shared Docker network `aws-emulator` is internal to Docker.
- **Telemetry is off by default**, both in the image (`ENV EMULATOR_TELEMETRY=off` in `docker/Dockerfile`, so any `docker run` of it is off) and in `emulator-up.sh`. To opt in: `EMULATOR_TELEMETRY=on make up`.
- `emulator-env.sh` unsets real credentials, and the pytest session aborts unless the endpoint is local.

## Next steps

- Add a GitHub Actions job that installs the `.deb` natively, waits on `/__admin/health`, runs `make test` and uploads `reports/junit.xml`.
- Add suites for other services, such as SQS/SNS, Secrets Manager and CodeArtifact. Check each one's fidelity score first.
- Confirm the DynamoDB evaluation-order finding against real AWS, then file it upstream.
- Compare with our current local-testing tools (moto, LocalStack).
