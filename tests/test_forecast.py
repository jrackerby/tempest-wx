"""Self-test for the pure transform layer.

Runs with no Home Assistant, no network and no Tempest token: `forecast.py`
imports nothing from `homeassistant`, so every assertion here is about the
transform itself.

An assertion set needs a self-test proving it CAN fail. The three
regression tests at the bottom each re-implement the defect they guard against
and assert that the OLD behaviour would have been caught — so a green run is
evidence the check discriminates, not evidence it was never exercised.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# LOADED BY FILE PATH, NOT AS `tempest_wx.forecast`, and that is an assertion
# rather than a convenience. Importing through the package would execute
# `tempest_wx/__init__.py`, which imports `homeassistant` — so a path load is
# the only way this file can prove what its docstring claims: that the
# transform layer stands up with no Home Assistant present at all. If a
# `homeassistant` import ever leaks into forecast.py, this collection fails.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "forecast.py"
_spec = importlib.util.spec_from_file_location("tempest_wx_forecast", _MODULE_PATH)
assert _spec and _spec.loader
forecast = importlib.util.module_from_spec(_spec)
sys.modules["tempest_wx_forecast"] = forecast
_spec.loader.exec_module(forecast)

CONDITION_MAP = forecast.CONDITION_MAP
VALID_CONDITIONS = forecast.VALID_CONDITIONS
current_conditions = forecast.current_conditions
daily_forecast = forecast.daily_forecast
hourly_forecast = forecast.hourly_forecast
map_condition = forecast.map_condition
pick = forecast.pick
refresh_interval = forecast.refresh_interval
to_utc = forecast.to_utc

FIXTURE = Path(__file__).parent / "fixtures" / "better_forecast.json"


def test_the_transform_layer_needs_no_home_assistant() -> None:
    """Nothing in forecast.py reaches into `homeassistant`.

    The path load at the top of this file is the live half of the proof — it
    would have raised on collection if the module needed HA. This is the
    static half, and it catches the case where HA happens to be installed in
    the test environment and an accidental import therefore succeeds quietly.
    """
    source = _MODULE_PATH.read_text()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and "homeassistant" in line
    ]
    assert not offenders, offenders


@pytest.fixture(name="payload")
def payload_fixture() -> dict:
    """A recorded-shape better_forecast payload."""
    return json.loads(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# Condition map
# ---------------------------------------------------------------------------


def test_every_mapped_condition_is_a_real_ha_condition() -> None:
    """Nothing in the map can put an out-of-enum string on the entity."""
    assert set(CONDITION_MAP.values()) <= VALID_CONDITIONS


def test_map_covers_every_documented_vendor_icon() -> None:
    """The map answers for all 19 icons WeatherFlow documents."""
    documented = {
        "clear-day", "clear-night", "cloudy", "foggy",
        "partly-cloudy-day", "partly-cloudy-night",
        "possibly-rainy-day", "possibly-rainy-night",
        "possibly-sleet-day", "possibly-sleet-night",
        "possibly-snow-day", "possibly-snow-night",
        "possibly-thunderstorm-day", "possibly-thunderstorm-night",
        "rainy", "sleet", "snow", "thunderstorm", "windy",
    }
    assert documented == set(CONDITION_MAP)
    assert len(documented) == 19


def test_unknown_icon_is_none_not_exceptional() -> None:
    """`exceptional` means severe weather, not 'we did not understand'."""
    for bogus in (None, "", "  ", 42, "not-an-icon", ["rainy"]):
        assert map_condition(bogus) is None


def test_icon_lookup_is_case_and_whitespace_tolerant() -> None:
    """A payload that shouts or pads still maps."""
    assert map_condition("  CLEAR-DAY ") == "sunny"


def test_possibly_rainy_is_rainy_not_pouring() -> None:
    """The chance of rain is rain, not HA's heaviest rain state."""
    assert map_condition("possibly-rainy-day") == "rainy"
    assert map_condition("possibly-rainy-night") == "rainy"


