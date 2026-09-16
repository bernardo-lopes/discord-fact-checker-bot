"""Turn the pictures and videos attached to a post into images Claude can look at.

Claude's API accepts images but not video or audio, so a video becomes a handful of
evenly spaced keyframes pulled with ffmpeg, plus - when a transcriber is configured - a
transcript of whatever is said in it. If ffmpeg is not installed we fall back to the
video's thumbnail, which X provides for free - degraded, but never broken.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import aiohttp

from .transcribe import Transcriber
from .twitter import MediaItem, Tweet

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
# Claude accepts up to 10MB per image; stay well under it.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
FFMPEG_TIMEOUT = 45


@dataclass
class ImageBlock:
    """One picture, ready to be dropped into a Claude message."""

    media_type: str
    data_b64: str
    label: str

    def as_content(self) -> list[dict]:
        return [
            {"type": "text", "text": self.label},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": self.media_type, "data": self.data_b64},
            },
        ]


@dataclass
class MediaBundle:
    """What we managed to pull out of a post's attachments."""

    images: list[ImageBlock] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)

    def extend(self, other: "MediaBundle") -> None:
        self.images.extend(other.images)
        self.transcripts.extend(other.transcripts)


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


@lru_cache(maxsize=1)
def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _sized_photo_url(url: str) -> str:
    """X serves several sizes; 'medium' (~1200px) is plenty and much cheaper."""
    if "pbs.twimg.com" in url and "name=" not in url:
        return f"{url}{'&' if '?' in url else '?'}name=medium"
    return url


async def _fetch_bytes(
    session: aiohttp.ClientSession, url: str, *, limit: int, timeout: int
) -> tuple[bytes, str] | None:
    """Download up to `limit` bytes. Returns (payload, content_type) or None."""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
        ) as response:
            if response.status != 200:
                log.debug("media %s -> HTTP %s", url[:80], response.status)
                return None
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    log.debug("media %s exceeded %d bytes - skipping", url[:80], limit)
                    return None
            return b"".join(chunks), content_type
    except asyncio.TimeoutError:
        log.debug("media %s timed out", url[:80])
    except Exception as exc:  # noqa: BLE001
        log.debug("media %s failed: %s", url[:80], type(exc).__name__)
    return None


async def _probe_duration(path: Path) -> float:
    probe = ffprobe_path()
    if not probe:
        return 0.0
    try:
        process = await asyncio.create_subprocess_exec(
            probe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        return float(out.decode().strip())
    except Exception:  # noqa: BLE001
        return 0.0


async def _grab_frame(source: Path, destination: Path, at_seconds: float) -> bool:
    binary = ffmpeg_path()
    if not binary:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "-v", "error", "-y",
            "-ss", f"{max(at_seconds, 0):.2f}", "-i", str(source),
            "-frames:v", "1",
            # Cap the long edge at 1280px: past that Claude gains nothing and you pay for it.
            "-vf", "scale='min(1280,iw)':-2",
            "-q:v", "3", "-f", "image2", str(destination),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT)
        if process.returncode != 0:
            log.debug("ffmpeg frame at %.1fs failed: %s", at_seconds, err.decode()[:200])
        return destination.exists() and destination.stat().st_size > 0
    except asyncio.TimeoutError:
        log.warning("ffmpeg timed out extracting a frame")
    except Exception as exc:  # noqa: BLE001
        log.warning("ffmpeg failed: %s", exc)
    return False


def _timestamps(duration: float, count: int) -> list[float]:
    """Evenly spaced sample points, avoiding the very first and last moments."""
    if duration <= 0:
        return [0.5, 2.0, 5.0][:count]
    return [duration * (index + 0.5) / count for index in range(count)]


