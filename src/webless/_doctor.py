"""A live check of every engine, for finding out which ones still work.

The test suite runs against saved result pages, so it cannot notice the two
ways engines actually break: they change their markup, or they start blocking
this client. Both only show up against the real endpoint. `doctor` queries
each engine directly -- bypassing the cooldown `search` applies, and leaving
its record untouched -- and reports what came back and whether it was about
the query.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ._engines import (
    ON_TOPIC_ALIGNMENT,
    WIDE_ENGINES,
    Engine,
    alignment,
    decoy_block,
    is_block,
    looks_like_decoy,
    query_terms,
)
from ._http import WebError

# Chosen so every engine has a real answer: a technical subject with pages on
# the major indexes, a Wikipedia article, small-web documentation, and Hacker
# News threads. It also reads as two subjects, so a decoy keyed on the first
# word ("sqlite") does not pass for an answer.
PROBE_QUERY = "sqlite write-ahead logging"

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
    hits: int = 0
    on_topic: float = 0.0
    detail: str = ""
    sample: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the engine answered the query."""
        return self.status == "ok"


async def check_engine(
    engine: type[Engine], query: str = PROBE_QUERY, *, timeout: float = 12.0
) -> EngineCheck:
    """Query one engine and classify what came back."""
    instance = engine()
    started = time.monotonic()

    def done(status: str, **fields) -> EngineCheck:
        return EngineCheck(
            engine=instance.name,
            status=status,
            seconds=round(time.monotonic() - started, 2),
            **fields,
        )

    try:
        hits = await asyncio.wait_for(
            instance.search(query, 10, timeout), timeout=timeout + 2
        )
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


async def doctor(
    query: str = PROBE_QUERY,
    *,
    engines: tuple[type[Engine], ...] = WIDE_ENGINES,
    timeout: float = 12.0,
) -> list[EngineCheck]:
    """Check every engine concurrently, in the order given."""
    return list(
        await asyncio.gather(*(check_engine(e, query, timeout=timeout) for e in engines))
    )


__all__ = ["doctor", "check_engine", "EngineCheck", "PROBE_QUERY", "STATUSES"]
