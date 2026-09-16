"""Spend control: a small pool of charges that refills slowly.

The bot starts with a full pool. Every check - an automatic link, an @mention,
/factcheck, or the right-click menu - spends one charge. When the pool is not
full, one charge comes back every refill interval. Run out and the bot goes
quiet until the next one arrives.

The pool is deliberately not persisted: a restart is a full pool, as asked for.
Timings use the wall clock rather than a monotonic one, so a laptop that sleeps
for the night still wakes up with its charges refilled.
"""
from __future__ import annotations

import logging
import math
import time

log = logging.getLogger(__name__)

# Why a check was refused. bot.py turns these into something a human reads.
ALLOWED = ""
NO_CHARGES = "no_charges"
COOLING_DOWN = "cooldown"


def format_hours_pt(seconds: float) -> str:
    """'1 hora' / '7 horas'. Rounded up, so the bot never promises to be back early."""
    hours = max(1, math.ceil(max(0.0, seconds) / 3600))
    return "1 hora" if hours == 1 else f"{hours} horas"


def format_duration(seconds: float) -> str:
    """'7h 41m', '41m', '30s' - short enough for a Discord line."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


class ChargeLimiter:
    def __init__(
        self, *, capacity: int = 3, refill_seconds: int = 8 * 3600, cooldown_seconds: int = 30
    ) -> None:
        self.capacity = max(1, capacity)
        self.refill_seconds = max(60, refill_seconds)
        self._cooldown = max(0, cooldown_seconds)
        self._charges = self.capacity
        self._next_refill: float | None = None  # None while the pool is full
        self._last_by_user: dict[int, float] = {}

    # ------------------------------------------------------------------ internals

    def _refill(self) -> None:
        """Hand back any charges that have come due since we last looked."""
        if self._next_refill is None:
            return
        now = time.time()
        while self._charges < self.capacity and now >= self._next_refill:
            self._charges += 1
            log.info(
                "a charge refilled - %d/%d available", self._charges, self.capacity
            )
            self._next_refill = (
                self._next_refill + self.refill_seconds
                if self._charges < self.capacity
                else None
            )
        if self._charges >= self.capacity:
            self._next_refill = None

    # ------------------------------------------------------------------ public API

    def allow(self, user_id: int, *, automatic: bool) -> str:
        """ALLOWED, or the reason this check is refused.

        The charge pool covers every kind of check. The per-user cooldown applies
        only to automatic ones - someone who deliberately asks should not be told
        to wait because a link happened to land ten seconds earlier.
        """
        self._refill()
        if self._charges <= 0:
            return NO_CHARGES
        if automatic:
            last = self._last_by_user.get(user_id)
            if last is not None and time.time() - last < self._cooldown:
                return COOLING_DOWN
        return ALLOWED

    def spend(self, user_id: int) -> None:
        """Take one charge. Call this only after allow() said yes."""
        self._refill()
        if self._charges <= 0:
            log.warning("spend() called with an empty pool - this should not happen")
            return
        self._charges -= 1
        self._last_by_user[user_id] = time.time()
        if self._next_refill is None and self._charges < self.capacity:
            # First charge out of a full pool starts the clock.
            self._next_refill = time.time() + self.refill_seconds
        log.info(
            "spent a charge - %d/%d left, next in %s",
            self._charges, self.capacity, self.next_charge_in_text or "n/a",
        )

    # ------------------------------------------------------------------ reporting

    @property
    def available(self) -> int:
        self._refill()
        return self._charges

    @property
    def seconds_to_next_charge(self) -> float | None:
        self._refill()
        if self._next_refill is None:
            return None
        return max(0.0, self._next_refill - time.time())

    @property
    def next_charge_in_text(self) -> str:
        """Precise-ish, for /status and logs."""
        remaining = self.seconds_to_next_charge
        return format_duration(remaining) if remaining is not None else ""

    @property
    def next_charge_hours_pt(self) -> str:
        """Rounded to whole hours, for what the bot says in the channel."""
        remaining = self.seconds_to_next_charge
        return format_hours_pt(remaining) if remaining is not None else ""

    def cooldown_remaining(self, user_id: int) -> int:
        last = self._last_by_user.get(user_id)
        if last is None:
            return 0
        return max(0, self._cooldown - int(time.time() - last))
