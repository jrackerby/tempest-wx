"""Pure transforms over the Tempest `better_forecast` payload.

THIS MODULE IMPORTS NOTHING FROM `homeassistant`, deliberately, the same way
`household_state`'s resolver does (LAW.md §11). Everything here is a plain
function over plain dicts, so the whole condition map and every forecast row
is testable without a running Home Assistant and without a Tempest token.

WHY THIS EXISTS AT ALL — the defect it is written to not repeat. The HACS
integration this replaces has TWO condition maps that disagree. Its current
condition renders through the component's own `STATE_MAP`; its forecast rows
render through `weatherflow4py`'s `Icon.ha_icon` property. Run both over the
vendor's 19-member icon enum and 6 of them differ: `possibly-rainy-*` is
`rainy` on the current-condition path and `pouring` — Home Assistant's
HEAVIEST rain state — on the forecast path, so the same sky an hour from now
reads as a downpour purely because a different function looked at it.
`sleet` and `possibly-sleet-*` split `snowy-rainy` against `hail`, which are
different phenomena, not different intensities of one.

There is exactly ONE map here and both paths call it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Condition
# ---------------------------------------------------------------------------

# Tempest `icon` -> Home Assistant weather condition.
#
# Every value is a member of HA's condition enum. `possibly-*` icons are the
# vendor's way of saying "a chance of", so they map to the plain form of the
# phenomenon and never to an intensified one: a chance of rain is `rainy`, not
# `pouring`. Intensity is decided separately, below, from the vendor's own
# `conditions` text, because that is the only place the API actually reports
# it.
CONDITION_MAP: dict[str, str] = {
    "clear-day": "sunny",
    "clear-night": "clear-night",
    "cloudy": "cloudy",
    "foggy": "fog",
    "partly-cloudy-day": "partlycloudy",
    "partly-cloudy-night": "partlycloudy",
    "possibly-rainy-day": "rainy",
    "possibly-rainy-night": "rainy",
    "possibly-sleet-day": "snowy-rainy",
    "possibly-sleet-night": "snowy-rainy",
    "possibly-snow-day": "snowy",
    "possibly-snow-night": "snowy",
    "possibly-thunderstorm-day": "lightning-rainy",
    "possibly-thunderstorm-night": "lightning-rainy",
    "rainy": "rainy",
    "sleet": "snowy-rainy",
    "snow": "snowy",
    "thunderstorm": "lightning",
    "windy": "windy",
}

# The vendor's `conditions` strings that mean the rain is heavy enough for HA's
# `pouring`. Lower-cased on comparison. The API reports intensity ONLY in this
# text field — the icon is `rainy` for a drizzle and for an extreme rain alike —
# so this is a real signal being read, not a threshold somebody invented.
_POURING_TEXT: frozenset[str] = frozenset(
    {"heavy rain", "very heavy rain", "extreme rain"}
)

# Every condition this module is allowed to emit, for the self-test to assert
# against. `exceptional` is deliberately absent: it is HA's SEVERE-WEATHER
# state, not its unknown state, and the vendor library's habit of mapping an
# unrecognised icon to it turns "we did not understand this" into "there is
# dangerous weather", which is the more expensive of the two errors on a
# household wall.
VALID_CONDITIONS: frozenset[str] = frozenset(
    {
        "clear-night",
        "cloudy",
        "exceptional",
        "fog",
        "hail",
        "lightning",
        "lightning-rainy",
        "partlycloudy",
        "pouring",
        "rainy",
        "snowy",
        "snowy-rainy",
        "sunny",
        "windy",
        "windy-variant",
    }
)


def map_condition(icon: Any, conditions_text: Any = None) -> str | None:
    """Map a Tempest icon to a Home Assistant condition.

    Returns ``None`` — never a string — when the icon is missing or not one the
    vendor documents. `None` is what HA's weather entity wants for "cannot
    say": it reads as `unknown` on the entity and logs nothing, whereas an
    out-of-enum string fails validation and writes an error every render. This
    file's YAML predecessor learned the same thing the same way (GH-62).
    """
    if not isinstance(icon, str):
        return None
    condition = CONDITION_MAP.get(icon.strip().lower())
    if condition is None:
        return None
    if condition == "rainy" and isinstance(conditions_text, str):
        if conditions_text.strip().lower() in _POURING_TEXT:
            return "pouring"
    return condition


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce to float, or None. Booleans are not numbers here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_utc(epoch: Any) -> datetime | None:
    """Epoch seconds -> aware UTC datetime, or None.

    Aware, via `datetime.fromtimestamp(..., UTC)`. The vendor library reaches
    for `datetime.utcfromtimestamp()`, which is deprecated and scheduled for
    removal, and which returns a NAIVE datetime that only happens to be right
    because it is immediately stamped with a literal "Z".
    """
    seconds = _num(epoch)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(epoch: Any) -> str | None:
    """Epoch seconds -> RFC 3339 string, or None."""
    moment = to_utc(epoch)
    return None if moment is None else moment.isoformat()


# ---------------------------------------------------------------------------
# Forecast rows
# ---------------------------------------------------------------------------
#
# BOTH BUILDERS DROP A BAD ROW AND KEEP THE REST. The integration being
# replaced does the opposite: its hourly builder is a bare list comprehension
# over `x.ha_forecast`, and `ha_forecast` dereferences `self.icon.ha_icon`
# unguarded while the model declares `icon: Icon | None = None`. One row
# without an icon therefore raises AttributeError out of the comprehension and
# the caller gets NO forecast at all — 240 good hours discarded by one bad one.
# Verified by constructing that row and calling the property.


def daily_forecast(payload: Any) -> list[dict[str, Any]]:
    """Build HA daily forecast rows from a `better_forecast` payload."""
    rows: list[dict[str, Any]] = []
    for entry in _rows(payload, "daily"):
        when = _iso(entry.get("day_start_local"))
        if when is None:
            continue
        rows.append(
            _drop_none(
                {
                    "datetime": when,
                    "condition": map_condition(
                        entry.get("icon"), entry.get("conditions")
                    ),
                    "native_temperature": _num(entry.get("air_temp_high")),
                    "native_templow": _num(entry.get("air_temp_low")),
                    "precipitation_probability": _num(
                        entry.get("precip_probability")
                    ),
                }
            )
        )
    return rows


def hourly_forecast(payload: Any) -> list[dict[str, Any]]:
    """Build HA hourly forecast rows from a `better_forecast` payload."""
    rows: list[dict[str, Any]] = []
    for entry in _rows(payload, "hourly"):
        when = _iso(entry.get("time"))
        if when is None:
            continue
        rows.append(
            _drop_none(
                {
                    "datetime": when,
                    "condition": map_condition(
                        entry.get("icon"), entry.get("conditions")
                    ),
                    "humidity": _num(entry.get("relative_humidity")),
                    "native_apparent_temperature": _num(entry.get("feels_like")),
                    "native_precipitation": _num(entry.get("precip")),
                    "native_pressure": _num(entry.get("sea_level_pressure")),
                    "native_temperature": _num(entry.get("air_temperature")),
                    "native_wind_gust_speed": _num(entry.get("wind_gust")),
                    "native_wind_speed": _num(entry.get("wind_avg")),
                    "precipitation_probability": _num(
                        entry.get("precip_probability")
                    ),
                    "uv_index": _num(entry.get("uv")),
                    "wind_bearing": _num(entry.get("wind_direction")),
                }
            )
        )
    return rows


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    """Pull `forecast.<key>` out of a payload, tolerating any shape."""
    if not isinstance(payload, dict):
        return []
    forecast = payload.get("forecast")
    if not isinstance(forecast, dict):
        return []
    rows = forecast.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _drop_none(row: dict[str, Any]) -> dict[str, Any]:
    """Strip keys whose value is None.

    An ABSENT key and a key set to None are the same to HA, but they are not
    the same to a board reading the dict, and `weather_home.yaml` already
    settled this estate's position: "clear" and "cannot tell" are different
    answers, and a missing measurement is carried as missing rather than
    defaulted. `datetime` is never None here — every caller checks it first.
    """
    return {key: value for key, value in row.items() if value is not None}


# ---------------------------------------------------------------------------
# Current conditions
# ---------------------------------------------------------------------------


def current_conditions(payload: Any) -> dict[str, Any]:
    """Return the payload's `current_conditions` block, or an empty dict."""
    if not isinstance(payload, dict):
        return {}
    current = payload.get("current_conditions")
    return current if isinstance(current, dict) else {}


