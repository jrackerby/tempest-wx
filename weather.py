"""The weather entity: current conditions and forecast, both from the Tempest.

The condition string is the STATION'S OWN, read from `better_forecast`'s
`icon`. It is not derived here, and that is the point. Deriving a condition
locally is what you are forced into when all you have is the UDP broadcast,
which reports no condition at all: precipitation, then fog, then a day/night
split, then cloud cover inferred from solar radiation against a clear-sky
model. That ladder has two places it reliably goes wrong. The dusk band, where
a fallback reading absolute illuminance slides sunny -> partlycloudy -> cloudy
with the time of day rather than the sky; and the night branch, which asserts
`clear-night` unconditionally below the horizon while refusing to publish
`cloud_coverage` there, so one attribute claims clear and the other says it
cannot tell. With the station's own icon there is no band to pick and no night
branch to contradict: the condition is whatever the Tempest says it is.

The weak link is now the vendor's icon vocabulary. `map_condition` returns
None for an icon it does not recognise, so an unmapped value reads `unknown`
on the glass rather than a confident wrong condition - but nothing warns when
the vendor adds one, and intensity is read from the free-text `conditions`
field, the only place the API reports it at all.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.weather import (
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import (
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TempestConfigEntry
from .const import DOMAIN
from .entity import TempestEntity
from .forecast import (
    current_conditions,
    daily_forecast,
    hourly_forecast,
    map_condition,
)
from .local import TempestLocalStation, signal_update

# Where the two sources spell one reading differently. The cloud reports a
# single `wind_direction`; the radio reports the three-second sample and the
# interval average separately, and the AVERAGE is the one that belongs on a
# weather entity — an instantaneous bearing on a gusty day swings the arrow on
# every dashboard several times a minute while saying nothing about the wind.
# Everything not listed here is spelled the same on both sides.
LOCAL_KEY: dict[str, str] = {"wind_direction": "wind_direction_avg"}

# Every entity on this platform reads an already-fetched coordinator
# payload; nothing here talks to the API on its own, so there is no
# request rate to limit. 0 = unlimited, which is the coordinator-backed
# convention. Declared rather than left to default so the quality scale's
# `parallel-updates` rule is answered explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: Any,
    entry: TempestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the station's weather entity."""
    data = entry.runtime_data
    async_add_entities([TempestWeather(data.coordinator, data.station)])


