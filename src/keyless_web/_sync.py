"""Blocking wrappers over the async API.

The core is async because a search is several concurrent requests and a slow
engine must not hold up the rest. Plenty of callers -- a script, a notebook
cell, a Django view -- have no loop to await in, so each entry point gets a
``*_sync`` twin that runs one for the duration of the call.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ._fetch import Page, fetch
from ._search import SearchResult, search


def _run(coro: Any) -> Any:
    """Run `coro` to completion, refusing to do it from inside a loop.

    `asyncio.run` inside a running loop raises a message about the loop rather
    than about the call the caller made, so the check is done here where the
    right advice -- await the async twin -- can be given.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "a sync call cannot run inside an event loop; await the async version instead"
    )


def search_sync(query: str, **kwargs: Any) -> SearchResult:
    """Blocking `search`. Takes the same keyword arguments."""
    return _run(search(query, **kwargs))


def fetch_sync(url: str, **kwargs: Any) -> Page:
    """Blocking `fetch`. Takes the same keyword arguments."""
    return _run(fetch(url, **kwargs))


__all__ = ["search_sync", "fetch_sync"]
