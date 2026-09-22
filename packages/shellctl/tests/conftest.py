"""Run the SDK tests on the asyncio backend used by Kapy."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
