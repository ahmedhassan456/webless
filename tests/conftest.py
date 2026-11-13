"""Shared fixtures."""

from __future__ import annotations

import pytest

from webless import ENGINE_HEALTH


@pytest.fixture(autouse=True)
def fresh_engine_health():
    """Start every test with no engine paused.

    Engine health is process-wide by design, so without this a test that
    blocks a fake engine would leave it paused for whichever test runs next.
    """
    ENGINE_HEALTH.reset()
    yield
    ENGINE_HEALTH.reset()