def test_sleet_is_snowy_rainy_not_hail() -> None:
    """Sleet and hail are different phenomena."""
    assert map_condition("sleet") == "snowy-rainy"
    assert map_condition("possibly-sleet-day") == "snowy-rainy"


def test_pouring_comes_from_the_vendors_own_intensity_text() -> None:
    """Only the vendor saying 'heavy' promotes rainy to pouring."""
    assert map_condition("rainy", "Light Rain") == "rainy"
    assert map_condition("rainy", "Moderate Rain") == "rainy"
    assert map_condition("rainy", "Heavy Rain") == "pouring"
    assert map_condition("rainy", "  very heavy rain  ") == "pouring"
    assert map_condition("rainy", "Extreme Rain") == "pouring"
    # Intensity text never promotes a non-rain icon.
    assert map_condition("snow", "Heavy Rain") == "snowy"


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def test_to_utc_is_timezone_aware() -> None:
    """Aware, not the naive value utcfromtimestamp returns."""
    moment = to_utc(1788651000)
    assert moment is not None
    assert moment.tzinfo is not None
    assert moment.utcoffset() == datetime.now(UTC).utcoffset()


def test_to_utc_rejects_junk() -> None:
    """Anything uncoercible is None, not a crash."""
    for bogus in (None, "", "abc", {}, [], True, False):
        assert to_utc(bogus) is None


def test_pick_takes_the_first_present_key() -> None:
    """Both vendor spellings resolve to the same reading."""
    assert pick({"lighting_strike_count_last_1hr": 4}, "lighting_strike_count_last_1hr", "lightning_strike_count_last_1hr") == 4
    assert pick({"lightning_strike_count_last_1hr": 7}, "lighting_strike_count_last_1hr", "lightning_strike_count_last_1hr") == 7
    assert pick({}, "a", "b") is None
    assert pick(None, "a") is None


def test_refresh_interval_honours_the_payload_and_its_floor() -> None:
    """The API's own cadence wins, but never below the floor."""
    assert refresh_interval({"refresh_interval_seconds": 600}, 300) == 600
    assert refresh_interval({"refresh_interval_seconds": 5}, 300, floor=60) == 60
    assert refresh_interval({}, 300) == 300
    assert refresh_interval({"refresh_interval_seconds": 0}, 300) == 300
    assert refresh_interval("not a dict", 300) == 300


# ---------------------------------------------------------------------------
# Forecast rows
# ---------------------------------------------------------------------------


def test_daily_rows_from_the_fixture(payload: dict) -> None:
    """The daily builder produces usable rows."""
    rows = daily_forecast(payload)
    assert len(rows) == 3
    first = rows[0]
    assert first["datetime"] == "2026-09-06T04:00:00+00:00"
    assert first["condition"] == "partlycloudy"
    assert first["native_temperature"] == 31.0
    assert first["native_templow"] == 21.0
    assert first["precipitation_probability"] == 10.0


def test_hourly_rows_from_the_fixture(payload: dict) -> None:
    """The hourly builder produces usable rows."""
    rows = hourly_forecast(payload)
    # 4 in the fixture, one of which has no `time` and is dropped.
    assert len(rows) == 3
    assert rows[0]["condition"] == "rainy"
    assert rows[0]["native_temperature"] == 24.0
    assert rows[0]["wind_bearing"] == 180.0


def test_a_row_with_no_icon_is_kept_with_no_condition(payload: dict) -> None:
    """An iconless row loses its condition and NOTHING else."""
    rows = hourly_forecast(payload)
    iconless = [row for row in rows if "condition" not in row]
    assert len(iconless) == 1
    # The rest of that row survived — this is the whole point.
    assert iconless[0]["native_temperature"] == 26.0
    assert iconless[0]["humidity"] == 55.0


def test_absent_measurements_are_absent_not_zero(payload: dict) -> None:
    """A missing field is omitted, never defaulted to 0."""
    row = daily_forecast(payload)[2]
    assert "native_templow" not in row
    assert "precipitation_probability" not in row


