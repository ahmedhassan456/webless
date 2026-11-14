"""A live check of every engine, for finding out which ones still work.

The test suite runs against saved result pages, so it cannot notice the two
ways engines actually break: they change their markup, or they start blocking
this client. Both only show up against the real endpoint. `doctor` queries
each engine directly -- bypassing the cooldown `search` applies, and leaving
its record untouched -- and reports what came back and whether it was about
the query.

Each engine is asked its own `probe` query, one it should always have an
answer for: a general web engine and a medical literature index do not share
a question both can be judged on.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from ._engines import (
    ALL_ENGINES,
    DEFAULT_PROBE,
    ON_TOPIC_ALIGNMENT,
    SEARXNG_ENV,
    Engine,
    alignment,
    decoy_block,
    is_block,
    looks_like_decoy,
    query_terms,
    searxng,
)
from ._http import WebError

# The general engines' probe: a technical subject with pages on the major
# indexes, a Wikipedia article, small-web documentation, and Hacker News
# threads. Topic engines each name their own (`Engine.probe`).
PROBE_QUERY = DEFAULT_PROBE

STATUSES = ("ok", "blocked", "decoy", "empty", "timeout", "error")


@dataclass(slots=True)
class EngineCheck:
    """What one engine did with the probe query.

    `status` is one of:

    - ``ok`` -- answered with results about the query
    - ``blocked`` -- refused: a challenge page or a 401, 403, or 429
    - ``decoy`` -- answered with results that ignore the query
    - ``empty`` -- answered with no results: a soft block, or markup that
      the parser no longer recognises
    - ``timeout`` -- did not answer in time
    - ``error`` -- anything else, such as a network failure or a 5xx
    """

    engine: str
    status: str
    seconds: float
    query: str = ""
    hits: int = 0
    on_topic: float = 0.0
    detail: str = ""
    sample: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the engine answered the query."""
        return self.status == "ok"


async def check_engine(
    engine: type[Engine], query: str | None = None, *, timeout: float = 12.0
) -> EngineCheck:
    """Query one engine -- with its own probe unless `query` is given -- and
    classify what came back."""
    instance = engine()
    query = query or instance.probe
    started = time.monotonic()

    def done(status: str, **fields) -> EngineCheck:
        return EngineCheck(
            engine=instance.name,
            status=status,
            seconds=round(time.monotonic() - started, 2),
            query=query,
            **fields,
        )

    # An engine that spaces its requests may wait for a slot; that wait is
    # not the engine being slow.
    budget = timeout + 2 + instance.min_interval
    try:
        hits = await asyncio.wait_for(instance.search(query, 10, timeout), timeout=budget)
    except asyncio.TimeoutError:
        return done("timeout", detail=f"timed out after {timeout:g}s")
    except WebError as exc:
        if is_block(exc):
            # A block with no status is a 200 that parsed to nothing.
            status = "blocked" if exc.status is not None else "empty"
        else:
            status = "error"
        return done(status, detail=str(exc))
    except Exception as exc:
        return done("error", detail=f"{type(exc).__name__}: {exc}")

    terms = query_terms(query)
    on_topic = (
        sum(1 for hit in hits if alignment(terms, hit) >= ON_TOPIC_ALIGNMENT) / len(hits)
        if hits
        else 0.0
    )
    fields = {
        "hits": len(hits),
        "on_topic": round(on_topic, 2),
        "sample": [hit.url for hit in hits[:3]],
    }
    if not hits:
        return done("empty", detail=f"{instance.name} found nothing for {query!r}", **fields)
    if instance.serves_decoys and looks_like_decoy(query, hits):
        return done("decoy", detail=str(decoy_block(instance.name)), **fields)
    return done("ok", **fields)


def default_engines(searxng_url: str | None = None) -> tuple[type[Engine], ...]:
    """Every built-in engine, plus SearXNG when a server is named or configured."""
    server = searxng_url or os.environ.get(SEARXNG_ENV, "")
    return ALL_ENGINES + ((searxng(server),) if server else ())


async def doctor(
    query: str | None = None,
    *,
    engines: tuple[type[Engine], ...] | None = None,
    timeout: float = 12.0,
) -> list[EngineCheck]:
    """Check every engine concurrently, in the order given.

    Each engine is asked its own probe query unless `query` is given. The
    default set is every built-in engine, plus SearXNG if
    ``WEBLESS_SEARXNG_URL`` is set.
    """
    chosen = default_engines() if engines is None else engines
    return list(
        await asyncio.gather(*(check_engine(e, query, timeout=timeout) for e in chosen))
    )


__all__ = [
    "doctor",
    "check_engine",
    "default_engines",
    "EngineCheck",
    "PROBE_QUERY",
    "STATUSES",
]