class TempestWeather(TempestEntity, SingleCoordinatorWeatherEntity):
    """Current conditions and forecast for one Tempest station."""

    _attr_name = None
    # const.FORECAST_UNITS asks the API for these, so no conversion happens
    # between the wire and here; Home Assistant converts for display.
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.MBAR
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_native_visibility_unit = UnitOfLength.KILOMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(
        self, coordinator: Any, station: TempestLocalStation
    ) -> None:
        """Bind the entity to its station's two sources."""
        super().__init__(coordinator)
        self._station = station
        self._attr_unique_id = f"{DOMAIN}_{coordinator.station_id}_weather"

    async def async_added_to_hass(self) -> None:
        """Also refresh when the radio speaks, not only when the cloud polls.

        `CoordinatorEntity` renders on the coordinator's schedule alone, which
        is once every few minutes. Without this the local readings would be
        collected the moment they arrived and then sit unpublished until the
        next cloud poll happened to write the entity — a temperature a minute
        fresh at the source and five minutes stale on the glass, with nothing
        anywhere to show the difference.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self.coordinator.config_entry.entry_id),
                self._async_local_update,
            )
        )

    @callback
    def _async_local_update(self, message_type: str) -> None:
        """The observation is what this entity reads; the wind sample is not."""
        if message_type == "obs_st":
            self.async_write_ha_state()

    @property
    def _current(self) -> dict[str, Any]:
        """The payload's current_conditions block, or an empty dict."""
        return current_conditions(self.coordinator.data)

    def _reading(self, key: str) -> float | None:
        """One current reading: the LOCAL radio first, the cloud second.

        Local wins because it is the same station over UDP without a WAN hop —
        fresher, and still answering when the internet is not. The cloud value
        is the fallback rather than the source, so a broadband outage ages this
        entity instead of emptying it. The template entity this replaced was
        local-only, and cutting the boards over to a cloud-only entity would
        have blanked every wall panel's temperature on the first WAN blip.

        FRESHNESS IS CHECKED, not just presence. A reading left over from
        before the radio went quiet is not a local reading any more; treating
        it as one would pin this entity to the last thing the station said
        before it died and never fall through to the cloud that is still
        answering.
        """
        local_key = LOCAL_KEY.get(key, key)
        if self._station.is_fresh(local_key):
            local = self._station.get(local_key)
            if isinstance(local, (int, float)) and not isinstance(local, bool):
                return float(local)

        value = self._current.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available while EITHER source can answer.

        CoordinatorEntity ties availability to the last cloud poll, which would
        take the whole entity down — temperature included — the moment the API
        blipped, even with the radio in the next room still reporting every
        minute. A wall that goes blank because a remote HTTP call failed is the
        exact failure this component was built to stop repeating.

        This is not a never-raise contract arriving by the back door. The
        coordinator still raises `UpdateFailed`, the forecast still goes away
        with the cloud, and `condition` still resolves to None when there is no
        payload. Only the readings the radio can answer survive.
        """
        return super().available or self._station.answering

    @property
    def condition(self) -> str | None:
        """The station's own condition. None, never a guess, when unmapped."""
        current = self._current
        return map_condition(current.get("icon"), current.get("conditions"))

    @property
    def native_temperature(self) -> float | None:
        """Air temperature."""
        return self._reading("air_temperature")

    @property
    def native_apparent_temperature(self) -> float | None:
        """Feels-like temperature."""
        return self._reading("feels_like")

    @property
    def native_dew_point(self) -> float | None:
        """Dew point."""
        return self._reading("dew_point")

    @property
    def humidity(self) -> float | None:
        """Relative humidity."""
        return self._reading("relative_humidity")

    @property
    def native_pressure(self) -> float | None:
        """Station pressure.

        `station_pressure`, not `sea_level_pressure`. The YAML entity this
        replaces records why: the HACS component's `sensor.tempest_sensor_pressure`
        read 0.786 inHg against a station pressure of 29.24 — wrong by a factor
        of about 37 — and that bad reading is what sent an earlier session to
        NWS for pressure it already had locally. Sea-level pressure is still
        published, as its own sensor, where a reader can tell the two apart.
        """
        return self._reading("station_pressure")

    @property
    def native_wind_speed(self) -> float | None:
        """Average wind speed."""
        return self._reading("wind_avg")

    @property
    def native_wind_gust_speed(self) -> float | None:
        """Wind gust."""
        return self._reading("wind_gust")

    @property
    def wind_bearing(self) -> float | None:
        """Wind direction, withheld while the vane is not actually turning.

        A calm Tempest reports bearing 0 — a real compass point over a dead
        vane — so below 1 m/s of averaged wind this publishes nothing rather
        than manufacture a direction nobody measured. Carried across from
        `weather_home.yaml`, which found it live.
        """
        # 1.0 m/s, and BOTH paths reach here in m/s already: the radio emits
        # m/s and the cloud is asked for it. The threshold is carried over from
        # weather_home.yaml, which expressed it as 1 mph — this is the stricter
        # of the two, so a bearing withheld there is withheld here.
        speed = self._reading("wind_avg")
        if speed is None or speed <= 1.0:
            return None
        return self._reading("wind_direction")

    @property
    def uv_index(self) -> float | None:
        """UV index."""
        return self._reading("uv")

    @callback
    def _async_forecast_daily(self) -> list[Forecast] | None:
        """Daily forecast rows."""
        return daily_forecast(self.coordinator.data)  # type: ignore[return-value]

    @callback
    def _async_forecast_hourly(self) -> list[Forecast] | None:
        """Hourly forecast rows."""
        return hourly_forecast(self.coordinator.data)  # type: ignore[return-value]
