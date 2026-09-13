"""The retry policies and the `Retry-After` parser, with no Home Assistant.

Loaded by file path for the reason `test_forecast.py` gives: `pacing.py` is
claimed pure, and a path load is what makes that claim fail loudly if an
`aiohttp` or `homeassistant` import ever leaks in.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "pacing.py"
_spec = importlib.util.spec_from_file_location("tempest_wx_pacing", _MODULE_PATH)
assert _spec and _spec.loader
pacing = importlib.util.module_from_spec(_spec)
sys.modules["tempest_wx_pacing"] = pacing
_spec.loader.exec_module(pacing)

parse_retry_after = pacing.parse_retry_after
throttled_retry = pacing.throttled_retry
unreachable_retry = pacing.unreachable_retry
MAX_BACKOFF_SECONDS = pacing.MAX_BACKOFF_SECONDS

# The component's own numbers, restated here rather than imported: const.py
# is HA-free today, but the test is about the SHAPE of the curve, and a change
# to the cadence should not silently move every expected value below.
FLOOR = 60
INTERVAL = 300
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_pacing_imports_nothing_from_ha_or_aiohttp() -> None:
    """The purity claim, checked against the loaded module's actual imports."""
    names = set(sys.modules)
    assert not {n for n in names if n.startswith(("homeassistant", "aiohttp"))}


# --- parse_retry_after -------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        (" 7 ", 7.0),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("   ", None),
        ("-5", None),  # not a digit string; not a date either
        ("soon", None),
        ("1.5", None),  # RFC 9110 delay-seconds is an integer
    ],
)
def test_retry_after_delay_seconds(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, NOW) == expected


def test_retry_after_http_date_in_the_future_is_a_delay() -> None:
    assert parse_retry_after("Sun, 13 Sep 2026 12:05:00 GMT", NOW) == 300.0


def test_retry_after_http_date_already_passed_is_unusable() -> None:
    assert parse_retry_after("Sun, 13 Sep 2026 11:59:59 GMT", NOW) is None


def test_retry_after_http_date_without_zone_is_unusable() -> None:
    # A naive datetime cannot be subtracted from an aware one; the parser
    # answers None rather than raising into the response handler.
    assert parse_retry_after("Sun, 13 Sep 2026 12:05:00", NOW) is None


# --- throttled_retry ---------------------------------------------------------


def test_throttled_honours_retry_after_over_backoff() -> None:
    assert throttled_retry(1, INTERVAL, FLOOR, retry_after=90) == 90
    assert throttled_retry(5, INTERVAL, FLOOR, retry_after=90) == 90


def test_throttled_retry_after_is_clamped_both_ways() -> None:
    assert throttled_retry(1, INTERVAL, FLOOR, retry_after=1) == FLOOR
    assert throttled_retry(1, INTERVAL, FLOOR, retry_after=86400) == MAX_BACKOFF_SECONDS


def test_throttled_without_retry_after_grows_from_the_interval() -> None:
    assert [throttled_retry(n, INTERVAL, FLOOR) for n in (1, 2, 3, 4, 5)] == [
        300,
        600,
        1200,
        2400,
        MAX_BACKOFF_SECONDS,
    ]


def test_throttled_never_waits_less_than_the_floor() -> None:
    assert throttled_retry(1, 10, FLOOR) == FLOOR


def test_throttled_treats_zero_failures_as_the_first() -> None:
    assert throttled_retry(0, INTERVAL, FLOOR) == throttled_retry(1, INTERVAL, FLOOR)


# --- unreachable_retry -------------------------------------------------------


def test_unreachable_starts_at_the_floor_and_settles_at_the_interval() -> None:
    assert [unreachable_retry(n, INTERVAL, FLOOR) for n in (1, 2, 3, 4, 5)] == [
        60,
        120,
        240,
        300,
        300,
    ]


def test_unreachable_never_exceeds_a_healthy_poll() -> None:
    assert unreachable_retry(50, INTERVAL, FLOOR) == INTERVAL


def test_unreachable_never_goes_below_the_floor_even_at_a_short_interval() -> None:
    # A payload that asked for the floor itself: first retry is the floor, and
    # the cap is the floor too, so the curve is flat.
    assert unreachable_retry(1, FLOOR, FLOOR) == FLOOR
    assert unreachable_retry(3, FLOOR, FLOOR) == FLOOR
    # An interval below the floor cannot pull the retry below it either.
    assert unreachable_retry(1, 10, FLOOR) == FLOOR


def test_unreachable_treats_zero_failures_as_the_first() -> None:
    assert unreachable_retry(0, INTERVAL, FLOOR) == FLOOR


# --- the two shapes are actually different -----------------------------------


def test_throttled_is_never_sooner_than_unreachable_for_the_same_failure() -> None:
    """The whole point: a refusal waits longer than a silence, at every count."""
    for n in range(1, 8):
        assert throttled_retry(n, INTERVAL, FLOOR) >= unreachable_retry(
            n, INTERVAL, FLOOR
        )


def test_selftest_the_purity_check_can_fail() -> None:
    """Prove the import assertion above would notice a leak."""
    fake = "homeassistant_selftest_marker"
    sys.modules[fake] = sys  # any object will do
    try:
        assert {n for n in sys.modules if n.startswith("homeassistant")}
    finally:
        del sys.modules[fake]
