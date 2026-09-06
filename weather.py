"""The weather entity: current conditions and forecast, both from the Tempest.

The condition string is the STATION'S OWN, read from `better_forecast`'s
`icon`. It is not derived here, and that is the point. `weather.forecast_home`
in `packages/weather_home.yaml` derives one, because local UDP reports no
condition at all: precipitation, then fog, then a day/night split, then cloud
cover inferred from solar radiation against a clear-sky model. GH-584 is open
against the dusk band of that derivation, asking for a ruling on what to say
between 0° and 10° of sun elevation, where the fallback branch reads absolute
illuminance and therefore slides sunny -> partlycloudy -> cloudy with the time
of day rather than with the sky. With the station's own icon there is nothing
to derive and no band to rule on.
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
from .local import local_is_answering, read_local

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
    async_add_entities([TempestWeather(entry.runtime_data)])


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

    def __init__(self, coordinator: Any) -> None:
        """Bind the entity to its station."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.station_id}_weather"

    @property
    def _current(self) -> dict[str, Any]:
        """The payload's current_conditions block, or an empty dict."""
        return current_conditions(self.coordinator.data)

    def _reading(self, key: str) -> float | None:
        """One current reading: the LOCAL radio first, the cloud second.

        Local wins because it is the same station reported over UDP without a
        WAN hop — fresher, and still answering when the internet is not. The
        cloud value is the fallback rather than the source, so a broadband
        outage ages this entity instead of emptying it. See local.py for why
        that mattered enough to build: the template entity this replaced was
        local-only, and cutting the boards over to a cloud-only entity would
        have blanked every wall panel's temperature on the first WAN blip.
        """
        local = read_local(self.hass, key)
        if local is not None:
            return local

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

        This is not the never-raise contract of LAW.md §11 arriving by the back
        door. The coordinator still raises `UpdateFailed`, the forecast still
        goes away with the cloud, and `condition` still resolves to None when
        there is no payload. Only the readings the radio can answer survive.
        """
        return super().available or local_is_answering(self.hass)

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
        # 1.0 m/s, and BOTH paths reach here in m/s: local.py converts the
        # radio's mph before it gets this far. The threshold is carried over
        # from weather_home.yaml, which expressed it as 1 mph — this is the
        # stricter of the two, so a bearing withheld there is withheld here.
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
