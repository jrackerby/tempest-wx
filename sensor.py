"""Sensors for what the Tempest app shows and the local radio cannot.

DELIBERATELY NOT A SECOND COPY OF THE LOCAL SENSORS. Air temperature, humidity,
station pressure, wind, UV, illuminance, solar radiation and the rest already
arrive over local UDP, faster and without an internet dependency, and
republishing them from the cloud would give the estate two entities per
reading that disagree whenever the WAN blinks. Everything below is a value the
UDP broadcast does not carry.

The lightning group is the one that changes what the estate can say.
`packages/weather_home.yaml` refuses to classify a `lightning` condition at
all, and says why: `sensor.tempest_sensor_lightning_count` carries state_class
`total`, so whether it resets per observation window or accumulates for the
life of the station was never measured, and reading it as "strikes now" would
latch the weather entity into `lightning` forever after the station's first
strike. `better_forecast` answers that question directly with counts already
windowed to the last hour and the last three.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TempestConfigEntry
from .const import DOMAIN
from .entity import TempestEntity
from .forecast import current_conditions, daily_forecast, pick, to_utc

# Every entity on this platform reads an already-fetched coordinator
# payload; nothing here talks to the API on its own, so there is no
# request rate to limit. 0 = unlimited, which is the coordinator-backed
# convention. Declared rather than left to default so the quality scale's
# `parallel-updates` rule is answered explicitly.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TempestSensorDescription(SensorEntityDescription):
    """A sensor plus the function that pulls its value out of the payload."""

    value_fn: Callable[[dict[str, Any]], Any]


def _current(*keys: str) -> Callable[[dict[str, Any]], Any]:
    """Read one current-conditions field, tolerating the vendor's spellings."""

    def _read(payload: dict[str, Any]) -> Any:
        return pick(current_conditions(payload), *keys)

    return _read


def _epoch(*keys: str) -> Callable[[dict[str, Any]], Any]:
    """Read an epoch field as an aware datetime, for a timestamp sensor."""

    def _read(payload: dict[str, Any]) -> Any:
        return to_utc(pick(current_conditions(payload), *keys))

    return _read


def _today(key: str) -> Callable[[dict[str, Any]], Any]:
    """Read one field off the first daily forecast row."""

    def _read(payload: dict[str, Any]) -> Any:
        rows = daily_forecast(payload)
        return rows[0].get(key) if rows else None

    return _read


