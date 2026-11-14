"""The public search API.

One call, `search`, queries several keyless engines at once and returns a
single fused ranking. The engines are public endpoints, so nothing has to be
configured before the first call works, and none of them is treated as
required: an engine that is blocked, rate limited, or slow costs recall and is
named in `SearchResult.failed`, while the rest of the ranking stands.

A `topic` adds specialist engines to the general ones: Stack Overflow for
code, OpenAlex, arXiv, and PubMed for science, Bing News for news.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._engines import TOPICS, Engine, SearchHit, engines_for, search_web

DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_S = 12.0


@dataclass(slots=True)
class SearchResult:
    """A fused ranking, plus what each engine did to produce it.

    `failed` is part of the result rather than an exception because a partial
    answer is still an answer: knowing that Mojeek was blocked while
    DuckDuckGo answered lets a caller decide whether to trust the ranking,
    without having to lose it.
    """

    query: str
    hits: list[SearchHit]
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self):
        return iter(self.hits)

    def __getitem__(self, index):
        return self.hits[index]

    @property
    def ok(self) -> bool:
        """Whether at least one engine answered."""
        return bool(self.succeeded)


async def search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    wide: bool = False,
    topic: str | None = None,
    engines: tuple[type[Engine], ...] | None = None,
    searxng_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> SearchResult:
    """Search the web without an API key.

    `wide` adds the long-tail engines -- Marginalia's small-web index and
    Hacker News -- to the general set, which helps on niche or discussion
    oriented queries and adds latency on ordinary ones. Pass `engines`
    directly to choose the set yourself.

    `topic` adds the engines for one subject on top of that set: ``code``
    (Stack Overflow), ``science`` (OpenAlex, arXiv, PubMed), or ``news``
    (Bing News). Their results are fused with the general ones, so a topic
    reorders the answer toward specialist sources without losing the web.

    `searxng_url` adds a SearXNG server you run. Without it, the
    ``WEBLESS_SEARXNG_URL`` environment variable is used, unless `engines`
    was passed; ``searxng_url=""`` leaves SearXNG out.

    `allowed_domains` and `blocked_domains` filter on host, and an entry
    covers its subdomains, so ``python.org`` keeps ``docs.python.org``.
    """
    if not query.strip():
        raise ValueError("query is empty")

    chosen = engines_for(topic, wide=wide, engines=engines, searxng_url=searxng_url)
    report = await search_web(
        query,
        limit=limit,
        engines=chosen,
        timeout=timeout,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    return SearchResult(
        query=query,
        hits=report.hits,
        succeeded=report.succeeded,
        failed=report.failed,
    )


__all__ = ["search", "SearchResult", "SearchHit", "TOPICS"]
