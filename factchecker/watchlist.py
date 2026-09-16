"""JSON-backed, per-channel list of users whose X links get checked automatically.

Scoped to the channel the /watch was run in, not the whole server: a watch set up
in #links-suspeitos has no business firing in #general.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

FORMAT_VERSION = 2


class Watchlist:
    def __init__(self, path: Path, seed_ids: frozenset[int] = frozenset()) -> None:
        self._path = path
        self._seed = set(seed_ids)
        self._channels: dict[int, set[int]] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not self._path.exists():
            log.info("no watchlist yet at %s - starting empty", self._path)
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("could not read watchlist (%s) - starting empty", exc)
            return

        if "guilds" in raw and "channels" not in raw:
            # The old server-wide format. A guild id can't be turned into a channel
            # id, so there is nothing to migrate - say so plainly rather than
            # silently watching nobody.
            watched = sum(len(v) for v in (raw.get("guilds") or {}).values())
            log.warning(
                "watchlist is in the old server-wide format (%d entr%s). Watches are now "
                "per-channel, so these cannot be migrated - re-run /watch in the channels "
                "you want, and this message will stop.",
                watched, "y" if watched == 1 else "ies",
            )
            return

        for channel_id, user_ids in (raw.get("channels") or {}).items():
            try:
                self._channels[int(channel_id)] = {int(u) for u in user_ids}
            except (TypeError, ValueError):
                continue
        log.info("watchlist loaded: %d channel(s)", len(self._channels))

    def _write(self) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "channels": {str(c): sorted(u) for c, u in self._channels.items() if u},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, prefix=".watchlist-", suffix=".tmp", delete=False
        )
        try:
            with handle as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(handle.name, self._path)
        except OSError as exc:
            log.error("could not save watchlist: %s", exc)
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    def is_watched(self, channel_id: int | None, user_id: int, *, parent_id: int | None = None) -> bool:
        """Is this user watched here? A thread inherits its parent channel's watches."""
        if user_id in self._seed:
            return True
        if channel_id is not None and user_id in self._channels.get(channel_id, set()):
            return True
        # Posting in a thread counts as posting in the channel it hangs off.
        return parent_id is not None and user_id in self._channels.get(parent_id, set())

    def members(self, channel_id: int | None) -> list[int]:
        here = self._channels.get(channel_id, set()) if channel_id is not None else set()
        return sorted(here | self._seed)

    def is_seeded(self, user_id: int) -> bool:
        return user_id in self._seed

    async def add(self, channel_id: int, user_id: int) -> bool:
        """Returns False if the user was already watched in this channel."""
        async with self._lock:
            bucket = self._channels.setdefault(channel_id, set())
            if user_id in bucket:
                return False
            bucket.add(user_id)
            await asyncio.to_thread(self._write)
            return True

    async def remove(self, channel_id: int, user_id: int) -> bool:
        """Returns False if the user was not watched in this channel."""
        async with self._lock:
            bucket = self._channels.get(channel_id, set())
            if user_id not in bucket:
                return False
            bucket.discard(user_id)
            await asyncio.to_thread(self._write)
            return True