def test_malformed_payloads_yield_no_rows_rather_than_raising() -> None:
    """Every shape of junk returns an empty list."""
    for bogus in (None, {}, [], "text", 5, {"forecast": None},
                  {"forecast": {"daily": None}}, {"forecast": {"daily": "x"}},
                  {"forecast": {"daily": [None, 3, "x"]}}):
        assert daily_forecast(bogus) == []
        assert hourly_forecast(bogus) == []


def test_current_conditions_extraction() -> None:
    """The block comes back, or an empty dict."""
    assert current_conditions({"current_conditions": {"uv": 3}}) == {"uv": 3}
    assert current_conditions({"current_conditions": None}) == {}
    assert current_conditions(None) == {}


def test_every_emitted_condition_is_valid(payload: dict) -> None:
    """No row can carry a condition HA would reject."""
    for row in daily_forecast(payload) + hourly_forecast(payload):
        if "condition" in row:
            assert row["condition"] in VALID_CONDITIONS


# ---------------------------------------------------------------------------
# Self-test: these prove the checks above CAN fail
# ---------------------------------------------------------------------------


def test_selftest_the_vendor_maps_would_fail_these_assertions() -> None:
    """Re-implement the two maps this component replaces and catch them.

    If this test ever passes trivially — because the maps agree — the
    disagreement assertions above have stopped discriminating.
    """
    # weatherflow4py's Icon.ha_icon, transcribed.
    vendor_lib = {
        "possibly-rainy-day": "pouring",
        "possibly-rainy-night": "pouring",
        "possibly-sleet-day": "hail",
        "possibly-sleet-night": "hail",
        "sleet": "hail",
        "windy": "windy-variant",
    }
    disagreements = [
        icon for icon, theirs in vendor_lib.items() if CONDITION_MAP[icon] != theirs
    ]
    assert len(disagreements) == 6, (
        "the vendor maps no longer disagree with ours; "
        "the tests asserting our values are no longer meaningful"
    )


def test_selftest_the_old_hourly_builder_would_have_crashed() -> None:
    """The unguarded dereference, reproduced, still raises.

    This is the shape of `weatherflow4py`'s `ha_forecast`: it reads
    `self.icon.ha_icon` with no None check while the model defaults `icon` to
    None. Our builder handles the same row; theirs takes the whole list down.
    """

    class _Row:
        icon = None

        @property
        def ha_forecast(self) -> dict:
            return {"condition": self.icon.ha_icon}  # type: ignore[union-attr]

    with pytest.raises(AttributeError):
        [row.ha_forecast for row in [_Row()]]

    # Ours, on the equivalent input, keeps the row and drops only the condition.
    ours = hourly_forecast(
        {"forecast": {"hourly": [{"time": 1788651000, "air_temperature": 20}]}}
    )
    assert len(ours) == 1
    assert "condition" not in ours[0]
    assert ours[0]["native_temperature"] == 20.0


def test_selftest_a_wrong_condition_map_is_caught() -> None:
    """Corrupt the map in a copy and confirm the validity check fires."""
    broken = dict(CONDITION_MAP)
    broken["rainy"] = "torrential"  # not an HA condition
    assert not set(broken.values()) <= VALID_CONDITIONS


def test_intensity_promotion_reaches_the_built_rows(payload: dict) -> None:
    """The pouring promotion is not just a unit-level behaviour.

    The fixture's second daily row and second hourly row both carry the
    `rainy` icon with heavy-rain text. If the builders dropped `conditions`
    on the way to map_condition, both would silently read `rainy` and this is
    the only place that would notice.
    """
    assert daily_forecast(payload)[1]["condition"] == "pouring"
    assert hourly_forecast(payload)[1]["condition"] == "pouring"
    # ...and the moderate-rain row next to it is NOT promoted.
    assert hourly_forecast(payload)[0]["condition"] == "rainy"
