"""Tests for the topic engines, SearXNG, and the Stack Exchange fetch path.

Offline like the rest of the suite: each engine's `_get`, or the module's
`fetch_url`, is replaced with captured API output.
"""

from __future__ import annotations

import json
import time

import pytest

from webless import (
    ALL_ENGINES,
    GENERAL_ENGINES,
    WIDE_ENGINES,
    ArxivEngine,
    BingNewsEngine,
    Engine,
    EngineBlocked,
    EngineCheck,
    OpenAlexEngine,
    PubMedEngine,
    Response,
    SearchHit,
    SearXNGEngine,
    StackExchangeEngine,
    WebError,
    doctor,
    engines_for,
    fetch,
    search,
    searxng,
)
from webless._doctor import PROBE_QUERY, default_engines
from webless._engines import SEARXNG_ENV, abstract_text, decode_bing_news_url
from webless._fetch import stack_exchange_question
from webless.cli import main


def json_response(url: str, payload, status: int = 200) -> Response:
    return Response(
        url=url, final_url=url, status=status, text=json.dumps(payload),
        content_type="application/json",
    )


def xml_response(url: str, body: str) -> Response:
    return Response(url=url, final_url=url, status=200, text=body, content_type="text/xml")


def with_get(engine: Engine, answer, seen: list | None = None) -> Engine:
    """Replace `engine._get` with one that answers from `answer(url)`."""

    async def fake_get(url, timeout, headers=None, **kwargs):
        if seen is not None:
            seen.append((url, kwargs))
        return answer(url)

    engine._get = fake_get
    return engine


# -- engines -----------------------------------------------------------------


STACK_EXCHANGE = {
    "items": [
        {
            "title": "How to cancel a TaskGroup &amp; its tasks?",
            "link": "https://stackoverflow.com/questions/77001/how-to-cancel",
            "score": 12, "answer_count": 3, "is_answered": True,
            "tags": ["python", "python-asyncio"],
        },
        {
            "title": "TaskGroup swallowing exceptions",
            "link": "https://stackoverflow.com/questions/77002/taskgroup",
            "score": 0, "answer_count": 0, "is_answered": False, "tags": [],
        },
    ],
    "quota_remaining": 280,
}


async def test_stack_exchange_reads_questions() -> None:
    engine = with_get(StackExchangeEngine(), lambda url: json_response(url, STACK_EXCHANGE))
    hits = await engine.search("asyncio taskgroup cancel", 10, 5.0)
    assert [h.title for h in hits] == [
        "How to cancel a TaskGroup & its tasks?",
        "TaskGroup swallowing exceptions",
    ]
    assert "12 votes, 3 answers (answered)" in hits[0].snippet
    assert "python-asyncio" in hits[0].snippet
    assert "(unanswered)" in hits[1].snippet


async def test_stack_exchange_quota_exhaustion_is_a_block() -> None:
    def refuse(url):
        raise WebError("returned HTTP 400.", status=400)

    engine = with_get(StackExchangeEngine(), refuse)
    with pytest.raises(EngineBlocked) as excinfo:
        await engine.search("anything", 5, 5.0)
    assert excinfo.value.status == 400 and "quota" in str(excinfo.value)


OPENALEX = {
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1/abc",
            "display_name": "Off-target effects of CRISPR",
            "publication_year": 2019,
            "cited_by_count": 450,
            "primary_location": {
                "landing_page_url": "https://publisher.test/abc",
                "source": {"display_name": "Nature Methods"},
            },
            "best_oa_location": {"landing_page_url": "https://europepmc.test/abc"},
            "abstract_inverted_index": {"CRISPR": [0], "cuts": [1], "DNA": [2]},
        },
        {
            "id": "https://openalex.org/W2",
            "doi": "https://doi.org/10.1/def",
            "display_name": "Closed paper",
            "publication_year": 2021,
            "cited_by_count": 3,
            "primary_location": None,
            "best_oa_location": None,
            "abstract_inverted_index": None,
        },
    ]
}


async def test_openalex_prefers_the_open_access_copy() -> None:
    engine = with_get(OpenAlexEngine(), lambda url: json_response(url, OPENALEX))
    hits = await engine.search("crispr off-target", 10, 5.0)
    assert [h.url for h in hits] == ["https://europepmc.test/abc", "https://doi.org/10.1/def"]
    assert hits[0].snippet == "2019, Nature Methods, cited by 450. CRISPR cuts DNA"
    assert hits[1].snippet == "2021, cited by 3"


