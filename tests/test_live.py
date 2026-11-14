"""Live checks against the real engines. Opt-in: ``pytest -m live``.

The offline suite runs against saved result pages, so it cannot see an engine
change its markup or start blocking this client -- the two ways engines
actually break. These tests ask the real endpoints.

An engine refusing this network outright (a challenge, 401/403/429, or a
timeout) is skipped with the reason rather than failed: that is a property of
the IP the tests run from, not of this code. Everything else that is not a
clean, on-topic answer fails, including an empty page and decoy results,
because from outside those cannot be told apart from a parser that no longer
matches the engine's markup.
"""

from __future__ import annotations

import pytest

from webless import ALL_ENGINES, aclose, alignment, check_engine, fetch, query_terms, search
from webless._doctor import PROBE_QUERY

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
async def close_shared_client():
    """Release each test loop's connections rather than leaving them to GC."""
    yield
    await aclose()


@pytest.mark.parametrize("engine", ALL_ENGINES, ids=lambda cls: cls.name)
async def test_engine_answers_the_probe_query(engine) -> None:
    check = await check_engine(engine)
    if check.status in ("blocked", "timeout"):
        pytest.skip(f"{check.engine} is refusing this network: {check.detail}")
    assert check.ok, (
        f"{check.engine} is {check.status}: {check.detail} "
        f"({check.hits} hits, {check.on_topic:.0%} on topic; top: {check.sample})"
    )


async def test_search_returns_an_on_topic_ranking() -> None:
    result = await search(PROBE_QUERY)
    assert result.ok, f"every engine failed: {result.failed}"
    assert result.hits, f"no hits; failures: {result.failed}"
    assert alignment(query_terms(PROBE_QUERY), result[0]) >= 0.5, result[0]


async def test_fetch_renders_a_real_page() -> None:
    page = await fetch("https://www.sqlite.org/wal.html")
    assert page.status == 200
    assert "Write-Ahead Log" in page.title
    assert "WAL" in page.content


async def test_fetch_reads_a_stack_overflow_question() -> None:
    # The page itself answers 403; this goes through the API.
    page = await fetch("https://stackoverflow.com/questions/231767")
    assert "yield" in page.title.lower()
    assert "Accepted answer" in page.content or "Answer (score" in page.content
