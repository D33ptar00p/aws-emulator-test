from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from emulator_poc.captures import CaptureRepository


@pytest.fixture
def repo(dynamodb) -> Iterator[CaptureRepository]:
    """Fresh, uniquely-named tables per test so tests stay isolated on a shared emulator."""
    suffix = uuid.uuid4().hex[:8]
    repository = CaptureRepository(dynamodb, f"captures-{suffix}", f"quotas-{suffix}")
    repository.create_tables()
    yield repository
    repository.delete_tables()
