#!/usr/bin/env python3
"""Sanity checks that need no keys, plus a live mirror test.

    python test_fetch.py                       # offline checks only
    python test_fetch.py <x.com status url>    # also hit the mirrors
"""
from __future__ import annotations

import asyncio
import sys

import aiohttp

from factchecker.claude import _extract_json, _language_instruction, _looks_like_json, _salvage
from factchecker.render import VERDICT_META
from factchecker.twitter import fetch_tweet, find_status_links, status_id_of

FIXTURES = [
    ("https://x.com/elonmusk/status/1899258403462058164", "1899258403462058164"),
    ("https://twitter.com/BBCBreaking/status/1234567890123456789", "1234567890123456789"),
    ("https://www.x.com/some_user/status/1111111111111111111?s=20&t=abc", "1111111111111111111"),
    ("https://mobile.twitter.com/a/statuses/2222222222222222222", "2222222222222222222"),
    ("https://fxtwitter.com/x/status/3333333333333333333", "3333333333333333333"),
    ("https://x.com/i/web/status/4444444444444444444", "4444444444444444444"),
]
NEGATIVES = [
    "https://x.com/elonmusk",
    "https://example.com/status/1899258403462058164",
    "just a normal message with no links",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
]


def check_url_parsing() -> int:
    failures = 0
    for url, expected in FIXTURES:
        found = find_status_links(f"olha isto {url} que loucura")
        if not found or status_id_of(found[0]) != expected:
            print(f"  FAIL  {url} -> {found}")
            failures += 1
    for text in NEGATIVES:
        if find_status_links(text):
            print(f"  FAIL  should not have matched: {text}")
            failures += 1

    both = find_status_links(f"{FIXTURES[0][0]} and {FIXTURES[1][0]} and {FIXTURES[0][0]}")
    if len(both) != 2:
        print(f"  FAIL  dedupe: expected 2 unique links, got {len(both)}")
        failures += 1

    print(f"url parsing: {len(FIXTURES) + len(NEGATIVES) + 1 - failures}/{len(FIXTURES) + len(NEGATIVES) + 1} passed")
    return failures


def check_json_extraction() -> int:
    failures = 0
    cases = [
        ('```json\n{"verdict": "FALSE", "claim": "x"}\n```', "FALSE"),
        ('Here you go:\n```\n{"verdict": "TRUE"}\n```\nhope that helps', "TRUE"),
        ('{"verdict": "MISLEADING", "sources": [{"url": "https://a.com"}]}', "MISLEADING"),
        ('thinking out loud {"not": "it"} then ```json\n{"verdict": "SATIRE"}\n```', "SATIRE"),
    ]
    for text, expected in cases:
        parsed = _extract_json(text)
        if not parsed or parsed.get("verdict") != expected:
            print(f"  FAIL  {text[:50]!r} -> {parsed}")
            failures += 1
    if _extract_json("no json here at all") is not None:
        print("  FAIL  found JSON where there is none")
        failures += 1
    print(f"json extraction: {len(cases) + 1 - failures}/{len(cases) + 1} passed")
    return failures


def check_malformed_json() -> int:
    """The model quoting someone inside a JSON string used to leak raw JSON into Discord."""
    failures = 0

    broken = ('{"claim": "Trump afirmou que já "ganhou centenas de milhões" em ações", '
              '"verdict": "MISLEADING", "summary": "A citação é real.", '
              '"sources": [{"title": "Reuters", "url": "https://reuters.com/a"}], "language": "pt"}')
    salvaged = _salvage(broken)
    if not salvaged or salvaged.get("verdict") != "MISLEADING":
        print(f"  FAIL  could not salvage unescaped quotes -> {salvaged}")
        failures += 1
    elif "ganhou centenas" not in salvaged.get("claim", ""):
        print(f"  FAIL  salvaged claim is wrong -> {salvaged.get('claim')!r}")
        failures += 1

    # A cut-off reply must NOT be salvaged into a half-sentence verdict.
    if _salvage('{"verdict": "FALSE", "summary": "he said "yes" and then') is not None:
        print("  FAIL  salvaged a truncated reply")
        failures += 1

    for payload in ('```json\n{"verdict": "X"}', '{"summary": "a"}', '  {"claim": 1}'):
        if not _looks_like_json(payload):
            print(f"  FAIL  {payload[:30]!r} should be recognised as JSON")
            failures += 1
    if _looks_like_json("The claim checks out against the ONS figures."):
        print("  FAIL  ordinary prose flagged as JSON")
        failures += 1

    print(f"malformed json: {6 - failures}/6 passed")
    return failures


def check_language() -> int:
    failures = 0
    cases = [
        (("auto", "en"), "English"),
        (("auto", "pt"), "European Portuguese"),
        (("auto", "pt-BR"), "Portuguese"),
        (("auto", "de"), "English"),
        (("auto", "fr"), "English"),
        (("pt", "en"), "European Portuguese"),   # explicit setting beats the hint
        (("en", "pt"), "English"),
    ]
    for (setting, hint), expected in cases:
        text = _language_instruction(setting, hint)
        if expected not in text:
            print(f"  FAIL  {setting}/{hint} -> {text[:70]!r}")
            failures += 1
    if "not from this bot's name" not in _language_instruction("auto", ""):
        print("  FAIL  the no-hint instruction lost its anti-bias wording")
        failures += 1
    print(f"language: {len(cases) + 1 - failures}/{len(cases) + 1} passed")
    return failures


def check_verdict_coverage() -> int:
    from factchecker.claude import VERDICTS

    missing = [v for v in VERDICTS if v not in VERDICT_META]
    if missing:
        print(f"  FAIL  verdicts with no embed styling: {missing}")
        return 1
    print(f"verdict styling: all {len(VERDICTS)} verdicts covered")
    return 0


async def check_live(url: str) -> int:
    print(f"\nlive mirror test: {url}")
    async with aiohttp.ClientSession() as session:
        tweet = await fetch_tweet(session, url, timeout=20)
    if not tweet.ok:
        print(f"  FAIL  could not retrieve: {tweet.error}")
        print("  (the bot still works - it falls back to letting Claude search for the post)")
        return 1
    print(f"  author: {tweet.display_author}")
    print(f"  posted: {tweet.created_at or 'unknown'}   lang: {tweet.lang or '?'}")
    print(f"  text:   {tweet.text[:200]}")
    if tweet.quoted:
        print(f"  quotes: {tweet.quoted[:120]}")
    return 0


def main() -> int:
    failures = (
        check_url_parsing()
        + check_json_extraction()
        + check_malformed_json()
        + check_language()
        + check_verdict_coverage()
    )
    if len(sys.argv) > 1:
        failures += asyncio.run(check_live(sys.argv[1]))
    print("\n" + ("all good" if failures == 0 else f"{failures} failure(s)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
