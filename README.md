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

The only dependency is `httpx`. HTML parsing, content extraction, and Markdown
rendering are all written against the standard library, so there is no
`lxml`, no `beautifulsoup4`, and no compiler needed to install.

## `search`

```python
result = await search(
    "sqlite write-ahead logging",
    limit=10,
    wide=False,
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

## Not blocking

Two senses, both deliberate.

**Nothing blocks anything else.** Every call is async with a hard timeout. A
search is several concurrent requests, so a slow engine delays its own result
and nothing more.

**Requests are hard to block.** Hosts refuse on fingerprint reputation, so a
retry after a 403 or 429 presents a *different* browser user agent rather than
repeating the one just refused — which clears most transient blocks on its
own. Retries back off exponentially with jitter.

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
webless fetch https://peps.python.org/pep-3156/ > pep.md
webless fetch https://api.github.com/repos/python/cpython --json | jq .status
```

`--json` on either subcommand prints the full record, so it composes with
`jq`. `search` exits non-zero when every engine failed.

## Engines

| engine | how | notes |
|---|---|---|
| DuckDuckGo | scrape | lite view, falls back to the html view |
| Mojeek | scrape | independent index |
| Bing | RSS, then scrape | tracker URLs decoded |
| Wikipedia | OpenSearch API | |
| Marginalia | public API | `wide` only; small-web index |
| Hacker News | Algolia API | `wide` only; discussion |

Pick your own set with `engines=`:

```python
from webless import search, DuckDuckGoEngine, MojeekEngine

result = await search("query", engines=(DuckDuckGoEngine, MojeekEngine))
```

A custom engine is any subclass of `Engine` with a `name` and an
`async search(query, limit, timeout) -> list[SearchHit]`.

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
- **Scrapers track markup.** When an engine changes its results page, its
  parser needs updating; the soft-block detection turns that into a reported
  failure rather than silence.

## Requirements

Python 3.10+. `httpx` 0.27+.

## License

MIT
