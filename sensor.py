"""Sensors: what the radio measures, and what only the cloud can answer.

TWO SETS, ONE DEVICE, NO OVERLAP. The LOCAL set below is read straight off the
station's UDP broadcast — temperature, humidity, station pressure, the four
wind figures, light, rain and the station's own health — and the CLOUD set is
everything `better_forecast` knows that the radio never sends: sea-level
pressure, the windowed lightning counts, daily rain totals, today's forecast
row. No reading appears in both. Publishing one quantity from two sources gives
you two entities that disagree whenever the WAN blinks, and no way to tell
which one a dashboard is reading.

Until 0.2.0 the local half was not here at all: it lived in a separate
integration, and this one read nine of its entity ids. That is why these two
sets look like they were designed apart — they were, and merging them is what
lets this component stand on its own.

The lightning group is the one that changes what can be said at all. The radio
sends a strike count per report interval and nothing about the hour, so nothing
here classifies a `lightning` CONDITION from it: reading a per-interval counter
as "strikes now" is how a weather entity latches into `lightning` for ever
after the first strike. `better_forecast` answers that directly, with counts
already windowed to the last hour and the last three.
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
    DEGREE,
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumetricFlux,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TempestConfigEntry
from .const import DOMAIN
from .entity import TempestEntity, TempestLocalEntity
from .forecast import current_conditions, daily_forecast, pick, to_utc
from .udp import PRECIPITATION_TYPES

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
        # INHG IS NOT COSMETIC HERE. Without it HA's US-customary map sends a
        # PRESSURE device_class to psi, while the weather platform does its own
        # pressure handling and lands on inHg — so this integration published
        # the same quantity as "14.685 psi" on the sensor and "29.25 inHg" on
        # its own weather entity, and its own forecast rows. Found by loading
        # it, not by reading it; the code was identical either way.
        suggested_unit_of_measurement=UnitOfPressure.INHG,
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


@dataclass(frozen=True, kw_only=True)
class TempestLocalSensorDescription(SensorEntityDescription):
    """A sensor fed by one key off the local radio.

    `key` is the reading name `udp.py` publishes, not a separate identifier.
    One name, so the sensor, its staleness window and the wire field it comes
    from cannot drift apart into a three-way rename nobody catches.
    """

    transform: Callable[[Any], Any] | None = None


def _timestamp(value: Any) -> Any:
    """An epoch second to an aware datetime, for a timestamp sensor."""
    return None if value is None else dt_util.utc_from_timestamp(float(value))


LOCAL_SENSORS: tuple[TempestLocalSensorDescription, ...] = (
    # --- What the observation measures ------------------------------------
    TempestLocalSensorDescription(
        key="air_temperature",
        translation_key="air_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="relative_humidity",
        translation_key="relative_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    TempestLocalSensorDescription(
        key="station_pressure",
        translation_key="station_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.MBAR,
        # inHg for the same reason `sea_level_pressure` above declares it:
        # without it HA's US-customary map sends a PRESSURE device_class to
        # psi, and this station's two pressure readings would then render in
        # different units on the same card.
        suggested_unit_of_measurement=UnitOfPressure.INHG,
        suggested_display_precision=2,
    ),
    # --- What the observation implies -------------------------------------
    TempestLocalSensorDescription(
        key="dew_point",
        translation_key="dew_point",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="feels_like",
        translation_key="feels_like",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="wet_bulb_temperature",
        translation_key="wet_bulb_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="air_density",
        translation_key="air_density",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kg/m³",
        suggested_display_precision=4,
    ),
    # --- Wind. FOUR figures, not one, because they answer different questions:
    #     the three-second sample is what a flag is doing now, the average is
    #     what the minute did, and lull and gust are that minute's floor and
    #     ceiling. Collapsing them loses the spread, which is the part that
    #     says whether it is gusty.
    TempestLocalSensorDescription(
        key="wind_speed",
        translation_key="wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="wind_avg",
        translation_key="wind_avg",
        device_class=SensorDeviceClass.WIND_SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="wind_gust",
        translation_key="wind_gust",
        device_class=SensorDeviceClass.WIND_SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="wind_lull",
        translation_key="wind_lull",
        device_class=SensorDeviceClass.WIND_SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
    ),
    # A bearing is NOT a measurement to average or sum: 359° and 1° average to
    # due south. No state_class, deliberately, so nothing downstream offers to
    # do statistics on it.
    TempestLocalSensorDescription(
        key="wind_direction",
        translation_key="wind_direction",
        native_unit_of_measurement=DEGREE,
        suggested_display_precision=0,
    ),
    TempestLocalSensorDescription(
        key="wind_direction_avg",
        translation_key="wind_direction_avg",
        native_unit_of_measurement=DEGREE,
        suggested_display_precision=0,
    ),
    # --- Light -------------------------------------------------------------
    TempestLocalSensorDescription(
        key="illuminance",
        translation_key="illuminance",
        device_class=SensorDeviceClass.ILLUMINANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=LIGHT_LUX,
        suggested_display_precision=0,
    ),
    TempestLocalSensorDescription(
        key="uv",
        translation_key="uv",
        state_class=SensorStateClass.MEASUREMENT,
        # The literal HA itself uses for `UV_INDEX`; spelled rather than
        # imported so this module does not depend on a constant whose only
        # content is this string.
        native_unit_of_measurement="UV index",
        suggested_display_precision=1,
    ),
    TempestLocalSensorDescription(
        key="solar_radiation",
        translation_key="solar_radiation",
        device_class=SensorDeviceClass.IRRADIANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        suggested_display_precision=0,
    ),
    # --- Rain, as the radio reports it ------------------------------------
    #
    # NOT `TOTAL_INCREASING`. This is the accumulation for ONE report interval
    # and it returns to zero the moment the rain stops, so a total-increasing
    # state class would read every dry minute as a counter reset and the
    # long-term statistic would count each shower several times over. The
    # day and yesterday totals, which really are cumulative, come from the
    # cloud set above.
    TempestLocalSensorDescription(
        key="precip_accum_last_interval",
        translation_key="precip_accum_last_interval",
        device_class=SensorDeviceClass.PRECIPITATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=2,
    ),
    TempestLocalSensorDescription(
        key="precipitation_intensity",
        translation_key="precipitation_intensity",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
        suggested_display_precision=2,
    ),
    TempestLocalSensorDescription(
        key="precipitation_type",
        translation_key="precipitation_type",
        device_class=SensorDeviceClass.ENUM,
        options=list(PRECIPITATION_TYPES.values()),
    ),
    # --- Lightning, per report interval -----------------------------------
    TempestLocalSensorDescription(
        key="lightning_avg_distance",
        translation_key="lightning_avg_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
    ),
    TempestLocalSensorDescription(
        key="lightning_count",
        translation_key="lightning_count",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="strikes",
    ),
    # --- The hardware's own health ----------------------------------------
    TempestLocalSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TempestLocalSensorDescription(
        key="device_rssi",
        translation_key="device_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TempestLocalSensorDescription(
        key="hub_rssi",
        translation_key="hub_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # BOOT TIME, NOT UPTIME. A duration changes every time it is read, so an
    # uptime sensor writes a new state on every message for a device that has
    # not moved; the moment it came up is a constant that changes only when it
    # actually reboots, which is the event anybody watching this wants.
    TempestLocalSensorDescription(
        key="device_boot_time",
        translation_key="device_boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_timestamp,
    ),
    TempestLocalSensorDescription(
        key="hub_boot_time",
        translation_key="hub_boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_timestamp,
    ),
    TempestLocalSensorDescription(
        key="device_firmware",
        translation_key="device_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TempestLocalSensorDescription(
        key="hub_firmware",
        translation_key="hub_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: Any,
    entry: TempestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every sensor this station can answer, from both of its sources."""
    data = entry.runtime_data
    async_add_entities(
        [
            *(
                TempestSensor(data.coordinator, description)
                for description in SENSORS
            ),
            *(
                TempestLocalSensor(entry, data.station, description)
                for description in LOCAL_SENSORS
            ),
        ]
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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The vendor's own wording for a reading that is not a point value.

        `lightning_strike_last_distance` came back as 36 against a
        `_msg` of "34 - 38 km": the API reports a BUCKET and this sensor
        publishes its midpoint. The number is the useful thing for a graph or a
        threshold, so it stays the state — but a surface that wants to tell a
        person how far away the strike was should render the range, not imply a
        precision the sensor does not have.
        """
        if self.entity_description.key != "lightning_strike_last_distance":
            return None
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        message = pick(
            current_conditions(data),
            "lighting_strike_last_distance_msg",
            "lightning_strike_last_distance_msg",
        )
        return None if message is None else {"range": message}

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


class TempestLocalSensor(TempestLocalEntity, SensorEntity):
    """One reading pushed in off the station's radio."""

    entity_description: TempestLocalSensorDescription

    def __init__(
        self,
        entry: TempestConfigEntry,
        station: Any,
        description: TempestLocalSensorDescription,
    ) -> None:
        """Bind the description to the station's reading of the same name."""
        super().__init__(entry, station, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """The reading, transformed only where the wire type is not the state type.

        There is no unit conversion here and there is not meant to be: the
        radio emits °C, millibars, m/s and millimetres, which are exactly the
        natives declared above, so a value crosses this boundary untouched and
        Home Assistant converts once for display. The entity-id path this
        replaced had to convert on the way in, and a conversion on the way in
        is one that can happen twice.
        """
        value = self._value
        if value is None:
            return None
        transform = self.entity_description.transform
        return value if transform is None else transform(value)
