"""The keyless search back ends and the fusion that merges them.

Each engine here is a public endpoint that answers without credentials. The
general set searches the whole web; the topic sets (`TOPIC_ENGINES`) add
specialist indexes -- Stack Overflow for code, OpenAlex, arXiv, and PubMed
for science, Bing News for news -- whose stable APIs answer the questions a
general engine buries. A SearXNG server the user runs can be added to any
set. None of them is trusted to be up. `search_web` queries them
concurrently, keeps whatever answers inside the deadline, and fuses the surviving rankings, so an engine
that is blocked, rate limited, or simply slow costs recall rather than the
whole call.

An engine is not trusted to say when it is blocked, either. A scraped engine
that has flagged a client may answer 200 with a page of plausible results
that have nothing to do with the query; those are caught by checking how many
of them mention the query at all (`looks_like_decoy`). And an engine that was
just blocked is likely to stay blocked for a while, so `ENGINE_HEALTH` pauses
it for a few minutes rather than paying its timeout on every call.

Fusion is Reciprocal Rank Fusion: a result's score is the sum of ``1 / (k +
rank)`` over the engines that returned it. Rank is all it needs, which is the
point -- the engines disagree about scoring and none of them exposes a
comparable number, but they all produce an ordered list, and a page several
independent indexes rank highly is a better answer than one page's favourite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import html
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

from ._html import parse_html
from ._http import (
    BLOCKED_STATUSES,
    Response,
    WebError,
    dedup_key,
    fetch_url,
    host_of,
    normalize_url,
)

RRF_K = 60

# The query `doctor` asks an engine that does not name its own. Chosen so the
# general engines all have a real answer, and so a decoy keyed on the first
# word ("sqlite") does not pass for one.
DEFAULT_PROBE = "sqlite write-ahead logging"

SEARXNG_ENV = "WEBLESS_SEARXNG_URL"

# A hit is on topic when it mentions at least half the query's content words,
# and a scraped engine is serving decoys when fewer than a fifth of its hits
# are on topic. Measured live, an unblocked engine sits at or near 100% and
# Bing's decoy pages at 0%, so the line has wide margins on both sides.
ON_TOPIC_ALIGNMENT = 0.5
DECOY_ON_TOPIC_SHARE = 0.2
DECOY_MIN_HITS = 3

# A block pauses an engine at once; other failures (timeouts, network errors)
# only after several in a row, since one slow answer says little.
BLOCK_COOLDOWN_S = 300.0
FAILURES_BEFORE_COOLDOWN = 3

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
    "for", "from", "how", "in", "is", "it", "of", "on", "or", "that", "the",
    "to", "use", "using", "vs", "was", "what", "when", "where", "which", "who",
    "why", "with", "you", "your",
})

REDIRECT_PARAMS = {
    "duckduckgo.com": "uddg",
    "lite.duckduckgo.com": "uddg",
    "html.duckduckgo.com": "uddg",
    "google.com": "q",
    "www.google.com": "q",
}


@dataclass(slots=True)
class SearchHit:
    """One result, before or after fusion."""

    title: str
    url: str
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def host(self) -> str:
        """The hostname the result lives on."""
        return host_of(self.url)


def unwrap_redirect(href: str) -> str:
    """Return the real target behind an engine's click-tracking redirect."""
    try:
        parts = urlsplit(href if "//" in href else f"https://{href}")
    except ValueError:
        return href
    param = REDIRECT_PARAMS.get((parts.hostname or "").lower())
    if not param:
        return href
    target = parse_qs(parts.query).get(param, [""])[0]
    return target if target.lower().startswith(("http://", "https://")) else href


class EngineBlocked(WebError):
    """An engine refused to answer: a challenge, a soft block, or decoys.

    Kept distinct from other failures because it predicts the next request:
    an engine that has flagged this client tends to keep refusing it for a
    while, whereas a timeout or a dropped connection usually does not repeat.
    """


def soft_block(engine: str) -> EngineBlocked:
    """The error raised when a results page parses to nothing.

    An engine that answers 200 with no result rows has almost always served a
    consent wall, a challenge, or its own home page. Reporting that as an
    engine failure is more honest than reporting zero results, and it keeps a
    silently blocked engine from looking like one that simply found nothing.
    """
    return EngineBlocked(f"{engine} returned a page with no results, likely a soft block.")


def decoy_block(engine: str) -> EngineBlocked:
    """The error raised when a results page answers some other question.

    The quieter form of a soft block: rather than an empty page, the engine
    serves a full page of real-looking results -- for "reciprocal rank
    fusion", Outlook login pages -- that ignore the query. Counting that as a
    success would credit the engine with an answer it never gave.
    """
    return EngineBlocked(f"{engine} returned off-topic results, likely a decoy block.")


