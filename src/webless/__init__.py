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
event loop, and as a `webless` command on the terminal.
"""

from ._engines import (
    ENGINES_BY_NAME,
    GENERAL_ENGINES,
    WIDE_ENGINES,
    BingEngine,
    DuckDuckGoEngine,
    Engine,
    HackerNewsEngine,
    MarginaliaEngine,
    MojeekEngine,
    WikipediaEngine,
    alignment,
    fuse,
    query_terms,
    rank_by_relevance,
)
from ._html import html_to_markdown, main_content, page_description, page_title, parse_html
from ._http import USER_AGENTS, Response, WebError, fetch_url, host_of, normalize_url
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
    "render",
    "Engine",
    "DuckDuckGoEngine",
    "MojeekEngine",
    "BingEngine",
    "WikipediaEngine",
    "MarginaliaEngine",
    "HackerNewsEngine",
    "GENERAL_ENGINES",
    "WIDE_ENGINES",
    "ENGINES_BY_NAME",
    "fuse",
    "rank_by_relevance",
    "query_terms",
    "alignment",
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
