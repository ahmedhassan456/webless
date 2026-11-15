"""webless as an MCP server, so any MCP client can search and fetch.

Three read-only tools over the library's own calls:

- ``web_search`` -- the fused multi-engine search, with topics and domain
  filters
- ``web_fetch`` -- a page as Markdown or text, read in sections
- ``search_engine_status`` -- `doctor`, for when searches come back thin

It speaks stdio by default, which is what desktop clients and editors launch,
and Streamable HTTP for clients that connect to a URL. Nothing is configured
up front; ``WEBLESS_SEARXNG_URL`` adds a SearXNG server, as it does for the
library.

The URLs `web_fetch` opens are chosen by a model, so the library's guard
against private, loopback, and link-local addresses matters here more than
anywhere: a page cannot talk the model into reading ``localhost`` or a cloud
metadata endpoint through this server.

Needs the optional dependency: ``pip install "webless[mcp]"``.
"""

from __future__ import annotations

import argparse
import logging
from typing import Annotated, Literal

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The webless MCP server needs the "mcp" extra: pip install "webless[mcp]"'
    ) from exc
from pydantic import Field

from . import __version__
from ._doctor import doctor
from .cli import add_mcp_arguments
from ._fetch import fetch
from ._http import WebError
from ._search import search

# httpx logs every request URL at INFO, which floods a client's server log
# with one line per engine per search.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_RESULTS = 25
DEFAULT_FETCH_CHARS = 20_000
MAX_FETCH_CHARS = 100_000

INSTRUCTIONS = """\
Web search and page fetching with no API keys.

Use web_search to find pages, then web_fetch to read the ones worth reading;
snippets are short and often stale. Set topic for specialist sources: "code"
adds Stack Overflow, "science" adds OpenAlex, arXiv, and PubMed, "news" adds
Bing News. Long pages come back in sections: when a fetch says it was
truncated, call web_fetch again with the offset it gives. If searches return
little, search_engine_status shows which engines are answering.
"""

# Every tool only reads the public web: nothing it does changes state, and the
# same call can be repeated safely.
READ_ONLY_WEB = dict(read_only_hint=True, destructive_hint=False, open_world_hint=True)

server = MCPServer(
    name="webless",
    title="webless",
    version=__version__,
    instructions=INSTRUCTIONS,
    website_url="https://github.com/ahmedhassan456/webless",
)


@server.tool(
    title="Search the web",
    annotations=ToolAnnotations(title="Search the web", idempotent_hint=True, **READ_ONLY_WEB),
    structured_output=False,
)
async def web_search(
    query: Annotated[str, Field(min_length=1, description="What to search for.")],
    topic: Annotated[
        Literal["code", "science", "news"] | None,
        Field(
            description='Add specialist engines: "code" (Stack Overflow), "science" '
            '(OpenAlex, arXiv, PubMed), or "news" (Bing News). Omit for general web search.'
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_RESULTS, description="How many results.")] = 10,
    allowed_domains: Annotated[
        list[str] | None,
        Field(description="Only return results from these domains (subdomains included)."),
    ] = None,
    blocked_domains: Annotated[
        list[str] | None, Field(description="Never return results from these domains.")
    ] = None,
    wide: Annotated[
        bool,
        Field(description="Also query Marginalia and Hacker News, for niche or discussion topics."),
    ] = False,
) -> str:
    """Search the web across several engines at once and return one fused ranking.

    Each result has a title, URL, snippet, and the engines that found it; a
    result several engines agree on is usually the better answer. Use
    web_fetch to read a result in full.
    """
    try:
        result = await search(
            query,
            limit=limit,
            topic=topic,
            wide=wide,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
    except (WebError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    return format_search(query, result)


@server.tool(
    title="Fetch a web page",
    annotations=ToolAnnotations(title="Fetch a web page", idempotent_hint=True, **READ_ONLY_WEB),
    structured_output=False,
)
async def web_fetch(
    url: Annotated[str, Field(min_length=1, description="The page to read (http or https).")],
    format: Annotated[
        Literal["markdown", "text"],
        Field(description="Markdown keeps headings, links, lists, and code; text is plain."),
    ] = "markdown",
    max_chars: Annotated[
        int,
        Field(ge=500, le=MAX_FETCH_CHARS, description="How much of the page to return at once."),
    ] = DEFAULT_FETCH_CHARS,
    offset: Annotated[
        int, Field(ge=0, description="Where to start, to read the next section of a long page.")
    ] = 0,
    full_page: Annotated[
        bool,
        Field(description="Keep navigation, sidebars, and footers instead of just the main content."),
    ] = False,
) -> str:
    """Fetch a URL and return its readable content, not its markup.

    The default trims to the article body and renders it as Markdown. Stack
    Overflow and other Stack Exchange questions come back as the question and
    its best answers. Private and local network addresses are refused.
    """
    try:
        page = await fetch(
            url, format=format, max_chars=max_chars, offset=offset, full_page=full_page
        )
    except (WebError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    return format_page(page, offset)


@server.tool(
    title="Check search engines",
    annotations=ToolAnnotations(title="Check search engines", **READ_ONLY_WEB),
    structured_output=False,
)
async def search_engine_status() -> str:
    """Check which search engines are answering from this network.

    Takes several seconds: each engine is sent a test query. Use it when
    web_search returns few or poor results, to see which engines are blocked.
    """
    checks = await doctor()
    lines = [
        f"{c.engine}: {c.status}"
        + (f" ({c.hits} results, {c.on_topic:.0%} on topic)" if c.hits else "")
        + (f" -- {c.detail}" if c.detail else "")
        for c in checks
    ]
    working = sum(c.ok for c in checks)
    lines.append(f"\n{working} of {len(checks)} engines working.")
    return "\n".join(lines)


def format_search(query: str, result) -> str:
    """A ranking as numbered plain text, with any engine that failed named."""
    if result.hits:
        blocks = []
        for index, hit in enumerate(result.hits, start=1):
            lines = [f"{index}. {hit.title}", f"   {hit.url}"]
            if hit.snippet:
                lines.append(f"   {hit.snippet}")
            lines.append(f"   found by: {', '.join(hit.engines)}")
            blocks.append("\n".join(lines))
        text = "\n\n".join(blocks)
    else:
        text = f"No results for {query!r}."
    if result.failed:
        reasons = "\n".join(f"- {name}: {reason}" for name, reason in result.failed.items())
        text += f"\n\nEngines that did not answer:\n{reasons}"
    return text


def format_page(page, offset: int) -> str:
    """A page with its title and final URL above the content."""
    header = [f"# {page.title}" if page.title else "", f"URL: {page.final_url}"]
    text = "\n".join(line for line in header if line) + "\n\n" + page.content
    if page.truncated:
        text += (
            f"\n\n[Truncated. Call web_fetch with offset={offset + len(page.content)} "
            "to read on.]"
        )
    elif offset and not page.content:
        text += "(Nothing past this offset; the page has been read to the end.)"
    return text


def build_parser() -> argparse.ArgumentParser:
    """Arguments for ``webless mcp`` and ``webless-mcp``."""
    parser = argparse.ArgumentParser(
        prog="webless-mcp", description="Run webless as an MCP server."
    )
    add_mcp_arguments(parser)
    return parser


def serve(args: argparse.Namespace) -> None:
    """Run the server until the client disconnects or it is interrupted."""
    if args.transport == "http":
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    else:
        server.run("stdio")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``webless-mcp`` script."""
    try:
        serve(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["server", "main", "web_search", "web_fetch", "search_engine_status"]