def is_block(exc: BaseException) -> bool:
    """Whether a failure means the engine is refusing this client."""
    if isinstance(exc, EngineBlocked):
        return True
    return isinstance(exc, WebError) and exc.status in BLOCKED_STATUSES


def _clean(text: str) -> str:
    """Collapse whitespace in engine-supplied text."""
    return " ".join(text.split())


def _hit(title: str, url: str, snippet: str, engine: str) -> SearchHit | None:
    """Build a hit, dropping rows that lack a usable title or URL."""
    title, snippet = _clean(title), _clean(snippet)
    url = unwrap_redirect(url.strip())
    if not title or not url.lower().startswith(("http://", "https://")):
        return None
    return SearchHit(title=title, url=normalize_url(url), snippet=snippet, engines=[engine])


class Engine:
    """A single search back end."""

    name: str

    # Whether this engine can answer a flagged client with decoy results.
    # True for engines that scrape a results page, since that is how they get
    # blocked silently. False for search APIs, whose loose matches are real
    # answers to the query -- just weak ones, which ranking already demotes.
    serves_decoys: bool = False

    # The query `doctor` checks this engine with: one it should always answer.
    probe: str = DEFAULT_PROBE

    # The least time between two requests to this engine, for APIs that ask
    # for spacing (arXiv: one request every three seconds).
    min_interval: float = 0.0

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        """Return this engine's ranked results for `query`."""
        raise NotImplementedError

    async def _get(
        self,
        url: str,
        timeout: float,
        headers: dict[str, str] | None = None,
        *,
        allow_private: bool = False,
    ) -> Response:
        """Fetch a results page, refusing anything but a plain 200.

        An engine that has decided a request is a robot often answers 202 with
        a challenge page rather than an error status. That body parses to no
        results, so accepting it would report the engine as working and
        finding nothing.
        """
        await self._wait_for_slot()
        response = await fetch_url(
            url, timeout=timeout, headers=headers, allow_private=allow_private
        )
        if response.status != 200:
            raise EngineBlocked(
                f"{self.name} answered HTTP {response.status}, which is a "
                "challenge page rather than results.",
                status=response.status,
            )
        return response

    async def _wait_for_slot(self) -> None:
        """Hold this request until `min_interval` after the previous one.

        Slots are reserved before sleeping, with no await in between, so
        concurrent searches queue up one interval apart instead of all waking
        at once. The schedule is per process and per engine name.
        """
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        slot = max(now, _NEXT_SLOT.get(self.name, 0.0))
        _NEXT_SLOT[self.name] = slot + self.min_interval
        if slot > now:
            await asyncio.sleep(slot - now)


_NEXT_SLOT: dict[str, float] = {}


def _json(response: Response) -> object:
    """The decoded body, or None when it is not JSON."""
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return None


