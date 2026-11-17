"""DynamoDB resource factory.

Endpoint, credentials, region and CA bundle all come from the standard AWS
environment variables (see scripts/emulator-env.sh), so the same code runs
unchanged against the emulator or real AWS.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import boto3
from botocore.config import Config

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_CONFIG = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)


class NotLocalEndpointError(RuntimeError):
    """Raised when code that must only touch the emulator is pointed elsewhere."""


def assert_local_endpoint() -> str:
    """Return AWS_ENDPOINT_URL, or raise if it does not point at localhost."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    host = urlparse(endpoint).hostname
    if host not in LOCAL_HOSTS:
        raise NotLocalEndpointError(
            f"AWS_ENDPOINT_URL={endpoint!r} is not a local endpoint; "
            "run `source scripts/emulator-env.sh` first."
        )
    return endpoint


def dynamodb_resource():  # noqa: ANN201 - boto3 resource types are dynamic
    """Create a DynamoDB service resource from the ambient AWS environment."""
    return boto3.Session().resource("dynamodb", config=_CONFIG)