def test_abstract_is_rebuilt_in_word_order() -> None:
    assert abstract_text({"world": [1], "hello": [0], "again": [3], "and": [2]}) == (
        "hello world and again"
    )
    assert abstract_text({"a": [0], "b": [1], "c": [2]}, max_words=2) == "a b ..."
    assert abstract_text(None) == ""


ARXIV = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v2</id>
    <published>2021-01-01T00:00:00Z</published>
    <title>Predicting CRISPR off-target
      effects</title>
    <summary>  We predict off-target cleavage.  </summary>
  </entry>
</feed>"""


async def test_arxiv_ands_the_terms_and_links_over_https() -> None:
    seen: list = []
    engine = with_get(ArxivEngine(), lambda url: xml_response(url, ARXIV), seen)
    hits = await engine.search("crispr off-target effects", 10, 5.0)
    assert "search_query=all:crispr+AND+all:" in seen[0][0]
    assert hits[0].url == "https://arxiv.org/abs/2101.00001v2"
    assert hits[0].title == "Predicting CRISPR off-target effects"
    assert hits[0].snippet == "arXiv preprint, 2021. We predict off-target cleavage."


async def test_pubmed_looks_up_ids_then_summaries() -> None:
    def answer(url):
        if "esearch" in url:
            return json_response(url, {"esearchresult": {"idlist": ["111", "222"]}})
        return json_response(url, {"result": {
            "uids": ["111", "222"],
            "111": {"title": "CRISPR safety.", "source": "Nat Biotechnol",
                    "pubdate": "2020 Mar", "authors": [{"name": "Kim J"}, {"name": "Lee S"}]},
            "222": {"title": "Guide design.", "source": "Cell", "pubdate": "2018",
                    "authors": [{"name": "Wu X"}]},
        }})

    seen: list = []
    engine = with_get(PubMedEngine(), answer, seen)
    hits = await engine.search("crispr off-target", 10, 5.0)
    assert [h.url for h in hits] == [
        "https://pubmed.ncbi.nlm.nih.gov/111/",
        "https://pubmed.ncbi.nlm.nih.gov/222/",
    ]
    assert hits[0].snippet == "PubMed: Kim J et al., Nat Biotechnol, 2020 Mar."
    assert hits[1].snippet == "PubMed: Wu X, Cell, 2018."
    assert "id=111,222" in seen[1][0]
    # Identifies the tool, and nothing about the person running it.
    assert all("tool=webless" in url and "email" not in url for url, _ in seen)


async def test_pubmed_with_no_matches_makes_one_request() -> None:
    seen: list = []
    engine = with_get(
        PubMedEngine(),
        lambda url: json_response(url, {"esearchresult": {"idlist": []}}),
        seen,
    )
    assert await engine.search("zzzz", 10, 5.0) == []
    assert len(seen) == 1


BING_NEWS = """<rss><channel><title>Bing News</title>
<item><title>Fed holds rates</title>
  <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=x&amp;url=https%3a%2f%2fnews.test%2ffed-holds&amp;c=1</link>
  <description>The Federal Reserve kept rates unchanged.</description>
  <pubDate>Wed, 12 Nov 2026 18:00:00 GMT</pubDate></item>
<item><title>No target</title>
  <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss</link></item>
