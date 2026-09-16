"""Detect X/Twitter status links and pull their text via the FixTweet mirrors.

X itself blocks unauthenticated reads, so we go through the public FixTweet-family
JSON API (api.fxtwitter.com and friends). No API key, no scraping. If every mirror
fails we return a stub so the caller can still hand the bare URL to Claude.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

# Matches x.com / twitter.com / vxtwitter / fxtwitter / fixupx status links,
# with or without the @handle segment.
STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?"
    r"(?:twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|fixupx\.com|fixvx\.com|nitter\.[\w.]+)"
    r"/(?:i/web/status|(?P<handle>[A-Za-z0-9_]{1,20})/status(?:es)?)/(?P<id>\d{5,25})",
    re.IGNORECASE,
)

# Tried in order. {id} is the status id.
_MIRRORS = (
    "https://api.fxtwitter.com/status/{id}",
    "https://api.vxtwitter.com/Twitter/status/{id}",
    "https://api.fxtwitter.com/i/status/{id}",
)

_UA = "Mozilla/5.0 (compatible; BotDaVerdade/1.0; +https://github.com/)"


@dataclass
class MediaItem:
    """One photo, video or gif attached to a post."""

    kind: str  # photo | video | gif
    url: str
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    alt: str = ""


@dataclass
class Tweet:
    """Everything we managed to learn about a linked post."""

    url: str
    status_id: str
    author: str = ""
    handle: str = ""
    text: str = ""
    created_at: str = ""
    lang: str = ""
    quoted: str = ""
    media_alt: list[str] = field(default_factory=list)
    photos: list[MediaItem] = field(default_factory=list)
    videos: list[MediaItem] = field(default_factory=list)
    ok: bool = False
    error: str = ""

    @property
    def display_author(self) -> str:
        if self.author and self.handle:
            return f"{self.author} (@{self.handle})"
        return self.author or (f"@{self.handle}" if self.handle else "unknown author")

    def as_prompt_block(self) -> str:
        """Render the tweet as plain text for the model."""
        if not self.ok:
            return (
                f"A post on X that could not be retrieved automatically: {self.url}\n"
                f"(retrieval error: {self.error or 'unknown'})\n"
                "Use web search to find what this post says and whether its claims hold up."
            )
        parts = [f"Post on X by {self.display_author}"]
        if self.created_at:
            parts.append(f"Posted: {self.created_at}")
        parts.append(f"URL: {self.url}")
        parts.append(f"\nText:\n{self.text or '(no text)'}")
        if self.quoted:
            parts.append(f"\nIt quotes another post that says:\n{self.quoted}")
        if self.media_alt:
            parts.append("\nAttached media described as: " + " | ".join(self.media_alt))
        if self.photos or self.videos:
            counts = []
            if self.photos:
                counts.append(f"{len(self.photos)} image(s)")
            if self.videos:
                total = sum(v.duration for v in self.videos)
                counts.append(
                    f"{len(self.videos)} video(s)"
                    + (f" totalling about {int(total)}s" if total else "")
                )
            parts.append(
                "\nThis post carries " + " and ".join(counts)
                + ". They are attached to this message as images - read them, and treat what "
                "they show as part of the claim."
            )
        return "\n".join(parts)


def find_status_links(text: str) -> list[str]:
    """Return de-duplicated X status URLs found in a block of text, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in STATUS_RE.finditer(text or ""):
        status_id = match.group("id")
        if status_id in seen:
            continue
        seen.add(status_id)
        out.append(match.group(0))
    return out


