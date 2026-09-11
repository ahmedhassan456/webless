"""The public fetch API.

One call, `fetch`, retrieves a URL and returns its readable content rather
than its markup. A page's HTML is mostly navigation, scripts, and layout, so
the default trims to the article body and renders headings, lists, code
blocks, and links as Markdown -- small enough to read or feed to a model, and
structured enough to still quote from and follow links out of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ._html import (
    html_to_markdown,
    main_content,
    page_description,
    page_title,
    parse_html,
)
from ._http import WebError, fetch_url, host_of, normalize_url

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
    response = await fetch_url(target, **kwargs)

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


__all__ = ["fetch", "render", "Page", "WebError"]
