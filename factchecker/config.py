"""Environment-backed configuration for Bot da Verdade."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing."""


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _ids(name: str) -> frozenset[int]:
    out: set[int] = set()
    for chunk in _str(name).replace(";", ",").split(","):
        chunk = chunk.strip().strip("<@!>")
        if chunk.isdigit():
            out.add(int(chunk))
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    discord_token: str
    anthropic_api_key: str
    model: str
    max_web_searches: int
    reply_language: str
    bot_name: str
    watchlist_path: Path
    seed_watch_user_ids: frozenset[int]
    dev_guild_id: int | None
    sync_global: bool
    cooldown_seconds: int
    max_charges: int
    charge_refill_hours: float
    http_timeout: int
    analyse_media: bool
    max_images: int
    video_frames: int
    max_video_bytes: int
    max_video_seconds: int
    transcribe_backend: str
    openai_api_key: str
    openai_transcribe_model: str
    whisper_model: str
    max_transcript_chars: int
    log_level: str


def load_config() -> Config:
    token = _str("DISCORD_TOKEN")
    api_key = _str("ANTHROPIC_API_KEY")

    missing = [n for n, v in (("DISCORD_TOKEN", token), ("ANTHROPIC_API_KEY", api_key)) if not v]
    if missing:
        raise ConfigError(
            "Missing required setting(s): "
            + ", ".join(missing)
            + f". Copy .env.example to .env in {BASE_DIR} and fill them in."
        )

    language = _str("REPLY_LANGUAGE", "auto").lower()
    if language not in {"auto", "pt", "en"}:
        language = "auto"

    return Config(
        discord_token=token,
        anthropic_api_key=api_key,
        model=_str("CLAUDE_MODEL", "claude-sonnet-5"),
        max_web_searches=max(1, min(_int("MAX_WEB_SEARCHES", 5), 10)),
        reply_language=language,
        bot_name=_str("BOT_NAME", "Bot da Verdade"),
        watchlist_path=BASE_DIR / "data" / "watchlist.json",
        seed_watch_user_ids=_ids("WATCH_USER_IDS"),
        dev_guild_id=(_int("DEV_GUILD_ID", 0) or None),
        sync_global=_bool("SYNC_GLOBAL", False),
        cooldown_seconds=max(0, _int("COOLDOWN_SECONDS", 30)),
        max_charges=max(1, _int("MAX_CHARGES", 3)),
        charge_refill_hours=max(0.05, _float("CHARGE_REFILL_HOURS", 8.0)),
        http_timeout=max(5, _int("HTTP_TIMEOUT", 15)),
        analyse_media=_bool("ANALYSE_MEDIA", True),
        max_images=max(0, min(_int("MAX_IMAGES", 4), 12)),
        video_frames=max(1, min(_int("VIDEO_FRAMES", 3), 8)),
        max_video_bytes=max(1, _int("MAX_VIDEO_MB", 25)) * 1024 * 1024,
        max_video_seconds=max(5, _int("MAX_VIDEO_SECONDS", 180)),
        transcribe_backend=_str("TRANSCRIBE_BACKEND", "auto").lower(),
        openai_api_key=_str("OPENAI_API_KEY"),
        openai_transcribe_model=_str("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        whisper_model=_str("WHISPER_MODEL", "base"),
        max_transcript_chars=max(200, _int("MAX_TRANSCRIPT_CHARS", 3000)),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