def pick(source: Any, *keys: str) -> Any:
    """First present, non-None value among `keys`.

    THE LIGHTNING FIELDS ARE SPELLED TWO WAYS AND THE DOCUMENTATION IS THE
    WRONG ONE. WeatherFlow's published `better_forecast` reference names them
    `lighting_strike_count_last_1hr`, `lighting_strike_last_distance` and so on
    — "lighting", no first `n`. The LIVE endpoint does not: measured against
    station 197799, every one of them comes back correctly spelled,
    `lightning_*`. So the docs are wrong and `weatherflow4py`'s declaration is
    right, which is the opposite of what this docstring claimed before anyone
    had called the API with a token.

    The dual lookup stays, and is now defending against the documentation
    rather than against the library: the two disagree, this component controls
    neither, and a lightning counter reading zero because a key never bound is
    the failure mode you cannot see — it looks exactly like calm weather.
    Whichever spelling the API emits is the one that answers.
    """
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def refresh_interval(payload: Any, default: int, floor: int = 60) -> int:
    """Honour the payload's own `refresh_interval_seconds`.

    The API states how often it is worth asking again. The integration being
    replaced ignores it and polls a FORECAST endpoint every 60 seconds, which
    is 1440 calls a day against a rate-limited vendor for data that does not
    move that fast. `floor` guards against a payload asking to be hammered.
    """
    advertised = _num(payload.get("refresh_interval_seconds")) if isinstance(
        payload, dict
    ) else None
    if advertised is None or advertised <= 0:
        return default
    return max(floor, int(advertised))
