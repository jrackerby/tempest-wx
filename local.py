"""Current conditions from the LOCAL Tempest radio, with the cloud as backup.

WHY THIS EXISTS. The weather entity started cloud-only, and the entity it
replaces — `weather.forecast_home`, the template in
`packages/weather_home.yaml` — is local-only. Cutting the boards over without
this would have traded one weakness for another: every wall panel would keep
its forecast through a WAN outage and lose its TEMPERATURE, which the template
entity never did. A household wall that goes blank because the internet
blinked is worse than one showing a stale forecast.

So measurements prefer the local UDP radio and fall back to the cloud, while
condition and forecast stay cloud-only because the radio cannot produce them.
Losing the WAN degrades this entity instead of emptying it.

THE ENTITY IDS ARE THE ONES `weather_home.yaml` ALREADY READ, deliberately:
preserving them makes the cutover a swap rather than a rewrite, and each one
is a claim that file had already tested against the live estate. They belong
to the separate HACS `tempest` integration running in local-UDP mode. That is
a real coupling, declared here rather than hidden — if that integration goes
away these lookups find nothing, which is precisely the case the cloud
fallback covers.

NOT A PREFIX MATCH. TOOLS.md is explicit that grouping a device's entities by
common id prefix silently drops every entity on a different one, because each
entity keeps the area it was born in. These are full ids, named one at a time.

UNITS ARE CONVERTED, NEVER ASSUMED. The local platform publishes °F, inHg and
mph; this component's weather entity declares metric natives and lets Home
Assistant convert for display. Handing a local °F reading straight to a
property declared in °C would have HA convert it a second time and render 26°
on a 79° day — the kind of fault that looks like a broken sensor rather than a
broken unit. Every value is converted from the unit the source entity actually
reports, read off its own `unit_of_measurement`, using Home Assistant's own
converters rather than constants retyped here.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import (
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import (
    PressureConverter,
    SpeedConverter,
    TemperatureConverter,
)

from .const import LOGGER

# Weather-entity reading -> the local sensor that answers it.
#
# `station_pressure`, NOT `pressure`: weather_home.yaml records that
# `sensor.tempest_sensor_pressure` read 0.786 inHg against a station pressure
# of 29.24 — wrong by a factor of about 37 — and that bad sensor is what once
# sent a session to NWS for a reading the Tempest already had locally.
LOCAL_SOURCES: dict[str, str] = {
    "air_temperature": "sensor.tempest_sensor_air_temperature",
    "feels_like": "sensor.tempest_sensor_feels_like",
    "dew_point": "sensor.tempest_sensor_dew_point",
    "relative_humidity": "sensor.tempest_sensor_relative_humidity",
    "station_pressure": "sensor.tempest_sensor_station_pressure",
    "wind_avg": "sensor.tempest_sensor_wind_speed_average",
    "wind_gust": "sensor.tempest_sensor_wind_gust",
    "wind_direction": "sensor.tempest_sensor_wind_direction_average",
    "uv": "sensor.tempest_sensor_uv_index",
}

# Which converter and target unit each reading needs. A reading absent here is
# unitless (humidity, UV, a compass bearing) and passes through untouched.
_CONVERSIONS: dict[str, tuple[Any, str]] = {
    "air_temperature": (TemperatureConverter, UnitOfTemperature.CELSIUS),
    "feels_like": (TemperatureConverter, UnitOfTemperature.CELSIUS),
    "dew_point": (TemperatureConverter, UnitOfTemperature.CELSIUS),
    "station_pressure": (PressureConverter, UnitOfPressure.MBAR),
    "wind_avg": (SpeedConverter, UnitOfSpeed.METERS_PER_SECOND),
    "wind_gust": (SpeedConverter, UnitOfSpeed.METERS_PER_SECOND),
}

# A local sensor in any of these states has not answered. `none` is included
# because HA renders a Jinja `none` to the literal string, and the template
# entity being replaced published exactly that for `wind_bearing`.
UNREADABLE = frozenset({"unknown", "unavailable", "none", ""})


def read_local(hass: HomeAssistant, key: str) -> float | None:
    """One local reading, converted to this component's native units.

    None means "the radio did not say", which is the caller's signal to ask the
    cloud — it is never a zero. `ok at zero` and `could not read` are different
    values at the source (LAW.md §11); collapsing them here would publish a
    calm, cold, dry day every time the hub went quiet.
    """
    entity_id = LOCAL_SOURCES.get(key)
    if entity_id is None:
        return None

    state = hass.states.get(entity_id)
    if state is None or str(state.state).strip().lower() in UNREADABLE:
        return None

    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None

    conversion = _CONVERSIONS.get(key)
    if conversion is None:
        return value

    converter, target = conversion
    source = state.attributes.get("unit_of_measurement")
    if source is None or source == target:
        return value

    try:
        return converter.convert(value, source, target)
    except (ValueError, TypeError) as err:
        # A unit this converter does not know is a reason to fall through to
        # the cloud, not a reason to publish the raw number in the wrong unit.
        # Warned, not debugged: nobody can wait this out, it needs an edit.
        LOGGER.warning(
            "%s reports %r, which cannot be converted to %s (%s); "
            "falling back to the cloud reading",
            entity_id,
            source,
            target,
            err,
        )
        return None


def local_is_answering(hass: HomeAssistant) -> bool:
    """Whether the radio is producing anything at all.

    Used for availability, so the weather entity can stay up on local data
    through a cloud outage. Temperature alone is the test: a Tempest that is
    reporting reports it, and an entity with no temperature has nothing worth
    keeping a wall lit for.
    """
    return read_local(hass, "air_temperature") is not None
