"""The bot's Discord status: one line per day, switching at midnight in Lisbon.

Lines are read from a text file so the ones your server actually runs stay out of
version control - see STATUS_FILE below. The defaults here are deliberately dull;
the funny ones are yours to write.

The order matters. Lines from the same family - same joke, same shape, same
running gag - should sit as far apart as possible so two days never feel like a
repeat, and that includes the wrap from the last line back to the first.

Discord caps a status at 128 characters.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
# One status per line. Blank lines and lines starting with # are ignored.
# Copy data/statuses.example.txt to data/statuses.txt and make it your own.
STATUS_FILE = BASE_DIR / "data" / "statuses.txt"

DISCORD_STATUS_LIMIT = 128

# Used when data/statuses.txt is absent, so a fresh clone still works.
DEFAULT_STATUSES: tuple[str, ...] = (
    "checking the sources",
    "reading the fine print",
    "asking for a citation",
    "following the footnotes",
    "comparing what was said to what was published",
)


def load_statuses(path: Path | None = None) -> tuple[str, ...]:
    """Read the status lines, falling back to the dull defaults."""
    path = path or STATUS_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("no %s - using the built-in statuses", path.name)
        return DEFAULT_STATUSES
    except OSError as exc:
        log.warning("could not read %s (%s) - using the built-in statuses", path.name, exc)
        return DEFAULT_STATUSES

    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > DISCORD_STATUS_LIMIT:
            log.warning("status over %d chars, skipping: %.40s...", DISCORD_STATUS_LIMIT, line)
            continue
        if line not in lines:
            lines.append(line)

    if not lines:
        log.warning("%s had no usable lines - using the built-in statuses", path.name)
        return DEFAULT_STATUSES
    log.info("loaded %d status line(s) from %s", len(lines), path.name)
    return tuple(lines)


STATUSES: tuple[str, ...] = load_statuses()


def status_for(day: date, statuses: tuple[str, ...] | None = None) -> str:
    """The status for a given day.

    Derived from the date, so restarting the bot mid-afternoon doesn't change
    today's line, and every line is used exactly once per cycle.
    """
    statuses = STATUSES if statuses is None else statuses
    if not statuses:
        return ""
    return statuses[day.toordinal() % len(statuses)]
