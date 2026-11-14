"""webless: web search and page fetching with no API keys.

Two calls. `search` queries several public search engines at once and fuses
their rankings; `fetch` retrieves a URL and returns its readable content as
Markdown. Nothing has to be configured first -- there is no account, no key,
and no per-request quota to manage.

Example:
    import asyncio
    from webless import search, fetch

    async def main():
        result = await search("reciprocal rank fusion")
        for hit in result[:3]:
            print(hit.title, hit.url)

        page = await fetch(result[0].url)
        print(page.content[:500])

    asyncio.run(main())

The same two calls exist as `search_sync` and `fetch_sync` for code with no
event loop, and as a `webless` command on the terminal. `webless doctor` (or
`doctor()`) checks which engines are answering from where you are.

`search(query, topic="code" | "science" | "news")` adds specialist engines
-- Stack Overflow; OpenAlex, arXiv, and PubMed; Bing News -- to the general
ones, and `search(query, searxng_url=...)` adds a SearXNG server you run.
"""

from ._doctor import EngineCheck, check_engine, doctor
from ._engines import (
    ALL_ENGINES,
    ENGINE_HEALTH,
    ENGINES_BY_NAME,
    GENERAL_ENGINES,
    TOPIC_ENGINES,
    TOPICS,
    WIDE_ENGINES,
    ArxivEngine,
    BingEngine,
    BingNewsEngine,
    DuckDuckGoEngine,
    Engine,
    EngineBlocked,
    EngineHealth,
    HackerNewsEngine,
    MarginaliaEngine,
    MojeekEngine,
    OpenAlexEngine,
    PubMedEngine,
    SearXNGEngine,
    StackExchangeEngine,
    WikipediaEngine,
    alignment,
    engines_for,
    fuse,
    looks_like_decoy,
    query_terms,
    rank_by_relevance,
    searxng,
)
from ._html import html_to_markdown, main_content, page_description, page_title, parse_html
from ._http import USER_AGENTS, Response, WebError, aclose, fetch_url, host_of, normalize_url
from ._sync import fetch_sync, search_sync
from ._fetch import Page, fetch, render
from ._search import SearchHit, SearchResult, search

__version__ = "0.1.0"

__all__ = [
    "search",
    "fetch",
    "search_sync",
    "fetch_sync",
    "SearchResult",
    "SearchHit",
    "Page",
    "WebError",
    "EngineBlocked",
    "render",
    "aclose",
    "doctor",
    "check_engine",
    "EngineCheck",
    "EngineHealth",
    "ENGINE_HEALTH",
    "Engine",
    "DuckDuckGoEngine",
    "MojeekEngine",
    "BingEngine",
    "WikipediaEngine",
    "MarginaliaEngine",
    "HackerNewsEngine",
    "StackExchangeEngine",
    "OpenAlexEngine",
    "ArxivEngine",
    "PubMedEngine",
    "BingNewsEngine",
    "SearXNGEngine",
    "searxng",
    "GENERAL_ENGINES",
    "WIDE_ENGINES",
    "TOPIC_ENGINES",
    "TOPICS",
    "ALL_ENGINES",
    "ENGINES_BY_NAME",
    "engines_for",
    "fuse",
    "rank_by_relevance",
    "query_terms",
    "alignment",
    "looks_like_decoy",
    "fetch_url",
    "Response",
    "normalize_url",
    "host_of",
    "USER_AGENTS",
    "parse_html",
    "html_to_markdown",
    "main_content",
    "page_title",
    "page_description",
    "__version__",
]