def status_id_of(url: str) -> str:
    match = STATUS_RE.search(url or "")
    return match.group("id") if match else ""


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _seconds(value: object) -> float:
    """Durations are fractional - do not round them to int."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_fxtwitter(payload: dict) -> dict:
    tweet = payload.get("tweet") or {}
    if not isinstance(tweet, dict):
        return {}
    author = tweet.get("author") or {}
    quote = tweet.get("quote") or {}
    quoted = ""
    if isinstance(quote, dict) and _clean(quote.get("text")):
        q_author = (quote.get("author") or {}).get("screen_name") if isinstance(quote.get("author"), dict) else ""
        quoted = f"@{q_author}: {_clean(quote.get('text'))}" if q_author else _clean(quote.get("text"))

    alts: list[str] = []
    photos: list[MediaItem] = []
    videos: list[MediaItem] = []
    media = tweet.get("media") or {}
    if isinstance(media, dict):
        for photo in media.get("photos") or []:
            if not isinstance(photo, dict) or not _clean(photo.get("url")):
                continue
            alt = _clean(photo.get("altText"))
            if alt:
                alts.append(alt)
            photos.append(MediaItem(
                kind="photo",
                url=_clean(photo["url"]),
                width=_number(photo.get("width")),
                height=_number(photo.get("height")),
                alt=alt,
            ))
        for video in media.get("videos") or []:
            if not isinstance(video, dict) or not _clean(video.get("url")):
                continue
            videos.append(MediaItem(
                kind=_clean(video.get("type")) or "video",
                url=_clean(video["url"]),
                thumbnail_url=_clean(video.get("thumbnail_url")),
                width=_number(video.get("width")),
                height=_number(video.get("height")),
                duration=_seconds(video.get("duration")),
            ))

    return {
        "photos": photos,
        "videos": videos,
        "text": _clean(tweet.get("text")),
        "author": _clean(author.get("name")) if isinstance(author, dict) else "",
        "handle": _clean(author.get("screen_name")) if isinstance(author, dict) else "",
        "created_at": _clean(tweet.get("created_at")),
        "lang": _clean(tweet.get("lang")),
        "quoted": quoted,
        "media_alt": alts,
    }


def _parse_vxtwitter(payload: dict) -> dict:
    """vxtwitter uses a flatter shape."""
    alts: list[str] = []
    photos: list[MediaItem] = []
    videos: list[MediaItem] = []
    for entry in payload.get("media_extended") or []:
        if not isinstance(entry, dict) or not _clean(entry.get("url")):
            continue
        alt = _clean(entry.get("altText"))
        if alt:
            alts.append(alt)
        size = entry.get("size") if isinstance(entry.get("size"), dict) else {}
        kind = _clean(entry.get("type")).lower()
        item = MediaItem(
            kind="photo" if kind in ("image", "photo") else (kind or "video"),
            url=_clean(entry["url"]),
            thumbnail_url=_clean(entry.get("thumbnail_url")),
            width=_number(size.get("width")),
            height=_number(size.get("height")),
            duration=_seconds(entry.get("duration_millis")) / 1000.0,
            alt=alt,
        )
        (photos if item.kind == "photo" else videos).append(item)
    quoted = ""
    qt = payload.get("qrt") or payload.get("quote")
    if isinstance(qt, dict):
        quoted = _clean(qt.get("text"))
    return {
        "photos": photos,
        "videos": videos,
        "text": _clean(payload.get("text")),
        "author": _clean(payload.get("user_name")),
        "handle": _clean(payload.get("user_screen_name")),
        "created_at": _clean(payload.get("date")),
        "lang": _clean(payload.get("lang")),
        "quoted": quoted,
        "media_alt": alts,
    }


async def fetch_tweet(session: aiohttp.ClientSession, url: str, *, timeout: int = 15) -> Tweet:
    """Best-effort retrieval of a tweet's text. Never raises."""
    status_id = status_id_of(url)
    match = STATUS_RE.search(url or "")
    tweet = Tweet(url=url, status_id=status_id, handle=(match.group("handle") or "") if match else "")
    if not status_id:
        tweet.error = "not an X status URL"
        return tweet

    last_error = "no mirror responded"
    for template in _MIRRORS:
        endpoint = template.format(id=status_id)
        try:
            async with session.get(
                endpoint,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    last_error = f"{endpoint.split('/')[2]} returned HTTP {response.status}"
                    continue
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError:
            last_error = f"{endpoint.split('/')[2]} timed out"
            continue
        except Exception as exc:  # noqa: BLE001 - mirrors fail in many creative ways
            last_error = f"{endpoint.split('/')[2]}: {type(exc).__name__}"
            continue

        if not isinstance(payload, dict):
            last_error = "unexpected response shape"
            continue

        parsed = _parse_fxtwitter(payload) if "tweet" in payload else _parse_vxtwitter(payload)
        if not parsed or not (parsed.get("text") or parsed.get("media_alt")):
            last_error = "mirror returned an empty post"
            continue

        tweet.text = parsed["text"]
        tweet.author = parsed["author"]
        tweet.handle = parsed["handle"] or tweet.handle
        tweet.created_at = parsed["created_at"]
        tweet.lang = parsed["lang"]
        tweet.quoted = parsed["quoted"]
        tweet.media_alt = parsed["media_alt"]
        tweet.photos = parsed.get("photos") or []
        tweet.videos = parsed.get("videos") or []
        tweet.ok = True
        log.debug("fetched %s via %s", status_id, endpoint)
        return tweet

    tweet.error = last_error
    log.warning("could not fetch tweet %s: %s", status_id, last_error)
    return tweet
