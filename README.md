# webless

Web search and page fetching for Python, with **no API keys**.

`pip install webless` and the first call works. There is no account to
create, no key to configure, no quota to track, and no paid tier — every
back end is a public endpoint.

```python
import asyncio
from webless import search, fetch

async def main():
    result = await search("reciprocal rank fusion")
    for hit in result[:3]:
        print(hit.title, "—", hit.url)

    page = await fetch(result[0].url)
    print(page.content[:500])

asyncio.run(main())
```

No event loop of your own? Use `search_sync` and `fetch_sync`, which take the
same arguments.

## Install

```bash
pip install webless
```

For the [MCP server](#mcp-server), install the extra:
`pip install "webless[mcp]"`.

The only dependency is `httpx`. HTML parsing, content extraction, and Markdown
rendering are all written against the standard library, so there is no
`lxml`, no `beautifulsoup4`, and no compiler needed to install.

## `search`

```python
result = await search(
    "sqlite write-ahead logging",
    limit=10,
    wide=False,
    topic=None,               # "code", "science", or "news"
    searxng_url=None,         # a SearXNG server you run
    allowed_domains=["sqlite.org"],
    blocked_domains=["pinterest.com"],
    timeout=12.0,
)
```

`search` queries several engines **concurrently** and fuses their rankings.
The general set is DuckDuckGo, Mojeek, Bing, and Wikipedia; `wide=True` adds
Marginalia's small-web index and Hacker News, which helps on niche or
discussion-oriented queries and costs some latency on ordinary ones.

Fusion is Reciprocal Rank Fusion: a result scores `Σ 1 / (60 + rank)` over the
engines that returned it. Rank is all it needs, which is the point — the
engines disagree about scoring and none exposes a comparable number, but they
all produce an ordered list, and a page several independent indexes rank
highly is a better answer than any one index's favourite. The fused ranking is
then reweighted by how much of the query actually appears in each result, so
an engine answering loosely cannot outrank one answering the question.

`allowed_domains` and `blocked_domains` filter on host, and an entry covers
its subdomains: `python.org` keeps `docs.python.org`.

### Topics

A general engine buries the answer to a specialist question under SEO pages,
and the specialist indexes have stable public APIs. `topic=` **adds** their
engines to the general set, so their results are fused in rather than
replacing the web:

| topic | adds | good for |
|---|---|---|
| `code` | Stack Overflow (Stack Exchange API) | errors, how-tos; snippet shows votes, answers, and tags |
| `science` | OpenAlex, arXiv, PubMed | papers; snippet shows year, venue, citations, abstract |
| `news` | Bing News (RSS) | recent reporting, linked straight to the publisher |

```python
await search("python asyncio taskgroup cancel", topic="code")
await search("crispr off-target effects", topic="science")
```

OpenAlex results link to a paper's open-access copy where one exists, since
publishers' DOI pages often refuse automated requests. arXiv asks for one
request every three seconds, and NCBI for at most three a second without a
key; both are enforced per process, so concurrent searches queue rather than
get refused. Stack Exchange allows about 300 keyless requests a day per IP;
past that it is reported as blocked and paused like any other engine.

### SearXNG

[SearXNG](https://docs.searxng.org/) is a self-hosted metasearch engine: one
request fans out to Google, Brave, and whatever else the server has enabled,
which no keyless public endpoint reaches from most networks. If you run one,
webless can use it as another engine:

```bash
docker run -d -p 8888:8080 searxng/searxng
export WEBLESS_SEARXNG_URL=http://localhost:8888
```

or `search(..., searxng_url="http://localhost:8888")`. The server must allow
JSON output — add `json` to `search: formats:` in its `settings.yml`, or it
answers 403 (webless says so). Because the server is usually on localhost,
that one request is exempt from the private-address guard below; the URLs it
returns are guarded like any other. Passing `engines=` explicitly leaves the
environment variable out, and `searxng_url=""` always does.

### Results

`SearchResult` iterates, indexes, and measures like the list of hits it
carries, and also tells you what happened:

```python
result.hits        # list[SearchHit]
result.succeeded   # ["duckduckgo", "bing"]
result.failed      # {"mojeek": "returned a page with no results, likely a soft block."}
result.ok          # at least one engine answered

hit.title, hit.url, hit.snippet, hit.host, hit.engines, hit.score
```

**A blocked engine is reported, not raised.** Public endpoints rate-limit,
soft-block, and serve challenge pages, and they do it differently from
different IPs. Every engine is given the same deadline and none can hold up
the others: a failure is recorded against its name and the rest of the ranking
stands. Only when all of them fail do you get an empty ranking — with the
reasons attached.

A soft block is detected rather than mistaken for an empty index. A results
page that returns HTTP 200 and parses to zero rows, or answers with a
challenge status, is reported as that engine failing.

So is a **decoy block**: an engine that has flagged a client may answer with a
full page of real-looking results that ignore the query (Bing, asked for
"reciprocal rank fusion", has been seen serving Outlook login pages). When
fewer than a fifth of a scraped engine's results mention at least half the
query's words, the page is reported as `"... returned off-topic results,
likely a decoy block."` rather than counted as an answer. Search APIs
(Wikipedia, Marginalia, Hacker News) are not judged this way: their loose
matches are genuine, just weak, and ranking already demotes them.

**A blocked engine is paused.** An engine that was just blocked is likely to
stay blocked, so it is skipped for five minutes rather than costing a request
on every call; other failures pause it after three in a row, and one success
clears the record. Skipped engines appear in `failed` with the reason. If
every engine is paused, all of them are tried anyway. The state is per
process, in `webless.ENGINE_HEALTH`; `ENGINE_HEALTH.reset()` forgets it.

## `fetch`

```python
page = await fetch(
    "https://docs.python.org/3/library/asyncio.html",
    format="markdown",
    full_page=False,
    max_chars=None,
    offset=0,
)
```

`fetch` returns the page's readable content, not its markup. A page's HTML is
mostly navigation, scripts, and layout, so the default trims to the article
body and renders headings, lists, code blocks, tables, and links as Markdown —
small enough to read or feed to a model, structured enough to still quote from
and follow links out of. Relative links are absolutised against the final URL.

- `format="markdown"` — main content as Markdown (default)
- `format="text"` — plain text, no markup
- `format="raw"` — the untouched response body
- `full_page=True` — keep navigation, sidebars, and footers, for when the part
  you want *is* the chrome (a docs index, a link list)
- `max_chars` / `offset` — read a long page in sections; `page.truncated` says
  whether anything was left behind

JSON responses are returned pretty-printed.

**Stack Exchange questions are read through the API.** Stack Overflow answers
HTTP 403 to automated page requests, so a question URL on any Stack Exchange
site is fetched from the public API instead and rendered as the question
followed by up to five answers, accepted first, then by score. If the API is
unavailable the page itself is tried; `format="raw"` always fetches the page.

```python
page.content       # the rendered text
page.title         # <og:title>, else <title>
page.description   # meta description
page.url           # what you asked for
page.final_url     # where you ended up
page.redirected    # whether those differ
page.status, page.content_type, page.host, page.truncated
```

Already holding a response from your own client or a cache? `render(response,
format=..., full_page=...)` does the same conversion with no network call.

## Checking the engines

Public endpoints change their markup and block clients without notice, and
which ones answer depends on the network you are on. `webless doctor` asks
every engine (and your SearXNG server, if configured) a probe query suited to
it and reports what came back:

```text
$ webless doctor
engine         status   hits on-topic   time  query
duckduckgo     ok         10     100%   0.8s  sqlite write-ahead logging
mojeek         empty       0       0%   0.4s  sqlite write-ahead logging
               mojeek returned a page with no results, likely a soft block.
bing           decoy      10       0%   0.3s  sqlite write-ahead logging
               bing returned off-topic results, likely a decoy block.
wikipedia      ok          7      57%   0.5s  sqlite write-ahead logging
...
stackexchange  ok          2     100%   0.4s  python asyncio taskgroup cancel
openalex       ok         10     100%   0.8s  crispr off-target effects
pubmed         ok         10      70%   0.9s  crispr off-target effects
bingnews       ok         10     100%   0.4s  federal reserve interest rates
```

The status is `ok`, `blocked` (challenge, 401, 403, or 429), `decoy`,
`empty` (soft block or changed markup), `timeout`, or `error`. It exits
non-zero unless every engine is working; `--engine NAME` checks just one,
`--query` asks every engine the same thing, `--json` prints machine-readable
output, and `doctor()` / `check_engine()`
do the same from Python. It bypasses the pause above and does not record
into it.

The test suite runs offline against saved pages. The live tests ask the real
engines and run only on request:

```bash
uv run pytest -m live -rs
```

An engine refusing your network outright is skipped with the reason; an empty
page or decoy results fail, since from outside they cannot be told apart from
a parser that no longer matches. `.github/workflows/live.yml` runs them
weekly.

## Not blocking

Two senses, both deliberate.

**Nothing blocks anything else.** Every call is async with a hard timeout. A
search is several concurrent requests, so a slow engine delays its own result
and nothing more.

**Requests are hard to block.** Hosts refuse on fingerprint reputation, so a
retry after a 403 or 429 presents a *different* browser user agent rather than
repeating the one just refused — which clears most transient blocks on its
own. Retries back off exponentially with jitter.

Requests on one event loop share a single HTTP client, so connections are
reused and cookies an engine sets are sent back, as a browser would; some
anti-bot checks flag a client that never returns them. `await
webless.aclose()` releases it early; otherwise it goes with its loop, and the
`*_sync` wrappers close theirs after each call.

## Not reachable inward

A URL that resolves to a private, loopback, link-local, or reserved address is
refused before the request is sent — and again on every redirect hop, since
redirects are followed by hand for exactly that reason. A URL from a search
result, a user, or a model cannot be steered into your own network.

```python
await fetch("http://169.254.169.254/latest/meta-data/")
# WebError: ... resolves to a private or loopback address; refusing to fetch it
```

Response bodies are capped, and redirect chains are limited to five hops.

## Command line

```bash
webless search "structured concurrency python" -n 5
webless search "rust async traits" --wide --allow rust-lang.org
webless search "rust borrow checker E0502" --topic code
webless search "dark matter rotation curves" -t science --searxng http://localhost:8888
webless fetch https://peps.python.org/pep-3156/ > pep.md
webless fetch https://api.github.com/repos/python/cpython --json | jq .status
```

`--json` on either subcommand prints the full record, so it composes with
`jq`. `webless mcp` runs the [MCP server](#mcp-server). `search` exits non-zero when every engine failed.

## MCP server

webless runs as a [Model Context Protocol](https://modelcontextprotocol.io)
server, so any MCP client — Claude Code, Claude Desktop, Cursor, VS Code,
Zed, or your own agent — can search and read the web through it, with no API
key to configure.

| tool | does |
|---|---|
| `web_search` | fused multi-engine search; `topic` (`code`, `science`, `news`), `limit`, `allowed_domains`, `blocked_domains`, `wide` |
| `web_fetch` | a page as Markdown or text; long pages are read in sections with `offset`, and the result says where to continue |
| `search_engine_status` | `webless doctor`: which engines are answering from this network |

All three are marked read-only and open-world, so clients that ask before
running a tool can treat them accordingly.

### Running it

```bash
webless-mcp                      # stdio, for clients that launch the server
webless mcp                      # the same, as a subcommand
webless mcp --transport http     # Streamable HTTP at http://127.0.0.1:8000/mcp
webless mcp --transport http --host 0.0.0.0 --port 9000 --path /mcp
```

Use stdio for a client on the same machine; it starts and stops the server
itself. Use HTTP to share one server between clients or across a network,
which keeps the engine pause state (and SearXNG configuration) in one
place. The HTTP server has no authentication, so it listens on localhost
unless you pass `--host`; put it behind something that authenticates before
exposing it.

With [uv](https://docs.astral.sh/uv/), `uvx --from "webless[mcp]" webless-mcp`
runs the server without installing anything first, which is what the
configurations below use. To run a local checkout instead, replace the
command with `uv` and the arguments with `["run", "--directory",
"/path/to/webless", "webless-mcp"]`.

### Adding it to a client

**Claude Code**

```bash
claude mcp add webless -- uvx --from "webless[mcp]" webless-mcp
```

**Claude Desktop** (`claude_desktop_config.json`), **Cursor**
(`~/.cursor/mcp.json`), and most other clients:

```json
{
  "mcpServers": {
    "webless": {
      "command": "uvx",
      "args": ["--from", "webless[mcp]", "webless-mcp"]
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "webless": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "webless[mcp]", "webless-mcp"]
    }
  }
}
```

**A client that connects to a URL:** start `webless mcp --transport http`
and point it at `http://127.0.0.1:8000/mcp`.

To add a SearXNG server, set `WEBLESS_SEARXNG_URL` in the server's
environment — `"env": {"WEBLESS_SEARXNG_URL": "http://localhost:8888"}` in the
JSON configurations, or `-e WEBLESS_SEARXNG_URL=...` with `claude mcp add`.

### Trying it by hand

```bash
npx @modelcontextprotocol/inspector uvx --from "webless[mcp]" webless-mcp
```

opens the MCP Inspector, where you can call each tool and see exactly what a
client receives.

### Safety

The URLs `web_fetch` opens are chosen by a model, which may be reading pages
written by anyone. The private-address guard (see [Not reachable
inward](#not-reachable-inward)) is what stops a page from talking the model
into reading `localhost`, your LAN, or a cloud metadata endpoint through this
server, and it applies to every fetch, including each redirect. Results and
page sections are capped (25 results, 100,000 characters) so a single call
cannot flood the model's context.

## Engines

| engine | how | notes |
|---|---|---|
| DuckDuckGo | scrape | lite view, falls back to the html view |
| Mojeek | scrape | independent index |
| Bing | RSS, then scrape | tracker URLs decoded |
| Wikipedia | full-text search API | |
| Marginalia | public API | `wide` only; small-web index |
| Hacker News | Algolia API | `wide` only; discussion |
| Stack Overflow | Stack Exchange API | `topic="code"`; ~300 requests/day/IP |
| OpenAlex | public API | `topic="science"`; all fields |
| arXiv | Atom API | `topic="science"`; one request per 3 s |
| PubMed | NCBI E-utilities | `topic="science"`; biomedical |
| Bing News | RSS | `topic="news"`; tracker URLs decoded |
| SearXNG | your server's JSON API | when `searxng_url` or `WEBLESS_SEARXNG_URL` is set |

Pick your own set with `engines=`:

```python
from webless import search, DuckDuckGoEngine, MojeekEngine

result = await search("query", engines=(DuckDuckGoEngine, MojeekEngine))
```

A custom engine is any subclass of `Engine` with a `name` and an
`async search(query, limit, timeout) -> list[SearchHit]`. Optionally set
`probe` (the query `doctor` checks it with), `min_interval` (seconds between
its requests), and `serves_decoys` (judge its results for decoy blocks).

## Honest caveats

These are public endpoints being read by a program, which has consequences
worth knowing before you build on them:

- **Availability varies by IP.** Mojeek soft-blocks some address ranges
  outright; Marginalia's public key is shared and rate-limits. This is why
  failures are per-engine and reported rather than fatal.
- **Bing occasionally serves results for an unrelated query** — the same
  response carries the right query in its metadata and the wrong results in
  its body. The relevance reweighting demotes them, but on a query where Bing
  is the only engine answering you may see them.
- **arXiv is slow at times.** The same query can take 0.2 s or 15 s, and it
  answers 429 when busy. On a search it costs only its own result; under
  `doctor` it may show as `blocked` or `timeout` on a bad minute.
- **Scrapers track markup.** When an engine changes its results page, its
  parser needs updating; the soft-block detection turns that into a reported
  failure rather than silence.

## Requirements

Python 3.10+. `httpx` 0.27+.

## License

MIT
