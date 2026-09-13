"""Tests for webless.

Nothing here touches the network. Engines are exercised against captured
result markup and `fetch_url` is replaced wherever a call would otherwise make
a request, so the suite stays offline and deterministic while still covering
the parts that break in practice: tracker-wrapped URLs, fusion across
disagreeing engines, and a failing engine that must not take the call down
with it.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from webless import (
    USER_AGENTS,
    BingEngine,
    DuckDuckGoEngine,
    MojeekEngine,
    Response,
    SearchHit,
    WebError,
    WikipediaEngine,
    alignment,
    fetch,
    fetch_sync,
    fuse,
    html_to_markdown,
    host_of,
    normalize_url,
    page_title,
    parse_html,
    query_terms,
    rank_by_relevance,
    render,
    search,
    search_sync,
)
from webless._engines import (
    apply_domain_filters,
    decode_bing_url,
    search_web,
    unwrap_redirect,
)
from webless._http import dedup_key, next_user_agent
from webless.cli import main

DDG_HTML = """
<html><body>
  <a class="result-link" href="https://docs.python.org/3/library/asyncio.html">asyncio docs</a>
  <td class="result-snippet">Asynchronous I/O for Python.</td>
  <a class="result-link" href="https://realpython.com/async-io-python/">Async IO in Python</a>
  <td class="result-snippet">A complete walkthrough.</td>
</body></html>
"""

MOJEEK_HTML = """
<html><body><ul class="results-standard">
  <li><h2><a class="title" href="https://realpython.com/async-io-python/">Async IO</a></h2>
      <p class="s">Guide to async.</p></li>
  <li><h2><a class="title" href="https://peps.python.org/pep-3156/">PEP 3156</a></h2>
      <p class="s">Asynchronous IO support.</p></li>
</ul></body></html>
"""

ARTICLE_HTML = """
<html><head><title>Sample Page</title>
<meta name="description" content="A page about widgets.">
<script>var tracking = 1;</script></head>
<body>
  <nav><a href="/home">Home</a><a href="/about">About</a></nav>
  <article>
    <h1>Widgets</h1>
    <p>Widgets are <strong>useful</strong> and come in <em>many</em> shapes.</p>
    <ul><li>First widget</li><li>Second widget</li></ul>
    <pre><code class="language-python">print("hi")</code></pre>
    <p>See the <a href="/specs/widget">spec</a> for details.</p>
    <table><tr><th>Name</th><th>Size</th></tr><tr><td>Bolt</td><td>M4</td></tr></table>
  </article>
  <footer>Copyright</footer>
