"""Tests for the MCP server, through a real MCP client connected in-process.

The library calls underneath are replaced, so this checks what an MCP client
sees -- tool listing, argument validation, result text, and errors -- without
touching the network.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from webless import EngineCheck, Page, SearchHit, SearchResult, WebError  # noqa: E402
from webless.cli import main  # noqa: E402
from webless.mcp_server import build_parser, server  # noqa: E402


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content)


def page(content: str, *, truncated: bool = False, title: str = "A page") -> Page:
    return Page(
        url="https://site.test/a", final_url="https://site.test/a", status=200,
        content_type="text/html", title=title, description="", content=content,
        truncated=truncated,
    )


async def test_lists_three_read_only_tools() -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"web_search", "web_fetch", "search_engine_status"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint and tool.annotations.open_world_hint
        assert not tool.annotations.destructive_hint
    search_args = tools["web_search"].input_schema["properties"]
    assert search_args["topic"]["anyOf"][0]["enum"] == ["code", "science", "news"]
    assert tools["web_search"].input_schema["required"] == ["query"]


async def test_web_search_returns_a_numbered_ranking(monkeypatch) -> None:
    seen = {}

    async def fake_search(query, **kwargs):
        seen.update(kwargs, query=query)
        return SearchResult(
            query=query,
            hits=[
                SearchHit(title="TaskGroup docs", url="https://docs.python.org/tg",
                          snippet="Structured concurrency.", engines=["duckduckgo", "bing"]),
                SearchHit(title="Cancel a TaskGroup", url="https://stackoverflow.com/q/1",
                          engines=["stackexchange"]),
            ],
            succeeded=["duckduckgo", "bing", "stackexchange"],
            failed={"mojeek": "returned a page with no results, likely a soft block."},
        )

    monkeypatch.setattr("webless.mcp_server.search", fake_search)
    async with Client(server) as client:
        result = await client.call_tool(
            "web_search",
            {"query": "asyncio taskgroup", "topic": "code", "limit": 5,
             "blocked_domains": ["pinterest.com"]},
        )
    text = text_of(result)
    assert not result.is_error
    assert text.startswith("1. TaskGroup docs\n   https://docs.python.org/tg")
    assert "found by: duckduckgo, bing" in text
    assert "2. Cancel a TaskGroup" in text
    assert "- mojeek: returned a page with no results" in text
    assert seen["topic"] == "code" and seen["limit"] == 5
    assert seen["blocked_domains"] == ["pinterest.com"]


async def test_web_search_rejects_arguments_outside_the_schema(monkeypatch) -> None:
    async def fake_search(query, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("called despite invalid arguments")

    monkeypatch.setattr("webless.mcp_server.search", fake_search)
    async with Client(server) as client:
        for arguments in (
            {"query": "x", "topic": "sports"},
            {"query": "x", "limit": 500},
            {"query": ""},
        ):
            result = await client.call_tool("web_search", arguments)
            assert result.is_error, arguments


async def test_web_search_with_nothing_found_says_so(monkeypatch) -> None:
    async def fake_search(query, **kwargs):
        return SearchResult(query=query, hits=[], failed={"bing": "returned HTTP 429."})

    monkeypatch.setattr("webless.mcp_server.search", fake_search)
    async with Client(server) as client:
        text = text_of(await client.call_tool("web_search", {"query": "zzz"}))
    assert text.startswith("No results for 'zzz'.") and "bing: returned HTTP 429." in text


async def test_web_fetch_returns_the_page_and_where_to_read_on(monkeypatch) -> None:
    seen = {}

    async def fake_fetch(url, **kwargs):
        seen.update(kwargs, url=url)
        return page("x" * 600, truncated=True)

    monkeypatch.setattr("webless.mcp_server.fetch", fake_fetch)
    async with Client(server) as client:
        result = await client.call_tool(
            "web_fetch", {"url": "https://site.test/a", "max_chars": 600, "offset": 1000}
        )
    text = text_of(result)
    assert text.startswith("# A page\nURL: https://site.test/a\n\n" + "x" * 600)
    assert "offset=1600" in text
    assert seen == {"url": "https://site.test/a", "format": "markdown", "max_chars": 600,
                    "offset": 1000, "full_page": False}


async def test_web_fetch_reports_a_refused_url_as_a_tool_error(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        raise WebError(f"{url} resolves to a private or loopback address; refusing to fetch it")

    monkeypatch.setattr("webless.mcp_server.fetch", fake_fetch)
    async with Client(server) as client:
        result = await client.call_tool("web_fetch", {"url": "http://169.254.169.254/"})
    assert result.is_error
    assert "private or loopback" in text_of(result)


async def test_web_fetch_really_refuses_private_addresses() -> None:
    """Not mocked: the guard in the library is what keeps a model out."""
    async with Client(server) as client:
        result = await client.call_tool("web_fetch", {"url": "http://127.0.0.1:9/"})
    assert result.is_error and "private" in text_of(result)


async def test_engine_status_summarises_doctor(monkeypatch) -> None:
    async def fake_doctor():
        return [
            EngineCheck(engine="duckduckgo", status="ok", seconds=0.8, hits=10, on_topic=1.0),
            EngineCheck(engine="bing", status="decoy", seconds=0.3, hits=10,
                        detail="bing returned off-topic results, likely a decoy block."),
        ]

    monkeypatch.setattr("webless.mcp_server.doctor", fake_doctor)
    async with Client(server) as client:
        text = text_of(await client.call_tool("search_engine_status", {}))
    assert "duckduckgo: ok (10 results, 100% on topic)" in text
    assert "bing: decoy" in text and "1 of 2 engines working." in text


def test_transport_options() -> None:
    args = build_parser().parse_args([])
    assert (args.transport, args.host, args.port, args.path) == ("stdio", "127.0.0.1", 8000, "/mcp")
    args = build_parser().parse_args(["--transport", "http", "--port", "9000"])
    assert args.transport == "http" and args.port == 9000


def test_cli_mcp_subcommand_runs_the_server(monkeypatch) -> None:
    served = []
    monkeypatch.setattr("webless.mcp_server.serve", served.append)
    assert main(["mcp", "--transport", "http", "--port", "8123"]) == 0
    assert served[0].transport == "http" and served[0].port == 8123