def _today_raw(key: str) -> Callable[[dict[str, Any]], Any]:
    """Read one raw field off the first daily row, before HA mapping."""

    def _read(payload: dict[str, Any]) -> Any:
        forecast = payload.get("forecast") if isinstance(payload, dict) else None
        rows = forecast.get("daily") if isinstance(forecast, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        value = row.get(key)
        return to_utc(value) if key in ("sunrise", "sunset") else value

    return _read


SENSORS: tuple[TempestSensorDescription, ...] = (
    # --- Pressure the local radio does not derive ------------------------
    TempestSensorDescription(
        key="sea_level_pressure",
        translation_key="sea_level_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.MBAR,
        suggested_display_precision=2,
        value_fn=_current("sea_level_pressure"),
    ),
    TempestSensorDescription(
        key="pressure_trend",
        translation_key="pressure_trend",
        device_class=SensorDeviceClass.ENUM,
        options=["falling", "steady", "rising"],
        value_fn=_current("pressure_trend"),
    ),
    # --- Lightning, windowed ---------------------------------------------
    TempestSensorDescription(
        key="lightning_strike_count_last_1hr",
        translation_key="lightning_strike_count_last_1hr",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="strikes",
        value_fn=_current(
            "lighting_strike_count_last_1hr", "lightning_strike_count_last_1hr"
        ),
    ),
    TempestSensorDescription(
        key="lightning_strike_count_last_3hr",
        translation_key="lightning_strike_count_last_3hr",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="strikes",
        value_fn=_current(
            "lighting_strike_count_last_3hr", "lightning_strike_count_last_3hr"
        ),
    ),
    TempestSensorDescription(
        key="lightning_strike_last_distance",
        translation_key="lightning_strike_last_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        value_fn=_current(
            "lighting_strike_last_distance", "lightning_strike_last_distance"
        ),
    ),
    TempestSensorDescription(
        key="lightning_strike_last_time",
        translation_key="lightning_strike_last_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        # No state_class. A timestamp is not a measurement, and declaring both
        # is exactly the contradiction HA logs against the integration this
        # replaces on every start: "sensor.tempest_hub_uptime is using state
        # class 'measurement' which is impossible considering device class
        # ('timestamp')".
        value_fn=_epoch(
            "lighting_strike_last_epoch", "lightning_strike_last_epoch"
        ),
    ),
    # --- Rain totals, which UDP reports only per minute -------------------
    TempestSensorDescription(
        key="precip_accum_local_day",
        translation_key="precip_accum_local_day",
        device_class=SensorDeviceClass.PRECIPITATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=2,
        value_fn=_current("precip_accum_local_day"),
    ),
    TempestSensorDescription(
        key="precip_accum_local_yesterday",
        translation_key="precip_accum_local_yesterday",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=2,
        value_fn=_current("precip_accum_local_yesterday"),
    ),
    TempestSensorDescription(
        key="precip_minutes_local_day",
        translation_key="precip_minutes_local_day",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_current("precip_minutes_local_day"),
    ),
    TempestSensorDescription(
        key="precip_minutes_local_yesterday",
        translation_key="precip_minutes_local_yesterday",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_current("precip_minutes_local_yesterday"),
    ),
    # --- Comfort and heat stress ------------------------------------------
    TempestSensorDescription(
        key="wet_bulb_globe_temperature",
        translation_key="wet_bulb_globe_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_current("wet_bulb_globe_temperature"),
    ),
    TempestSensorDescription(
        key="wind_direction_cardinal",
        translation_key="wind_direction_cardinal",
        value_fn=_current("wind_direction_cardinal"),
    ),
    TempestSensorDescription(
        key="conditions",
        translation_key="conditions",
        value_fn=_current("conditions"),
    ),
    # --- Today, off the first daily forecast row --------------------------
    TempestSensorDescription(
        key="forecast_temp_high",
        translation_key="forecast_temp_high",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_today("native_temperature"),
    ),
    TempestSensorDescription(
        key="forecast_temp_low",
        translation_key="forecast_temp_low",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_today("native_templow"),
    ),
    TempestSensorDescription(
        key="forecast_precip_probability",
        translation_key="forecast_precip_probability",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_today("precipitation_probability"),
    ),
    TempestSensorDescription(
        key="sunrise",
        translation_key="sunrise",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_today_raw("sunrise"),
    ),
    TempestSensorDescription(
        key="sunset",
        translation_key="sunset",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_today_raw("sunset"),
    ),
)


async def async_setup_entry(
    hass: Any,
    entry: TempestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every sensor this station can answer."""
    coordinator = entry.runtime_data
    async_add_entities(
        TempestSensor(coordinator, description) for description in SENSORS
    )


class TempestSensor(TempestEntity, SensorEntity):
    """One reading off the forecast payload."""

    entity_description: TempestSensorDescription

    def __init__(
        self, coordinator: Any, description: TempestSensorDescription
    ) -> None:
        """Bind the description to the station."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{coordinator.station_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """The reading, or None when the payload does not carry it."""
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        value = self.entity_description.value_fn(data)
        if isinstance(value, datetime):
            return value
        if self.entity_description.device_class is SensorDeviceClass.ENUM:
            # An out-of-options string is rejected by HA and logged on every
            # render. Lower-case it, then withhold anything still unlisted
            # rather than publish a value the entity has declared it cannot
            # hold.
            if not isinstance(value, str):
                return None
            value = value.strip().lower()
            options = self.entity_description.options or []
            return value if value in options else None
        return value