async def process_video(
    session: aiohttp.ClientSession,
    item: MediaItem,
    *,
    count: int,
    max_bytes: int,
    max_seconds: int,
    timeout: int,
    origin: str,
    transcriber: Transcriber | None = None,
) -> MediaBundle:
    """Download a video once, then pull keyframes and (optionally) a transcript from it."""
    if item.duration and item.duration > max_seconds:
        log.info("video is %.0fs, over the %ds limit - using its thumbnail", item.duration, max_seconds)
        return MediaBundle(images=await _thumbnail_only(session, item, timeout=timeout, origin=origin))

    if not ffmpeg_path():
        log.info("ffmpeg not installed - falling back to the video thumbnail")
        return MediaBundle(images=await _thumbnail_only(session, item, timeout=timeout, origin=origin))

    fetched = await _fetch_bytes(session, item.url, limit=max_bytes, timeout=timeout)
    if not fetched:
        return MediaBundle(images=await _thumbnail_only(session, item, timeout=timeout, origin=origin))

    payload, _ = fetched
    blocks: list[ImageBlock] = []
    transcript = ""

    with tempfile.TemporaryDirectory(prefix="verdade-") as workdir:
        root = Path(workdir)
        source = root / "clip.mp4"
        source.write_bytes(payload)

        duration = item.duration or await _probe_duration(source)
        for index, moment in enumerate(_timestamps(duration, count), start=1):
            destination = root / f"frame{index}.jpg"
            if not await _grab_frame(source, destination, moment):
                continue
            data = destination.read_bytes()
            if len(data) > MAX_IMAGE_BYTES:
                continue
            stamp = f" at {int(moment) // 60}:{int(moment) % 60:02d}" if duration else ""
            blocks.append(ImageBlock(
                media_type="image/jpeg",
                data_b64=base64.b64encode(data).decode("ascii"),
                label=f"Frame {index} of {count} from the video in {origin}{stamp}:",
            ))

        # Do this last: the temp dir must still exist, and a slow transcription
        # should not delay the frames if it throws.
        if transcriber is not None and transcriber.enabled:
            transcript = await transcriber.transcribe(source, session)

    if not blocks:
        blocks = await _thumbnail_only(session, item, timeout=timeout, origin=origin)
    else:
        log.info("extracted %d frame(s) from a %.0fs video", len(blocks), item.duration or 0)

    transcripts = []
    if transcript:
        transcripts.append(
            f"Transcript of the speech in the video in {origin} "
            f"(machine-transcribed, so proper nouns and numbers may be imperfect):\n{transcript}"
        )
    return MediaBundle(images=blocks, transcripts=transcripts)


async def _thumbnail_only(
    session: aiohttp.ClientSession, item: MediaItem, *, timeout: int, origin: str
) -> list[ImageBlock]:
    if not item.thumbnail_url:
        return []
    block = await _image_block(
        session, item.thumbnail_url,
        label=f"Thumbnail of the video in {origin} (the video itself could not be sampled):",
        timeout=timeout,
    )
    return [block] if block else []


async def _image_block(
    session: aiohttp.ClientSession, url: str, *, label: str, timeout: int
) -> ImageBlock | None:
    fetched = await _fetch_bytes(session, _sized_photo_url(url), limit=MAX_IMAGE_BYTES, timeout=timeout)
    if not fetched:
        return None
    payload, content_type = fetched
    if content_type not in SUPPORTED_IMAGE_TYPES:
        # Trust the magic bytes when the server is vague about the type.
        if payload.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif payload.startswith(b"\x89PNG"):
            content_type = "image/png"
        elif payload[:6] in (b"GIF87a", b"GIF89a"):
            content_type = "image/gif"
        elif payload[8:12] == b"WEBP":
            content_type = "image/webp"
        else:
            log.debug("unsupported image type %r at %s", content_type, url[:80])
            return None
    return ImageBlock(
        media_type=content_type,
        data_b64=base64.b64encode(payload).decode("ascii"),
        label=label,
    )


async def collect_from_tweet(
    session: aiohttp.ClientSession,
    tweet: Tweet,
    *,
    budget: int,
    video_frames: int,
    max_video_bytes: int,
    max_video_seconds: int,
    timeout: int,
    transcriber: Transcriber | None = None,
) -> MediaBundle:
    """Photos first (cheap and usually the point), then video frames if room remains."""
    origin = f"the post by {tweet.display_author}"
    bundle = MediaBundle()

    for index, photo in enumerate(tweet.photos, start=1):
        if len(bundle.images) >= budget:
            break
        alt = f' (its alt text: "{photo.alt}")' if photo.alt else ""
        block = await _image_block(
            session, photo.url,
            label=f"Image {index} attached to {origin}{alt}:",
            timeout=timeout,
        )
        if block:
            bundle.images.append(block)

    transcribing = transcriber is not None and transcriber.enabled
    for video in tweet.videos:
        remaining = budget - len(bundle.images)
        if remaining <= 0 and not transcribing:
            break  # no room for frames and nothing to listen to
        bundle.extend(await process_video(
            session, video,
            count=max(1, min(video_frames, remaining)),
            max_bytes=max_video_bytes,
            max_seconds=max_video_seconds,
            timeout=timeout,
            origin=origin,
            transcriber=transcriber,
        ))

    bundle.images = bundle.images[:budget]
    return bundle


async def collect_from_urls(
    session: aiohttp.ClientSession, urls: list[tuple[str, str]], *, budget: int, timeout: int
) -> list[ImageBlock]:
    """For plain image URLs, e.g. files attached to a Discord message. [(url, label)]"""
    blocks: list[ImageBlock] = []
    for url, label in urls:
        if len(blocks) >= budget:
            break
        block = await _image_block(session, url, label=label, timeout=timeout)
        if block:
            blocks.append(block)
    return blocks