</body></html>
"""


def html_response(url: str, body: str = ARTICLE_HTML) -> Response:
    """A stand-in for a completed fetch of an HTML page."""
    return Response(
        url=url, final_url=url, status=200, text=body, content_type="text/html"
    )


def test_normalize_and_dedup_keys() -> None:
    assert normalize_url("example.com/a") == "https://example.com/a"
    assert normalize_url("HTTPS://Example.COM/a#frag") == "https://example.com/a"
    assert dedup_key("https://www.example.com/a/") == dedup_key("http://example.com/a")
    assert host_of("https://docs.python.org/3/") == "docs.python.org"


def test_user_agent_rotation_always_changes() -> None:
    for agent in USER_AGENTS:
        assert next_user_agent(agent) != agent
        assert next_user_agent(agent) in USER_AGENTS
    assert next_user_agent("not-in-the-pool") in USER_AGENTS
    assert next_user_agent() in USER_AGENTS


def test_unwrap_and_decode_tracker_urls() -> None:
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage"
    assert unwrap_redirect(wrapped) == "https://example.com/page"
    assert unwrap_redirect("https://example.com/plain") == "https://example.com/plain"

    encoded = base64.b64encode(b"https://example.com/target").decode().rstrip("=")
    assert decode_bing_url(f"https://www.bing.com/ck/a?u=a1{encoded}") == (
        "https://example.com/target"
    )


def test_duckduckgo_parses_results() -> None:
    hits = DuckDuckGoEngine()._parse(DDG_HTML, "a.result-link", ".result-snippet", 10)
    assert [h.url for h in hits] == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://realpython.com/async-io-python/",
    ]
    assert hits[0].snippet == "Asynchronous I/O for Python."
    assert hits[0].engines == ["duckduckgo"]


def test_mojeek_parses_results() -> None:
    async def run() -> list[SearchHit]:
        engine = MojeekEngine()

        async def fake_get(url, timeout, headers=None):
            return html_response(url, MOJEEK_HTML)

        engine._get = fake_get
        return await engine.search("async io", 10, 5.0)

    assert [h.title for h in asyncio.run(run())] == ["Async IO", "PEP 3156"]


async def test_engine_refuses_a_challenge_status(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        return Response(
            url=url, final_url=url, status=202, text="<html>robot?</html>",
            content_type="text/html",
        )

    monkeypatch.setattr("webless._engines.fetch_url", fake_fetch)

    with pytest.raises(WebError) as excinfo:
        await MojeekEngine().search("anything", 5, 5.0)
    assert "202" in str(excinfo.value)


async def test_soft_block_is_reported_as_a_failure(monkeypatch) -> None:
    """A results page that parses to nothing is a block, not zero results."""

    async def fake_fetch(url, **kwargs):
        return html_response(url, "<html><body><h1>Mojeek</h1></body></html>")

    monkeypatch.setattr("webless._engines.fetch_url", fake_fetch)

    result = await search(" anything ", engines=(MojeekEngine,))
    assert not result.ok
    assert "soft block" in result.failed["mojeek"]


def test_bing_reads_the_rss_view() -> None:
    xml = """<rss><channel>
      <item><title>Async IO</title><link>https://example.com/a</link>
        <description>About async.</description></item>
    </channel></rss>"""
    hits = BingEngine()._parse_rss(xml, 10)
    assert hits[0].url == "https://example.com/a"
    assert hits[0].engines == ["bing"]


def test_wikipedia_parses_opensearch() -> None:
    async def run() -> list[SearchHit]:
        engine = WikipediaEngine()

        async def fake_get(url, timeout, headers=None):
            body = (
                '["asyncio",["Asyncio"],["Python library"],'
                '["https://en.wikipedia.org/wiki/Asyncio"]]'
            )
            return Response(
                url=url, final_url=url, status=200, text=body,
                content_type="application/json",
            )

        engine._get = fake_get
        return await engine.search("asyncio", 5, 5.0)

    assert asyncio.run(run())[0].url == "https://en.wikipedia.org/wiki/Asyncio"


def test_fusion_rewards_agreement_across_engines() -> None:
    shared = "https://example.com/shared"
    a = [
        SearchHit(title="Only A", url="https://example.com/a", engines=["alpha"]),
        SearchHit(title="Shared", url=shared, engines=["alpha"]),
    ]
    b = [
        SearchHit(title="Shared", url=shared + "/", engines=["beta"]),
        SearchHit(title="Only B", url="https://example.com/b", engines=["beta"]),
    ]

    fused = fuse([a, b])
    assert fused[0].url.startswith(shared)
    assert sorted(fused[0].engines) == ["alpha", "beta"]
    assert len(fused) == 3


def test_relevance_reweighting_demotes_off_topic_agreement() -> None:
    """An engine answering loosely must not outrank one answering the query."""
    on_topic = SearchHit(
        title="Reciprocal rank fusion explained",
        url="https://example.com/rrf",
        snippet="How RRF merges ranked lists.",
        score=0.016,
    )
    off_topic = SearchHit(
        title="Launch HN: a company brain",
        url="https://news.ycombinator.com/item?id=1",
        snippet="Discussion: 79 points.",
        score=0.017,
    )

    ranked = rank_by_relevance("reciprocal rank fusion explained", [off_topic, on_topic])
    assert ranked[0] is on_topic


def test_query_terms_drops_stopwords_and_noise() -> None:
    assert query_terms("What is the Reciprocal Rank Fusion?") == [
        "reciprocal",
        "rank",
        "fusion",
    ]


def test_alignment_is_the_share_of_query_terms_present() -> None:
    hit = SearchHit(title="Rank fusion", url="https://example.com/x", snippet="")
    assert alignment(["rank", "fusion"], hit) == 1.0
    assert alignment(["rank", "fusion", "elephant"], hit) == pytest.approx(2 / 3)
    assert alignment([], hit) == 1.0


def test_domain_filters_cover_subdomains() -> None:
    hits = [
        SearchHit(title="docs", url="https://docs.python.org/3/"),
        SearchHit(title="blog", url="https://medium.com/post"),
    ]
    assert len(apply_domain_filters(hits, ["python.org"], None)) == 1
    assert len(apply_domain_filters(hits, None, ["medium.com"])) == 1


async def test_search_survives_a_failing_engine() -> None:
    class Good(DuckDuckGoEngine):
        name = "good"

        async def search(self, query, limit, timeout):
            return [SearchHit(title="Result", url="https://example.com/x", engines=["good"])]

    class Bad(DuckDuckGoEngine):
        name = "bad"

        async def search(self, query, limit, timeout):
            raise WebError("https://bad.example returned HTTP 403.", status=403)

    report = await search_web("anything", engines=(Good, Bad))
    assert report.succeeded == ["good"]
    assert "bad" in report.failed
    assert report.hits[0].url == "https://example.com/x"


async def test_search_result_behaves_like_the_ranking_it_carries() -> None:
    class Good(DuckDuckGoEngine):
        name = "good"

        async def search(self, query, limit, timeout):
            return [
                SearchHit(title="One", url="https://example.com/1", engines=["good"]),
                SearchHit(title="Two", url="https://example.com/2", engines=["good"]),
            ]

    result = await search("anything", engines=(Good,))
    assert len(result) == 2
    assert result[0].title == "One"
    assert [hit.title for hit in result] == ["One", "Two"]
    assert result.ok


async def test_search_rejects_an_empty_query() -> None:
    with pytest.raises(ValueError):
        await search("   ")


async def test_wide_adds_the_long_tail_engines(monkeypatch) -> None:
    asked: list[str] = []

    async def spy(query, **kwargs):
        asked.extend(cls().name for cls in kwargs["engines"])
        return await search_web(query, engines=())

    monkeypatch.setattr("webless._search.search_web", spy)
    await search("anything", wide=True)
    assert "marginalia" in asked and "hackernews" in asked


async def test_fetch_returns_markdown_without_the_chrome(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        return html_response("https://site.test/docs/page")

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)

    page = await fetch("site.test/docs/page")
    assert page.title == "Sample Page"
    assert page.description == "A page about widgets."
    assert page.host == "site.test"
    assert not page.redirected
    assert "# Widgets" in page.content
    assert "Copyright" not in page.content
    assert not page.truncated


async def test_fetch_pages_a_long_body(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        return Response(
            url=url, final_url=url, status=200, text="x" * 100,
            content_type="text/plain",
        )

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)

    first = await fetch("https://site.test/long", max_chars=40)
    assert len(first) == 40
    assert first.truncated

    last = await fetch("https://site.test/long", offset=40, max_chars=60)
    assert len(last) == 60
    assert not last.truncated


async def test_fetch_reports_a_redirect(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        return Response(
            url=url, final_url="https://site.test/moved", status=200,
            text=ARTICLE_HTML, content_type="text/html",
        )

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)

    page = await fetch("https://site.test/old")
    assert page.redirected
    assert page.final_url == "https://site.test/moved"


async def test_fetch_rejects_a_bad_format_and_a_bad_url() -> None:
    with pytest.raises(ValueError):
        await fetch("https://site.test/", format="pdf")
    with pytest.raises(ValueError):
        await fetch("not a url at all", format="raw")


def test_render_formats_json_without_a_round_trip() -> None:
    response = Response(
        url="https://api.test/x", final_url="https://api.test/x", status=200,
        text='{"b":2,"a":1}', content_type="application/json",
    )
    content, title, description = render(response)
    assert content == '{\n  "b": 2,\n  "a": 1\n}'
    assert title == "" and description == ""


def test_render_raw_leaves_the_body_untouched() -> None:
    response = html_response("https://site.test/x")
    assert render(response, format="raw")[0] == ARTICLE_HTML


def test_render_text_drops_markup_but_keeps_the_title() -> None:
    content, title, _ = render(html_response("https://site.test/x"), format="text")
    assert title == "Sample Page"
    assert "Widgets are useful" in content
    assert "**" not in content


def test_render_full_page_keeps_the_navigation() -> None:
    content, _, _ = render(html_response("https://site.test/x"), full_page=True)
    assert "About" in content


def test_html_to_markdown_keeps_structure_and_drops_chrome() -> None:
    markdown = html_to_markdown(ARTICLE_HTML, base_url="https://site.test/docs/page")

    assert "# Widgets" in markdown
    assert "**useful**" in markdown
    assert "- First widget" in markdown
    assert "```" in markdown and 'print("hi")' in markdown
    assert "[spec](https://site.test/specs/widget)" in markdown
    assert "| Name | Size |" in markdown
    assert "tracking" not in markdown
    assert "Copyright" not in markdown
    assert "About" not in markdown


def test_code_blocks_keep_their_indentation() -> None:
    """Collapsing whitespace inside a code block would destroy the code."""
    html = (
        "<html><body><article><pre><code>def f():\n"
        "    if x:\n        return 1\n</code></pre></article></body></html>"
    )
    markdown = html_to_markdown(html)
    assert "    if x:" in markdown
    assert "        return 1" in markdown


def test_code_fences_survive_backticks_in_the_code() -> None:
    html = "<html><body><article><pre><code>a = ```b```</code></pre></article></body></html>"
    assert "````" in html_to_markdown(html)


def test_page_title_prefers_open_graph() -> None:
    root = parse_html(
        '<html><head><meta property="og:title" content="OG Title">'
        "<title>Fallback</title></head><body></body></html>"
    )
    assert page_title(root) == "OG Title"
    assert page_title(parse_html("<html><head><title>Only</title></head></html>")) == "Only"


def test_parser_recovers_from_unclosed_tags() -> None:
    root = parse_html("<ul><li>one<li>two<li>three</ul>")
    assert [li.text() for li in root.select("li")] == ["one", "two", "three"]


def test_sync_wrappers_run_their_own_loop(monkeypatch) -> None:
    async def fake_fetch(url, **kwargs):
        return html_response("https://site.test/x")

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    assert "# Widgets" in fetch_sync("https://site.test/x").content


async def test_sync_wrappers_refuse_to_run_inside_a_loop() -> None:
    """`asyncio.run` would report the loop rather than the mistake made."""
    with pytest.raises(RuntimeError, match="await the async version"):
        search_sync("anything")


def test_cli_prints_the_ranking(monkeypatch, capsys) -> None:
    async def fake_search(query, **kwargs):
        from webless._search import SearchResult

        return SearchResult(
            query=query,
            hits=[
                SearchHit(
                    title="Async IO",
                    url="https://example.com/a",
                    snippet="About async.",
                    engines=["duckduckgo", "bing"],
                )
            ],
            succeeded=["duckduckgo", "bing"],
            failed={"mojeek": "blocked"},
        )

    monkeypatch.setattr("webless.cli.search", fake_search)

    assert main(["search", "async", "io"]) == 0
    captured = capsys.readouterr()
    assert "1. Async IO" in captured.out
    assert "https://example.com/a" in captured.out
    assert "duckduckgo, bing" in captured.out
    assert "mojeek: blocked" in captured.err


def test_cli_exits_nonzero_when_every_engine_failed(monkeypatch, capsys) -> None:
    async def fake_search(query, **kwargs):
        from webless._search import SearchResult

        return SearchResult(query=query, hits=[], failed={"bing": "blocked"})

    monkeypatch.setattr("webless.cli.search", fake_search)
    assert main(["search", "anything"]) == 1


def test_cli_fetch_prints_the_page(monkeypatch, capsys) -> None:
    async def fake_fetch(url, **kwargs):
        return html_response("https://site.test/x")

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    assert main(["fetch", "https://site.test/x"]) == 0
    assert "# Widgets" in capsys.readouterr().out


def test_cli_turns_a_web_error_into_a_message(monkeypatch, capsys) -> None:
    async def fake_fetch(url, **kwargs):
        raise WebError("https://site.test/x returned HTTP 404.", status=404)

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    assert main(["fetch", "https://site.test/x"]) == 1
    assert "404" in capsys.readouterr().err