class DuckDuckGoEngine(Engine):
    """DuckDuckGo, via its no-JavaScript endpoints.

    The lite endpoint is tried first because its markup is small and stable.
    When it answers with something unparseable -- which is what a soft block
    looks like -- the fuller HTML endpoint is tried before giving up.
    """

    name = "duckduckgo"
    serves_decoys = True

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        endpoints = (
            (f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}", "a.result-link", ".result-snippet"),
            (f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", "a.result__a", ".result__snippet"),
        )
        last_error: Exception | None = None
        for url, link_selector, snippet_selector in endpoints:
            try:
                response = await self._get(url, timeout)
            except WebError as exc:
                last_error = exc
                continue
            hits = self._parse(response.text, link_selector, snippet_selector, limit)
            if hits:
                return hits
        raise last_error or soft_block(self.name)

    def _parse(
        self, html: str, link_selector: str, snippet_selector: str, limit: int
    ) -> list[SearchHit]:
        root = parse_html(html)
        links = root.select(link_selector)
        snippets = root.select(snippet_selector)
        hits: list[SearchHit] = []
        for index, link in enumerate(links[:limit]):
            snippet = snippets[index].text() if index < len(snippets) else ""
            hit = _hit(link.text(), link.get("href"), snippet, self.name)
            if hit is not None:
                hits.append(hit)
        return hits


class MojeekEngine(Engine):
    """Mojeek, which runs its own crawl rather than reselling another index.

    An independent index is worth including precisely because it disagrees:
    when it ranks the same page highly as the majors, fusion has real
    corroboration rather than two views of one crawl.
    """

    name = "mojeek"
    serves_decoys = True

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = f"https://www.mojeek.com/search?q={quote_plus(query)}&safe=0"
        response = await self._get(url, timeout)
        root = parse_html(response.text)
        rows = root.select("ul.results-standard li, .results .result")
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            link = row.select_one("a.title") or row.select_one("h2 a") or row.select_one("a")
            if link is None:
                continue
            snippet = row.select_one("p.s") or row.select_one(".description")
            hit = _hit(
                link.text(), link.get("href"), snippet.text() if snippet else "", self.name
            )
            if hit is not None:
                hits.append(hit)
        if not hits:
            raise soft_block(self.name)
        return hits


class BingEngine(Engine):
    """Bing's web results page.

    Result links arrive wrapped in a `bing.com/ck/a` tracker whose real target
    is base64 in the `u` parameter, so they are decoded here; a URL a caller
    cannot fetch is not a result.
    """

    name = "bing"
    serves_decoys = True

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            f"https://www.bing.com/search?q={quote_plus(query)}"
            "&mkt=en-US&setlang=en&format=rss"
        )
        response = await self._get(url, timeout)
        hits = self._parse_rss(response.text, limit)
        if hits:
            return hits
        page = await self._get(
            f"https://www.bing.com/search?q={quote_plus(query)}&mkt=en-US&setlang=en",
            timeout,
        )
        hits = self._parse_html(page.text, limit)
        if not hits:
            raise soft_block(self.name)
        return hits

    def _parse_rss(self, xml: str, limit: int) -> list[SearchHit]:
        """Parse the RSS view, which Bing serves without anti-bot markup."""
        root = parse_html(xml, xml=True)
        hits: list[SearchHit] = []
        for item in root.select("item")[:limit]:
            title = item.select_one("title")
            link = item.select_one("link")
            description = item.select_one("description")
            href = (link.text() if link is not None else "") or (
                link.get("href") if link is not None else ""
            )
            hit = _hit(
                title.text() if title else "",
                href,
                description.text() if description else "",
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    def _parse_html(self, html: str, limit: int) -> list[SearchHit]:
        root = parse_html(html)
        hits: list[SearchHit] = []
        for row in root.select("li.b_algo")[:limit]:
            link = row.select_one("h2 a")
            if link is None:
                continue
            snippet = (
                row.select_one(".b_lineclamp2")
                or row.select_one(".b_lineclamp3")
                or row.select_one(".b_caption p")
            )
            hit = _hit(
                link.text(),
                decode_bing_url(link.get("href")),
                snippet.text() if snippet else "",
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


def decode_bing_url(href: str) -> str:
    """Decode a `bing.com/ck/a` tracker back into its destination URL."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return href
    if not (parts.hostname or "").endswith("bing.com") or parts.path != "/ck/a":
        return href
    encoded = parse_qs(parts.query).get("u", [""])[0]
    if len(encoded) < 4:
        return href
    body = encoded[2:].replace("-", "+").replace("_", "/")
    body += "=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.b64decode(body).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return href
    return decoded if decoded.lower().startswith(("http://", "https://")) else href


class WikipediaEngine(Engine):
    """Wikipedia's full-text search API, as an encyclopedic anchor.

    It answers only when the query names a real subject, which is what makes
    it useful in fusion: it breaks ties toward the topic a word denotes rather
    than the company that happens to own the domain.

    Full-text search rather than OpenSearch, which only matches the start of
    article titles and so found nothing for most multi-word queries.
    """

    name = "wikipedia"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
            f"&srprop=snippet&srlimit={min(max(limit, 1), 50)}&srsearch={quote_plus(query)}"
        )
        response = await self._get(url, timeout, headers={"Accept": "application/json"})
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            return []
        found = body.get("query") if isinstance(body, dict) else None
        rows = found.get("search") if isinstance(found, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not row.get("title"):
                continue
            title = str(row["title"])
            hit = _hit(
                title,
                wikipedia_url(title),
                strip_tags(str(row.get("snippet", ""))),
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


def wikipedia_url(title: str) -> str:
    """The article URL for a Wikipedia page title."""
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="/:(),'!_")


def strip_tags(fragment: str) -> str:
    """Plain text from an HTML fragment such as a highlighted API snippet."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


class MarginaliaEngine(Engine):
    """Marginalia, a non-commercial index of the small web.

    It surfaces long-tail pages the commercial crawlers deprioritise, which is
    where a technical answer often actually lives. Its public tier is reached
    with the literal key ``public`` and rate limits by returning 503.
    """

    name = "marginalia"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            "https://api2.marginalia-search.com/search"
            f"?query={quote_plus(query)}&count={limit}&dc=3"
        )
        response = await self._get(
            url, timeout, headers={"Accept": "application/json", "API-Key": "public"}
        )
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            return []
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            hit = _hit(
                str(row.get("title", "")),
                str(row.get("url", "")),
                str(row.get("description", "")),
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


class HackerNewsEngine(Engine):
    """The Hacker News search API, for discussion and primary sources.

    Included because it answers a question the web engines cannot: whether
    practitioners have already argued about this, and what they linked to.
    """

    name = "hackernews"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            "https://hn.algolia.com/api/v1/search"
            f"?query={quote_plus(query)}&tags=story&hitsPerPage={limit}"
        )
        response = await self._get(url, timeout, headers={"Accept": "application/json"})
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            return []
        rows = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            story = row.get("objectID", "")
            url_value = row.get("url") or f"https://news.ycombinator.com/item?id={story}"
            points, comments = row.get("points", 0), row.get("num_comments", 0)
            hit = _hit(
                str(row.get("title", "")),
                str(url_value),
                f"Hacker News discussion: {points} points, {comments} comments.",
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


class StackExchangeEngine(Engine):
    """Stack Overflow, through the Stack Exchange API.

    For a how-to question this is where the answer usually is, and the API
    returns the questions themselves rather than a page about them, with
    votes and answer counts to judge them by. It is keyless at about 300
    requests a day per IP; past that it answers HTTP 400 with a throttle
    error, which is reported as a block so the engine is paused.
    """

    name = "stackexchange"
    probe = "python asyncio taskgroup cancel"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            "https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
            f"&site=stackoverflow&pagesize={min(max(limit, 1), 100)}&q={quote_plus(query)}"
        )
        try:
            response = await self._get(url, timeout, headers={"Accept": "application/json"})
        except WebError as exc:
            if exc.status == 400:
                raise EngineBlocked(
                    "stackexchange refused the request (HTTP 400), which is how it "
                    "reports an exhausted daily quota.",
                    status=400,
                ) from exc
            raise
        body = _json(response)
        rows = body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            answered = "answered" if row.get("is_answered") else "unanswered"
            tags = ", ".join(str(t) for t in row.get("tags", [])[:5])
            snippet = (
                f"Stack Overflow question, {row.get('score', 0)} votes, "
                f"{row.get('answer_count', 0)} answers ({answered})."
                + (f" Tags: {tags}." if tags else "")
            )
            hit = _hit(
                html.unescape(str(row.get("title", ""))),
                str(row.get("link", "")),
                snippet,
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


class OpenAlexEngine(Engine):
    """OpenAlex, an open index of scholarly works across every field.

    It ranks well -- the paper that coined a term tends to come first -- and
    carries each work's open-access copy where one exists. That copy is
    preferred as the result URL: a publisher's DOI landing page often refuses
    automated requests, and a result `fetch` cannot open is half a result.
    """

    name = "openalex"
    probe = "crispr off-target effects"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            f"https://api.openalex.org/works?search={quote_plus(query)}"
            f"&per-page={min(max(limit, 1), 50)}"
            "&select=id,doi,display_name,publication_year,cited_by_count,"
            "primary_location,best_oa_location,abstract_inverted_index"
        )
        response = await self._get(url, timeout, headers={"Accept": "application/json"})
        body = _json(response)
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for work in rows[:limit]:
            if not isinstance(work, dict):
                continue
            primary = work.get("primary_location") or {}
            open_copy = work.get("best_oa_location") or {}
            target = (
                open_copy.get("landing_page_url")
                or primary.get("landing_page_url")
                or work.get("doi")
                or work.get("id")
                or ""
            )
            venue = (primary.get("source") or {}).get("display_name") or ""
            details = ", ".join(
                str(part)
                for part in (
                    work.get("publication_year"),
                    venue,
                    f"cited by {work.get('cited_by_count', 0)}",
                )
                if part
            )
            abstract = abstract_text(work.get("abstract_inverted_index"))
            snippet = f"{details}. {abstract}" if abstract else details
            hit = _hit(str(work.get("display_name") or ""), str(target), snippet[:400], self.name)
            if hit is not None:
                hits.append(hit)
        return hits


def abstract_text(inverted: object, max_words: int = 60) -> str:
    """Rebuild the start of an abstract from OpenAlex's inverted index.

    OpenAlex ships abstracts as ``{word: [positions]}`` for licensing reasons;
    laying the words back out by position recovers the text.
    """
    if not isinstance(inverted, dict):
        return ""
    placed: dict[int, str] = {}
    for word, positions in inverted.items():
        if isinstance(positions, list):
            for position in positions:
                if isinstance(position, int) and position < max_words:
                    placed[position] = str(word)
    words = [placed[i] for i in sorted(placed)]
    text = " ".join(words)
    return f"{text} ..." if len(inverted) and len(words) >= max_words else text


class ArxivEngine(Engine):
    """arXiv, for preprints in physics, mathematics, and computer science.

    Its API asks for one request every three seconds, which `min_interval`
    enforces across every search in the process. Query words are ANDed, since
    arXiv's default would match a paper mentioning any one of them.
    """

    name = "arxiv"
    probe = "crispr off-target effects"
    min_interval = 3.0

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        terms = query_terms(query) or re.findall(r"[a-z0-9]+", query.lower())
        if not terms:
            return []
        search_query = "+AND+".join(f"all:{quote_plus(term)}" for term in terms)
        url = (
            f"https://export.arxiv.org/api/query?search_query={search_query}"
            f"&sortBy=relevance&max_results={min(max(limit, 1), 50)}"
        )
        response = await self._get(url, timeout)
        root = parse_html(response.text, xml=True)
        hits: list[SearchHit] = []
        for entry in root.select("entry")[:limit]:
            link = entry.select_one("id")
            title = entry.select_one("title")
            summary = entry.select_one("summary")
            published = entry.select_one("published")
            href = (link.text() if link else "").strip()
            if href.startswith("http://arxiv.org/"):
                href = "https://" + href.removeprefix("http://")
            year = (published.text() if published else "")[:4]
            snippet = f"arXiv preprint{', ' + year if year else ''}. " + _clean(
                summary.text() if summary else ""
            )
            hit = _hit(title.text() if title else "", href, snippet[:400], self.name)
            if hit is not None:
                hits.append(hit)
        return hits


class PubMedEngine(Engine):
    """PubMed, through NCBI's E-utilities, for biomedical literature.

    Two requests per search -- one for the matching IDs, one for their
    summaries. NCBI allows three requests a second without a key, which
    `min_interval` keeps under.
    """

    name = "pubmed"
    probe = "crispr off-target effects"
    min_interval = 0.34

    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        found = _json(
            await self._get(
                f"{self.BASE}/esearch.fcgi?db=pubmed&retmode=json&sort=relevance"
                f"&tool=webless&retmax={min(max(limit, 1), 100)}&term={quote_plus(query)}",
                timeout,
                headers={"Accept": "application/json"},
            )
        )
        result = found.get("esearchresult") if isinstance(found, dict) else None
        ids = [str(i) for i in (result or {}).get("idlist", []) if str(i).isdigit()]
        if not ids:
            return []
        summaries = _json(
            await self._get(
                f"{self.BASE}/esummary.fcgi?db=pubmed&retmode=json&tool=webless"
                f"&id={','.join(ids[:limit])}",
                timeout,
                headers={"Accept": "application/json"},
            )
        )
        records = summaries.get("result") if isinstance(summaries, dict) else None
        if not isinstance(records, dict):
            return []
        hits: list[SearchHit] = []
        for pmid in ids[:limit]:
            record = records.get(pmid)
            if not isinstance(record, dict):
                continue
            authors = record.get("authors") or []
            first = authors[0].get("name", "") if authors and isinstance(authors[0], dict) else ""
            byline = f"{first} et al." if len(authors) > 1 else first
            details = ", ".join(
                part
                for part in (byline, str(record.get("source", "")), str(record.get("pubdate", "")))
                if part
            )
            hit = _hit(
                str(record.get("title", "")),
                f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                f"PubMed: {details}.",
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


class BingNewsEngine(Engine):
    """Bing News, through its RSS view, for recent reporting.

    Each link is a `bing.com/news/apiclick.aspx` tracker carrying the article
    URL in its `url` parameter, decoded here so results point at the
    publisher. Scraped from Bing, so it is checked for decoys like Bing's web
    results are.
    """

    name = "bingnews"
    serves_decoys = True
    probe = "federal reserve interest rates"

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        url = (
            f"https://www.bing.com/news/search?q={quote_plus(query)}"
            "&format=rss&mkt=en-US&setlang=en"
        )
        response = await self._get(url, timeout)
        root = parse_html(response.text, xml=True)
        hits: list[SearchHit] = []
        for item in root.select("item")[:limit]:
            title = item.select_one("title")
            link = item.select_one("link")
            description = item.select_one("description")
            published = item.select_one("pubdate")  # tag names are lowercased
            snippet = " ".join(
                part
                for part in (
                    description.text() if description else "",
                    f"({published.text()})" if published else "",
                )
                if part
            )
            hit = _hit(
                title.text() if title else "",
                decode_bing_news_url(link.text() if link else ""),
                snippet,
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        if not hits and root.select_one("channel") is None:
            raise soft_block(self.name)
        return hits


def decode_bing_news_url(href: str) -> str:
    """The article behind a Bing News `apiclick.aspx` tracker.

    Returns an empty string for a tracker with no usable target, so the item
    is dropped rather than listed as a link back to Bing.
    """
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return ""
    if not (parts.hostname or "").endswith("bing.com") or not parts.path.lower().endswith(
        "/apiclick.aspx"
    ):
        return href
    target = parse_qs(parts.query).get("url", [""])[0]
    return target if target.lower().startswith(("http://", "https://")) else ""


class SearXNGEngine(Engine):
    """A SearXNG server you run, through its JSON API.

    SearXNG is a self-hosted metasearch engine: one request here fans out to
    whichever engines the server has enabled, Google and Brave among them,
    which no keyless public endpoint reaches from most networks. Point
    `search(searxng_url=...)` or the ``WEBLESS_SEARXNG_URL`` environment
    variable at it. The server must allow JSON output -- ``formats: [html,
    json]`` under ``search:`` in its settings.yml -- or it answers 403.

    The server is usually on localhost, so this one request is exempt from the
    private-address guard. The URLs it returns are guarded like any other
    when fetched.
    """

    name = "searxng"
    base_url: str = ""

    async def search(self, query: str, limit: int, timeout: float) -> list[SearchHit]:
        base = (self.base_url or os.environ.get(SEARXNG_ENV, "")).strip().rstrip("/")
        if not base:
            raise WebError(
                f"searxng has no server: pass searxng_url or set {SEARXNG_ENV}."
            )
        try:
            response = await self._get(
                f"{base}/search?q={quote_plus(query)}&format=json",
                timeout,
                headers={"Accept": "application/json"},
                allow_private=True,
            )
        except WebError as exc:
            if exc.status == 403:
                raise WebError(
                    f"{base} refused format=json (HTTP 403); enable it with "
                    "`formats: [html, json]` under `search:` in the server's settings.yml.",
                    status=403,
                ) from exc
            raise
        body = _json(response)
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise WebError(f"{base} did not answer with SearXNG JSON results.")
        hits: list[SearchHit] = []
        for row in rows:
            if len(hits) >= limit:
                break
            if not isinstance(row, dict):
                continue
            hit = _hit(
                str(row.get("title", "")),
                str(row.get("url", "")),
                str(row.get("content", "")),
                self.name,
            )
            if hit is not None:
                hits.append(hit)
        return hits


def searxng(url: str) -> type[SearXNGEngine]:
    """An engine class bound to the SearXNG server at `url`."""
    return type("SearXNGEngine", (SearXNGEngine,), {"base_url": url.strip().rstrip("/")})


GENERAL_ENGINES: tuple[type[Engine], ...] = (
    DuckDuckGoEngine,
    MojeekEngine,
    BingEngine,
    WikipediaEngine,
)

WIDE_ENGINES: tuple[type[Engine], ...] = GENERAL_ENGINES + (
    MarginaliaEngine,
    HackerNewsEngine,
)

TOPIC_ENGINES: dict[str, tuple[type[Engine], ...]] = {
    "code": (StackExchangeEngine,),
    "science": (OpenAlexEngine, ArxivEngine, PubMedEngine),
    "news": (BingNewsEngine,),
}

TOPICS = tuple(TOPIC_ENGINES)

ALL_ENGINES: tuple[type[Engine], ...] = WIDE_ENGINES + tuple(
    dict.fromkeys(cls for group in TOPIC_ENGINES.values() for cls in group)
)

ENGINES_BY_NAME = {cls.name: cls for cls in ALL_ENGINES}


def engines_for(
    topic: str | None = None,
    *,
    wide: bool = False,
    engines: tuple[type[Engine], ...] | None = None,
    searxng_url: str | None = None,
) -> tuple[type[Engine], ...]:
    """The engine set for a search.

    Starts from `engines` if given, else the wide or general set. A `topic`
    adds its specialists rather than replacing the web: a code question still
    gets general results, with Stack Overflow's on top. A SearXNG server is
    added when `searxng_url` names one, or -- unless `engines` was given
    explicitly -- when ``WEBLESS_SEARXNG_URL`` is set. Pass ``searxng_url=""``
    to leave it out regardless.
    """
    chosen = list(engines if engines is not None else (WIDE_ENGINES if wide else GENERAL_ENGINES))
    if topic is not None:
        if topic not in TOPIC_ENGINES:
            raise ValueError(f"topic must be one of {', '.join(TOPICS)}; got {topic!r}")
        chosen += [cls for cls in TOPIC_ENGINES[topic] if cls not in chosen]
    server = searxng_url
    if server is None and engines is None:
        server = os.environ.get(SEARXNG_ENV, "")
    if server and not any(issubclass(cls, SearXNGEngine) for cls in chosen):
        chosen.append(searxng(server))
    return tuple(chosen)


def fuse(ranked: list[list[SearchHit]], k: int = RRF_K) -> list[SearchHit]:
    """Merge per-engine rankings into one list by Reciprocal Rank Fusion.

    Results are keyed on a normalized URL so the same page found by several
    engines becomes one hit crediting all of them, and the engine that ranked
    it best supplies the title and snippet.
    """
    merged: dict[str, SearchHit] = {}
    best_rank: dict[str, int] = {}

    for hits in ranked:
        for rank, hit in enumerate(hits, start=1):
            key = dedup_key(hit.url)
            existing = merged.get(key)
            if existing is None:
                merged[key] = SearchHit(
                    title=hit.title,
                    url=hit.url,
                    snippet=hit.snippet,
                    engines=list(hit.engines),
                    score=1 / (k + rank),
                )
                best_rank[key] = rank
                continue
            existing.score += 1 / (k + rank)
            for engine in hit.engines:
                if engine not in existing.engines:
                    existing.engines.append(engine)
            if rank < best_rank[key]:
                best_rank[key] = rank
                existing.title = hit.title
                if hit.snippet:
                    existing.snippet = hit.snippet
            elif not existing.snippet and hit.snippet:
                existing.snippet = hit.snippet

    return sorted(merged.values(), key=lambda h: (-h.score, h.title.lower()))


def query_terms(query: str) -> list[str]:
    """The content words of a query, lowercased and de-duplicated."""
    seen: list[str] = []
    for token in re.findall(r"[a-z0-9]+", query.lower()):
        if len(token) < 3 or token in STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
    return seen


def alignment(terms: list[str], hit: SearchHit) -> float:
    """The share of query terms that appear in a hit's title, snippet, or URL."""
    if not terms:
        return 1.0
    haystack = f"{hit.title} {hit.snippet} {hit.url}".lower()
    return sum(1 for term in terms if term in haystack) / len(terms)


def rank_by_relevance(query: str, hits: list[SearchHit]) -> list[SearchHit]:
    """Reweight fused hits by how well they match the query's own words.

    Fusion on its own is topic-blind: every engine's first result gets the
    same credit, so a back end that answers a loose query loosely -- a forum
    search matching one word of five -- lands a stranger at the top of the
    list. Scaling each score by the share of query terms the result actually
    mentions costs an on-topic result nothing and pushes those strangers down,
    while leaving cross-engine agreement as the deciding signal among results
    that are all about the right thing.
    """
    terms = query_terms(query)
    for hit in hits:
        hit.score *= 1 + alignment(terms, hit)
    return sorted(hits, key=lambda h: (-h.score, h.title.lower()))


def looks_like_decoy(query: str, hits: list[SearchHit]) -> bool:
    """Whether a page of results ignores the query it was asked.

    A hit counts as on topic when it mentions at least half the query's
    content words; the page is a decoy when almost none of its hits are. Too
    few hits, or a query with no content words, is not enough to judge, and
    those are let through.
    """
    terms = query_terms(query)
    if not terms or len(hits) < DECOY_MIN_HITS:
        return False
    on_topic = sum(1 for hit in hits if alignment(terms, hit) >= ON_TOPIC_ALIGNMENT)
    return on_topic / len(hits) < DECOY_ON_TOPIC_SHARE


@dataclass(slots=True)
class _EngineState:
    failures: int = 0
    paused_until: float = 0.0
    reason: str = ""


class EngineHealth:
    """Remembers which engines are failing, and pauses them for a while.

    A blocked engine does not unblock between one call and the next, so
    querying it again costs a full request -- often a full timeout -- for a
    known answer, and keeps reminding the engine that this client exists. A
    block pauses an engine at once; any other failure pauses it after
    `failures_before_cooldown` in a row. One success clears the record.

    State lives in the process, shared by every search in it. The module-level
    `ENGINE_HEALTH` is the instance `search` uses; call its `reset()` to try
    every engine again straight away.
    """

    def __init__(
        self,
        *,
        cooldown: float = BLOCK_COOLDOWN_S,
        failures_before_cooldown: int = FAILURES_BEFORE_COOLDOWN,
        clock=time.monotonic,
    ) -> None:
        self.cooldown = cooldown
        self.failures_before_cooldown = failures_before_cooldown
        self._clock = clock
        self._states: dict[str, _EngineState] = {}

    def paused(self, name: str) -> str | None:
        """Why `name` is being skipped, or None when it should be queried."""
        state = self._states.get(name)
        if state is None:
            return None
        remaining = state.paused_until - self._clock()
        if remaining <= 0:
            return None
        return f"skipped for another {remaining:.0f}s after a recent failure: {state.reason}"

    def record_success(self, name: str) -> None:
        """Clear `name`'s failure record."""
        self._states.pop(name, None)

    def record_failure(self, name: str, reason: str, *, blocked: bool) -> None:
        """Count a failure, pausing `name` if it was blocked or keeps failing."""
        state = self._states.setdefault(name, _EngineState())
        state.failures += 1
        state.reason = reason
        if blocked or state.failures >= self.failures_before_cooldown:
            state.paused_until = self._clock() + self.cooldown

    def reset(self, name: str | None = None) -> None:
        """Forget one engine's failures, or every engine's."""
        if name is None:
            self._states.clear()
        else:
            self._states.pop(name, None)


ENGINE_HEALTH = EngineHealth()


def apply_domain_filters(
    hits: list[SearchHit],
    allowed: list[str] | None,
    blocked: list[str] | None,
) -> list[SearchHit]:
    """Keep only hits whose host passes the allow and block lists.

    A list entry matches its own domain and every subdomain of it, so
    ``python.org`` covers ``docs.python.org``.
    """

    def matches(host: str, domain: str) -> bool:
        domain = domain.strip().lower().removeprefix("www.")
        host = host.removeprefix("www.")
        return bool(domain) and (host == domain or host.endswith(f".{domain}"))

    result = hits
    if allowed:
        result = [h for h in result if any(matches(h.host, d) for d in allowed)]
    if blocked:
        result = [h for h in result if not any(matches(h.host, d) for d in blocked)]
    return result


@dataclass(slots=True)
class SearchReport:
    """The outcome of one fused search, including what each engine did."""

    hits: list[SearchHit]
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


async def search_web(
    query: str,
    *,
    limit: int = 10,
    engines: tuple[type[Engine], ...] = GENERAL_ENGINES,
    timeout: float = 12.0,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    health: EngineHealth | None = ENGINE_HEALTH,
) -> SearchReport:
    """Query every engine concurrently and return the fused ranking.

    Each engine is given the same deadline and none can hold up the others: a
    failure is recorded against its name and the rest of the results stand.
    Only when every engine fails does the caller get an empty ranking with the
    reasons attached.

    Engines `health` has paused are skipped and listed in `failed` -- unless
    every engine is paused, in which case all of them are tried anyway, since
    a long shot beats a certain empty answer. Pass `health=None` to query
    every engine regardless.
    """
    instances = [cls() for cls in engines]
    per_engine = max(limit, 10)
    report = SearchReport(hits=[])

    skipped = {
        e.name: reason
        for e in instances
        if health is not None and (reason := health.paused(e.name)) is not None
    }
    active = [e for e in instances if e.name not in skipped]
    if not active:
        active, skipped = instances, {}
    report.failed.update(skipped)

    async def run(engine: Engine) -> tuple[str, list[SearchHit] | str, bool]:
        try:
            hits = await asyncio.wait_for(
                engine.search(query, per_engine, timeout), timeout=timeout + 2
            )
        except asyncio.TimeoutError:
            return engine.name, f"timed out after {timeout:g}s", False
        except WebError as exc:
            return engine.name, str(exc), is_block(exc)
        except Exception as exc:
            return engine.name, f"{type(exc).__name__}: {exc}", False
        if engine.serves_decoys and looks_like_decoy(query, hits):
            return engine.name, str(decoy_block(engine.name)), True
        return engine.name, hits, False

    outcomes = await asyncio.gather(*(run(e) for e in active))

    ranked: list[list[SearchHit]] = []
    for name, outcome, blocked in outcomes:
        if isinstance(outcome, str):
            report.failed[name] = outcome
            if health is not None:
                health.record_failure(name, outcome, blocked=blocked)
            continue
        report.succeeded.append(name)
        if health is not None:
            health.record_success(name)
        if outcome:
            ranked.append(outcome)

    fused = rank_by_relevance(query, fuse(ranked))
    report.hits = apply_domain_filters(fused, allowed_domains, blocked_domains)[:limit]
    return report


__all__ = [
    "Engine",
    "EngineBlocked",
    "EngineHealth",
    "ENGINE_HEALTH",
    "SearchHit",
    "SearchReport",
    "GENERAL_ENGINES",
    "WIDE_ENGINES",
    "TOPIC_ENGINES",
    "TOPICS",
    "ALL_ENGINES",
    "ENGINES_BY_NAME",
    "engines_for",
    "searxng",
    "SEARXNG_ENV",
    "DEFAULT_PROBE",
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
    "abstract_text",
    "decode_bing_news_url",
    "search_web",
    "fuse",
    "rank_by_relevance",
    "query_terms",
    "alignment",
    "looks_like_decoy",
    "is_block",
    "soft_block",
    "decoy_block",
    "apply_domain_filters",
    "decode_bing_url",
    "unwrap_redirect",
]
