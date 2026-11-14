"""Shared fixtures."""

from __future__ import annotations

import pytest

from webless import ENGINE_HEALTH
from webless._engines import _NEXT_SLOT, SEARXNG_ENV


@pytest.fixture(autouse=True)
def fresh_engine_health():
    """Start every test with no engine paused.

    Engine health is process-wide by design, so without this a test that
    blocks a fake engine would leave it paused for whichever test runs next.
    """
    ENGINE_HEALTH.reset()
    yield
    ENGINE_HEALTH.reset()


@pytest.fixture(autouse=True)
def no_ambient_configuration(monkeypatch):
    """Keep the developer's environment and earlier tests out of each test.

    A SearXNG server set in the shell would add an engine to every search,
    and request spacing is process-wide, so a test that queried arXiv would
    make the next one wait three seconds.
    """
    monkeypatch.delenv(SEARXNG_ENV, raising=False)
    _NEXT_SLOT.clear()
    yield
    _NEXT_SLOT.clear()
