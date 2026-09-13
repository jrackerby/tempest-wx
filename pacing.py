"""When to ask the cloud again after it did not answer — a pure policy.

Imports nothing from `homeassistant` or `aiohttp` on purpose, so it can be
tested by file path like `forecast.py` is. `coordinator.py` feeds the answer
into `UpdateFailed(retry_after=...)`, which core honours for exactly the next
scheduled refresh; `api.py` feeds `parse_retry_after` from the response header.

TWO FAILURES, TWO SHAPES. They are not the same problem and one curve would
serve both badly.

  * THROTTLED — the vendor answered and said stop. The only correct move is to
    do what it said; where it gave no number, the interval GROWS away from the
    normal cadence on every consecutive refusal, because polling at the rate
    that just got refused is what a rate limit exists to punish.
  * UNREACHABLE — nothing answered: a timeout, a refused connection, a WAN
    outage. Nothing about the vendor's budget was spent, so the next attempt
    comes SOONER than the normal cadence, from the floor, doubling back up to
    the cadence and never past it. A blip recovers in a minute; a long outage
    settles at exactly the rate the entry polls at anyway, so the outage costs
    the vendor nothing extra and costs the household no more staleness than a
    quiet day would.

Both are bounded on both sides. The floor is the rate limit this component
enforces on itself regardless of what any header or payload says; the ceiling
keeps one bogus `Retry-After` (a date in the wrong year, a value in
milliseconds) from parking the entry for a day.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime

# The most one refusal can push the next poll out. A vendor saying "wait an
# hour" is honoured; one saying "wait a week" is more likely a bad header than
# a real instruction, and the entry re-asks at the ceiling instead.
MAX_BACKOFF_SECONDS = 3600


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a `Retry-After` header, or None when unusable.

    RFC 9110 allows two spellings: a delay in whole seconds, or an HTTP-date.
    Both are accepted; anything else — an empty header, a negative number, a
    date that has already passed, a date with no timezone — answers None and
    leaves the caller on its own backoff, rather than guessing.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    if text.isdigit():
        return float(text)

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    delay = (when - now).total_seconds()
    if delay <= 0:
        return None
    return delay


def throttled_retry(
    failures: int,
    interval: float,
    floor: float,
    retry_after: float | None = None,
    ceiling: float = MAX_BACKOFF_SECONDS,
) -> float:
    """Seconds until the next attempt after the vendor refused this one.

    `failures` counts consecutive failures INCLUDING this one, so it is never
    below 1. A stated `Retry-After` wins outright, clamped to [floor, ceiling];
    without one the wait doubles from the normal interval on each refusal.
    """
    failures = max(1, failures)
    if retry_after is not None:
        wanted = retry_after
    else:
        wanted = interval * (2 ** (failures - 1))
    return min(max(wanted, floor), ceiling)


def unreachable_retry(failures: int, interval: float, floor: float) -> float:
    """Seconds until the next attempt after nothing answered.

    Starts at the floor and doubles per consecutive failure, capped at the
    normal interval — never faster than the floor, never slower than a
    healthy poll. `failures` counts consecutive failures including this one.
    """
    failures = max(1, failures)
    wanted = floor * (2 ** (failures - 1))
    return min(max(wanted, floor), max(interval, floor))
