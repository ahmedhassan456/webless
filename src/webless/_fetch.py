"""The public fetch API.

One call, `fetch`, retrieves a URL and returns its readable content rather
than its markup. A page's HTML is mostly navigation, scripts, and layout, so
the default trims to the article body and renders headings, lists, code
blocks, and links as Markdown -- small enough to read or feed to a model, and
structured enough to still quote from and follow links out of.

Stack Exchange question pages refuse automated requests, so a question URL
on any Stack Exchange site is read through the public API instead and
rendered as the question followed by its best answers.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from ._html import (
    html_to_markdown,
    main_content,
    page_description,
    page_title,
    parse_html,
)
from ._http import Response, WebError, fetch_url, host_of, normalize_url

FORMATS = ("markdown", "text", "raw")


@dataclass(slots=True)
class Page:
    """A fetched page, rendered into the requested shape."""

    url: str
    final_url: str
    status: int
    content_type: str
    title: str
    description: str
    content: str
    truncated: bool = False

    @property
    def host(self) -> str:
        """The hostname the page was finally served from."""
        return host_of(self.final_url)

    @property
    def redirected(self) -> bool:
        """Whether the final URL differs from the one that was requested."""
        return self.final_url != self.url

    def __str__(self) -> str:
        return self.content

    def __len__(self) -> int:
        return len(self.content)


async def fetch(
    url: str,
    *,
    format: str = "markdown",
    full_page: bool = False,
    max_chars: int | None = None,
    offset: int = 0,
    timeout: float | None = None,
) -> Page:
    """Fetch a URL and return its content.

    `format` is ``markdown`` for the main content as Markdown, ``text`` for
    plain text with no markup, or ``raw`` for the untouched response body.
    JSON responses are returned pretty-printed whatever the format, except
    ``raw``.

    `full_page` keeps navigation, sidebars, and footers instead of trimming to
    the main content -- use it when the part you want is the chrome, such as a
    documentation index or a link list.

    `max_chars` and `offset` read a long page in sections; `Page.truncated`
    says whether anything was left behind. Raises `WebError` on a transport
    failure, a non-2xx status, or a URL that resolves to a private address.
    """
    if format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}; got {format!r}")
    if offset < 0:
        raise ValueError("offset cannot be negative")
    host = host_of(url)
    if not host or any(ch.isspace() for ch in host):
        raise ValueError(f"{url!r} is not a usable URL")

    target = normalize_url(url)
    kwargs = {} if timeout is None else {"timeout": timeout}
    response = None
    question = stack_exchange_question(target) if format != "raw" else None
    if question is not None:
        try:
            response = await fetch_stack_exchange(target, *question, **kwargs)
        except WebError:
            response = None  # the API is down or out of quota; try the page
    if response is None:
        response = await fetch_url(target, **kwargs)
    else:
        # The page is built from just the question and answers; there is no
        # chrome to trim, and trimming could drop answers as "sidebar".
        full_page = True

    body, title, description = render(response, format=format, full_page=full_page)

    section = body[offset:] if max_chars is None else body[offset : offset + max_chars]
    truncated = offset + len(section) < len(body)

    return Page(
        url=target,
        final_url=response.final_url,
        status=response.status,
        content_type=response.content_type,
        title=title,
        description=description,
        content=section,
        truncated=truncated,
    )


def render(response, *, format: str = "markdown", full_page: bool = False) -> tuple[str, str, str]:
    """Turn a response body into content, title, and description.

    Split out from `fetch` so a caller holding a response from somewhere else
    -- a cache, a test fixture, its own client -- can reuse the same
    conversion without a network round trip.
    """
    if format == "raw":
        return response.text, "", ""

    if response.is_json:
        try:
            return json.dumps(json.loads(response.text), indent=2), "", ""
        except json.JSONDecodeError:
            return response.text, "", ""

    if not response.is_html:
        return response.text, "", ""

    root = parse_html(response.text)
    title = page_title(root)
    description = page_description(root)

    if format == "text":
        node = root if full_page else main_content(root)
        return node.text(), title, description

    markdown = html_to_markdown(
        response.text, base_url=response.final_url, article=not full_page
    )
    if description and description not in markdown[:1000]:
        markdown = f"> {description}\n\n{markdown}"
    return markdown, title, description


# Stack Exchange sites on their own domains, by API site name. Every other
# site is a subdomain of stackexchange.com and uses that subdomain as its name.
STACK_EXCHANGE_HOSTS = {
    "stackoverflow.com": "stackoverflow",
    "superuser.com": "superuser",
    "serverfault.com": "serverfault",
    "askubuntu.com": "askubuntu",
    "mathoverflow.net": "mathoverflow",
    "stackapps.com": "stackapps",
}

# A filter that adds question and answer bodies, answer scores, and the
# accepted flag to the default fields. Filters are permanent once created.
STACK_EXCHANGE_FILTER = "!20aKG._8Oscv*6djs8Pgm"

MAX_ANSWERS = 5

_QUESTION_PATH = re.compile(r"^/(?:questions|q)/(\d+)(?:/|$)")


def stack_exchange_question(url: str) -> tuple[str, str] | None:
    """The API site name and question id for a Stack Exchange question URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    site = STACK_EXCHANGE_HOSTS.get(host)
    if site is None and host.endswith(".stackexchange.com"):
        site = host.removesuffix(".stackexchange.com")
        if site in ("", "api", "meta", "chat") or "." in site:
            return None
    match = _QUESTION_PATH.match(parts.path)
    if site is None or match is None:
        return None
    return site, match.group(1)


async def fetch_stack_exchange(
    url: str, site: str, question_id: str, **kwargs
) -> Response:
    """A question and its top answers, as an HTML page built from the API.

    The accepted answer comes first, then the rest by score. Raises
    `WebError` when the API refuses or knows no such question, so the caller
    can fall back to the page itself.
    """
    api = (
        f"https://api.stackexchange.com/2.3/questions/{question_id}"
        f"?site={quote_plus(site)}&filter={quote_plus(STACK_EXCHANGE_FILTER)}"
    )
    response = await fetch_url(api, headers={"Accept": "application/json"}, **kwargs)
    try:
        items = json.loads(response.text).get("items") or []
    except (json.JSONDecodeError, AttributeError):
        items = []
    if not items or not isinstance(items[0], dict):
        raise WebError(f"the Stack Exchange API has no question {question_id} on {site}")
    question = items[0]
    title = str(question.get("title", ""))  # already HTML-escaped by the API
    answers = sorted(
        (a for a in question.get("answers") or [] if isinstance(a, dict)),
        key=lambda a: (not a.get("is_accepted"), -int(a.get("score") or 0)),
    )[:MAX_ANSWERS]

    parts = [
        f"<html><head><title>{title}</title></head><body><article>",
        f"<h1>{title}</h1>",
        f"<p>Question, score {int(question.get('score') or 0)}"
        f" &middot; {len(question.get('answers') or [])} answers</p>",
        str(question.get("body", "")),
    ]
    for answer in answers:
        label = "Accepted answer" if answer.get("is_accepted") else "Answer"
        parts.append(f"<h2>{label} (score {int(answer.get('score') or 0)})</h2>")
        parts.append(str(answer.get("body", "")))
    parts.append("</article></body></html>")
    link = str(question.get("link") or url)
    return Response(
        url=url,
        final_url=html.unescape(link),
        status=200,
        text="\n".join(parts),
        content_type="text/html; charset=utf-8",
    )


__all__ = [
    "fetch",
    "render",
    "Page",
    "WebError",
    "stack_exchange_question",
    "fetch_stack_exchange",
]
