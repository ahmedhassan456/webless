"""The `webless` command.

A thin shell over `search` and `fetch`, so the package is usable from a
terminal or a pipe without writing a script. Output is human readable by
default and JSON with ``--json``, which makes it composable with `jq`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from . import __version__
from ._doctor import default_engines, doctor
from ._engines import ENGINES_BY_NAME, TOPICS, searxng
from ._http import WebError
from ._fetch import fetch
from ._search import DEFAULT_LIMIT, search


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser for both subcommands."""
    parser = argparse.ArgumentParser(
        prog="webless",
        description="Search the web and fetch pages, with no API keys.",
    )
    parser.add_argument("--version", action="version", version=f"webless {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("search", help="search the web")
    find.add_argument("query", nargs="+", help="what to search for")
    find.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT)
    find.add_argument("-w", "--wide", action="store_true", help="add the long-tail engines")
    find.add_argument(
        "-t", "--topic", choices=TOPICS, default=None,
        help="add specialist engines: code (Stack Overflow), science (OpenAlex, "
        "arXiv, PubMed), or news (Bing News)",
    )
    find.add_argument(
        "--searxng", metavar="URL", default=None,
        help="also query this SearXNG server (default: $WEBLESS_SEARXNG_URL)",
    )
    find.add_argument("--allow", action="append", metavar="DOMAIN", default=None)
    find.add_argument("--block", action="append", metavar="DOMAIN", default=None)
    find.add_argument("--timeout", type=float, default=12.0)
    find.add_argument("--json", action="store_true")

    get = sub.add_parser("fetch", help="fetch a page as Markdown")
    get.add_argument("url")
    get.add_argument("-f", "--format", choices=("markdown", "text", "raw"), default="markdown")
    get.add_argument("--full-page", action="store_true", help="keep navigation and footers")
    get.add_argument("-c", "--max-chars", type=int, default=None)
    get.add_argument("--offset", type=int, default=0)
    get.add_argument("--timeout", type=float, default=None)
    get.add_argument("--json", action="store_true")

    check = sub.add_parser("doctor", help="check which search engines are working")
    check.add_argument(
        "-q", "--query", default=None,
        help="ask every engine this instead of its own probe query",
    )
    check.add_argument(
        "-e", "--engine", action="append", choices=sorted(ENGINES_BY_NAME),
        metavar="NAME", default=None, help="check only this engine (repeatable)",
    )
    check.add_argument(
        "--searxng", metavar="URL", default=None,
        help="also check this SearXNG server (default: $WEBLESS_SEARXNG_URL)",
    )
    check.add_argument("--timeout", type=float, default=12.0)
    check.add_argument("--json", action="store_true")
    return parser


async def run_search(args: argparse.Namespace) -> int:
    """Print a fused ranking, and report any engine that did not answer."""
    result = await search(
        " ".join(args.query),
        limit=args.limit,
        wide=args.wide,
        topic=args.topic,
        searxng_url=args.searxng,
        timeout=args.timeout,
        allowed_domains=args.allow,
        blocked_domains=args.block,
    )

    if args.json:
        print(json.dumps(asdict(result), indent=2))
        return 0 if result.ok else 1

    for index, hit in enumerate(result.hits, start=1):
        print(f"{index}. {hit.title}")
        print(f"   {hit.url}")
        if hit.snippet:
            print(f"   {hit.snippet}")
        print(f"   [{', '.join(hit.engines)}]")
        print()

    if not result.hits:
        print("no results", file=sys.stderr)
    for name, reason in result.failed.items():
        print(f"{name}: {reason}", file=sys.stderr)
    return 0 if result.ok else 1


async def run_fetch(args: argparse.Namespace) -> int:
    """Print one page's content, or its full record with ``--json``."""
    page = await fetch(
        args.url,
        format=args.format,
        full_page=args.full_page,
        max_chars=args.max_chars,
        offset=args.offset,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(asdict(page), indent=2))
        return 0

    if page.redirected:
        print(f"(redirected to {page.final_url})", file=sys.stderr)
    print(page.content)
    if page.truncated:
        print(
            f"[truncated; continue with --offset {args.offset + len(page.content)}]",
            file=sys.stderr,
        )
    return 0


async def run_doctor(args: argparse.Namespace) -> int:
    """Print each engine's status. Exits 1 unless every engine is working."""
    if args.engine:
        engines = tuple(ENGINES_BY_NAME[name] for name in dict.fromkeys(args.engine))
        if args.searxng:
            engines += (searxng(args.searxng),)
    else:
        engines = default_engines(args.searxng)
    checks = await doctor(args.query, engines=engines, timeout=args.timeout)

    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(
            f"{'engine':<14} {'status':<8} {'hits':>4} {'on-topic':>8} {'time':>6}  query"
        )
        for c in checks:
            print(
                f"{c.engine:<14} {c.status:<8} {c.hits:>4} "
                f"{c.on_topic:>8.0%} {c.seconds:>5.1f}s  {c.query}"
            )
            if c.detail:
                print(f"{'':<14} {c.detail}")
        working = sum(c.ok for c in checks)
        print(f"\n{working} of {len(checks)} engines working")
    return 0 if all(c.ok for c in checks) else 1


RUNNERS = {"search": run_search, "fetch": run_fetch, "doctor": run_doctor}


def main(argv: list[str] | None = None) -> int:
    """Entry point. Network failures print a message rather than a traceback."""
    args = build_parser().parse_args(argv)
    runner = RUNNERS[args.command]
    try:
        return asyncio.run(runner(args))
    except (WebError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
