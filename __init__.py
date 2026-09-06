"""The estate's own WeatherFlow Tempest integration.

Replaces the HACS `tempest` component (julianbow/TempestHomeAssistant), which
runs here in local-UDP mode and therefore forwards only the sensor platform —
no weather entity, no forecast, and none of the derived fields the Tempest app
shows. See GH-587 for the verified defect list.

SCOPE, and what it does NOT change. LAW.md §3 carries a standing ruling with
two halves. The first — the Tempest is the ONLY source of current conditions,
nothing backstops it, no provider is reinstated to fill a gap — is untouched
and this component keeps it: every reading here comes from the same Tempest
station. The second half said forecast does not come through Home Assistant at
all and is pulled from weather.com by the surface that renders it. Joel
reversed that half directly, and this is the reversal, labelled as one
(LAW.md §14). It is a narrower estate than before, not a wider one: the
forecast now comes from the same station as everything else, and weather.com
leaves the picture.
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
