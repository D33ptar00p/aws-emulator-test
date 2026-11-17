"""Small helpers shared by the per-service test suites."""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Callable, Mapping
from typing import TypeVar

from emulator_poc.net import execute_api_url, http_request

__all__ = ["execute_api_url", "http_request", "wait_for", "zip_code"]

T = TypeVar("T")


def zip_code(files: Mapping[str, str]) -> bytes:
    """Build an in-memory Lambda deployment package from {filename: source}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, source in files.items():
            zf.writestr(name, source)
    return buf.getvalue()


def wait_for(probe: Callable[[], T | None], timeout: float = 15.0, interval: float = 0.25) -> T:
    """Poll probe() until it returns a truthy value, or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"condition not met within {timeout}s")
        time.sleep(interval)