</channel></rss>"""


async def test_bing_news_decodes_article_links_and_drops_bare_trackers() -> None:
    engine = with_get(BingNewsEngine(), lambda url: xml_response(url, BING_NEWS))
    hits = await engine.search("federal reserve rates", 10, 5.0)
    assert [h.url for h in hits] == ["https://news.test/fed-holds"]
    assert hits[0].snippet.endswith("(Wed, 12 Nov 2026 18:00:00 GMT)")


def test_bing_news_url_decoding() -> None:
    assert decode_bing_news_url(
        "http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fa.test%2fx"
    ) == "https://a.test/x"
    assert decode_bing_news_url("http://www.bing.com/news/apiclick.aspx?url=javascript:x") == ""
    assert decode_bing_news_url("https://a.test/direct") == "https://a.test/direct"


async def test_bing_news_page_without_a_feed_is_a_soft_block() -> None:
    engine = with_get(
        BingNewsEngine(), lambda url: xml_response(url, "<html><body>verify</body></html>")
    )
    with pytest.raises(EngineBlocked):
        await engine.search("anything", 5, 5.0)


# -- SearXNG -----------------------------------------------------------------


async def test_searxng_queries_the_configured_server_past_the_private_guard() -> None:
    seen: list = []
    engine = with_get(
        searxng("http://localhost:8888/")(),
        lambda url: json_response(url, {"results": [
            {"url": "https://a.test/1", "title": "One", "content": "first"},
            {"url": "https://a.test/2", "title": "Two", "content": "second"},
        ]}),
        seen,
    )
    hits = await engine.search("query", 1, 5.0)
    assert [h.title for h in hits] == ["One"]
    url, kwargs = seen[0]
    assert url == "http://localhost:8888/search?q=query&format=json"
    assert kwargs == {"allow_private": True}


async def test_searxng_reads_the_server_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(SEARXNG_ENV, "http://127.0.0.1:9000")
    seen: list = []
    engine = with_get(SearXNGEngine(), lambda url: json_response(url, {"results": []}), seen)
    assert await engine.search("q", 5, 5.0) == []
    assert seen[0][0].startswith("http://127.0.0.1:9000/search?")


async def test_searxng_without_a_server_says_how_to_configure_one() -> None:
    with pytest.raises(WebError, match=SEARXNG_ENV):
        await SearXNGEngine().search("q", 5, 5.0)


async def test_searxng_refusing_json_explains_the_setting() -> None:
    def refuse(url):
        raise WebError("returned HTTP 403.", status=403)

    engine = with_get(searxng("http://localhost:8888")(), refuse)
    with pytest.raises(WebError, match="formats: \\[html, json\\]"):
        await engine.search("q", 5, 5.0)


async def test_the_private_guard_is_lifted_only_on_request(monkeypatch) -> None:
    from webless._http import fetch_url

    with pytest.raises(WebError, match="private"):
        await fetch_url("http://127.0.0.1:1/", attempts=1, timeout=1)
    # With the guard lifted the request is attempted, and fails to connect.
    with pytest.raises(WebError) as excinfo:
        await fetch_url("http://127.0.0.1:1/", attempts=1, timeout=1, allow_private=True)
    assert "private" not in str(excinfo.value)


# -- choosing engines --------------------------------------------------------


def test_topics_add_to_the_general_set() -> None:
    assert engines_for() == GENERAL_ENGINES
    assert engines_for(wide=True) == WIDE_ENGINES
    assert engines_for("code") == GENERAL_ENGINES + (StackExchangeEngine,)
    assert engines_for("science")[-3:] == (OpenAlexEngine, ArxivEngine, PubMedEngine)
    assert engines_for("news", engines=(BingNewsEngine,)) == (BingNewsEngine,)
    with pytest.raises(ValueError, match="code, science, news"):
        engines_for("sports")


def test_searxng_joins_from_the_argument_or_the_environment(monkeypatch) -> None:
    [server] = engines_for(searxng_url="http://localhost:8888")[-1:]
    assert issubclass(server, SearXNGEngine) and server.base_url == "http://localhost:8888"

    monkeypatch.setenv(SEARXNG_ENV, "http://localhost:9999")
    assert engines_for()[-1].base_url == "http://localhost:9999"
    assert engines_for(searxng_url="") == GENERAL_ENGINES
    # An explicit engine list is taken as given.
    assert engines_for(engines=(StackExchangeEngine,)) == (StackExchangeEngine,)


async def test_search_with_a_topic_fuses_the_specialists_in(monkeypatch) -> None:
    def fake(name):
        async def run(self, query, limit, timeout):
            return [SearchHit(title=f"{name} asyncio", url=f"https://{name}.test/")]

        return run

    for cls in (*GENERAL_ENGINES, StackExchangeEngine):
        monkeypatch.setattr(cls, "search", fake(cls.name))
    result = await search("asyncio", topic="code")
    assert "stackexchange" in result.succeeded
    assert set(result.succeeded) == {cls.name for cls in GENERAL_ENGINES} | {"stackexchange"}


async def test_spaced_engines_queue_their_requests(monkeypatch) -> None:
    calls: list[float] = []

    async def fake_fetch(url, **kwargs):
        calls.append(time.monotonic())
        return Response(url=url, final_url=url, status=200, text="", content_type="text/plain")

    monkeypatch.setattr("webless._engines.fetch_url", fake_fetch)

    class Spaced(Engine):
        name = "spaced"
        min_interval = 0.2

    import asyncio

    await asyncio.gather(*(Spaced()._get(f"https://s.test/{i}", 5.0) for i in range(3)))
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(gap >= 0.18 for gap in gaps), gaps


# -- Stack Exchange fetch ----------------------------------------------------


def test_stack_exchange_question_urls() -> None:
    assert stack_exchange_question("https://stackoverflow.com/questions/123/title") == (
        "stackoverflow", "123",
    )
    assert stack_exchange_question("https://stackoverflow.com/q/123") == ("stackoverflow", "123")
    assert stack_exchange_question("https://unix.stackexchange.com/questions/9/x") == (
        "unix", "9",
    )
    assert stack_exchange_question("https://superuser.com/questions/5") == ("superuser", "5")
    assert stack_exchange_question("https://stackoverflow.com/tags/python") is None
    assert stack_exchange_question("https://example.com/questions/1") is None


QUESTION = {"items": [{
    "title": "Why is &lt;b&gt; slow?",
    "link": "https://stackoverflow.com/questions/123/why",
    "score": 40,
    "body": "<p>The question body.</p>",
    "answers": [
        {"score": 90, "is_accepted": False, "body": "<p>Popular answer.</p>"},
        {"score": 10, "is_accepted": True, "body": "<p>Accepted answer.</p>"},
        {"score": 1, "is_accepted": False, "body": "<p>Minor answer.</p>"},
    ],
}]}


async def test_stack_overflow_questions_are_read_through_the_api(monkeypatch) -> None:
    requested: list[str] = []

    async def fake_fetch(url, **kwargs):
        requested.append(url)
        return json_response(url, QUESTION)

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    page = await fetch("https://stackoverflow.com/questions/123/why")

    assert requested[0].startswith("https://api.stackexchange.com/2.3/questions/123?")
    assert page.title == "Why is <b> slow?"
    content = page.content
    assert content.index("The question body") < content.index("Accepted answer.")
    assert content.index("Accepted answer.") < content.index("Popular answer.")
    assert content.index("Popular answer.") < content.index("Minor answer.")
    assert "Accepted answer (score 10)" in content


async def test_stack_overflow_falls_back_to_the_page(monkeypatch) -> None:
    requested: list[str] = []

    async def fake_fetch(url, **kwargs):
        requested.append(url)
        if "api.stackexchange.com" in url:
            raise WebError("returned HTTP 400.", status=400)
        return Response(
            url=url, final_url=url, status=200, content_type="text/html",
            text="<html><head><title>Page</title></head><body><p>From the page.</p></body></html>",
        )

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    page = await fetch("https://stackoverflow.com/questions/123/why")
    assert len(requested) == 2 and "From the page." in page.content


async def test_raw_fetch_skips_the_api(monkeypatch) -> None:
    requested: list[str] = []

    async def fake_fetch(url, **kwargs):
        requested.append(url)
        return Response(url=url, final_url=url, status=200, text="x", content_type="text/html")

    monkeypatch.setattr("webless._fetch.fetch_url", fake_fetch)
    await fetch("https://stackoverflow.com/questions/123/why", format="raw")
    assert requested == ["https://stackoverflow.com/questions/123/why"]


# -- doctor and CLI ----------------------------------------------------------


async def test_doctor_asks_each_engine_its_own_probe() -> None:
    asked: dict[str, str] = {}

    def engine(name, probe=None):
        async def run(self, query, limit, timeout):
            asked[self.name] = query
            return []

        attrs = {"name": name, "search": run}
        if probe:
            attrs["probe"] = probe
        return type(name, (Engine,), attrs)

    checks = await doctor(engines=(engine("web"), engine("papers", "crispr")))
    assert asked == {"web": PROBE_QUERY, "papers": "crispr"}
    assert [c.query for c in checks] == [PROBE_QUERY, "crispr"]

    await doctor("override", engines=(engine("web"), engine("papers", "crispr")))
    assert set(asked.values()) == {"override"}


def test_doctor_checks_every_engine_and_a_configured_server(monkeypatch) -> None:
    assert default_engines() == ALL_ENGINES
    monkeypatch.setenv(SEARXNG_ENV, "http://localhost:8888")
    assert default_engines()[-1].name == "searxng"


def test_cli_search_passes_the_topic(monkeypatch) -> None:
    seen = {}

    async def fake_search(query, **kwargs):
        seen.update(kwargs)
        from webless import SearchResult

        return SearchResult(query=query, hits=[], succeeded=["stackexchange"])

    monkeypatch.setattr("webless.cli.search", fake_search)
    assert main(["search", "-t", "code", "--searxng", "http://localhost:8888", "x"]) == 0
    assert seen["topic"] == "code" and seen["searxng_url"] == "http://localhost:8888"


def test_cli_doctor_shows_each_engines_query(monkeypatch, capsys) -> None:
    async def fake_doctor(query, **kwargs):
        return [EngineCheck(engine="pubmed", status="ok", seconds=1.0, query="crispr", hits=5)]

    monkeypatch.setattr("webless.cli.doctor", fake_doctor)
    assert main(["doctor", "-e", "pubmed"]) == 0
    assert "crispr" in capsys.readouterr().out
