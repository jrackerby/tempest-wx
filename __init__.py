"""A WeatherFlow Tempest integration: current conditions and forecast.

Designed to run ALONGSIDE the HACS `tempest` component
(julianbow/TempestHomeAssistant) in local-UDP mode, not instead of it. In that
mode it forwards only the sensor platform — no weather entity, no forecast,
and none of the derived fields the Tempest app shows — and those are what this
component adds.

SCOPE. One station is the ONLY source: nothing here backstops the Tempest with
a second provider, and both halves of what it publishes — current conditions
and forecast — come from that same station. Measurements prefer the station's
local radio and fall back to its cloud; condition and forecast are cloud-only,
because the radio cannot produce them.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TempestApi
from .const import CONF_STATION_ID
from .coordinator import TempestForecastCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.WEATHER,
]

type TempestConfigEntry = ConfigEntry[TempestForecastCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TempestConfigEntry) -> bool:
    """Set up a Tempest station from a config entry."""
    api = TempestApi(async_get_clientsession(hass), entry.data[CONF_TOKEN])
    coordinator = TempestForecastCoordinator(
        hass, entry, api, entry.data[CONF_STATION_ID]
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TempestConfigEntry) -> bool:
    """Unload exactly the platforms that were forwarded."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
