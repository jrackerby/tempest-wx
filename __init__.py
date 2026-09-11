"""A WeatherFlow Tempest integration: current conditions and forecast.

SELF-CONTAINED AS OF 0.2.0. This component reads the station's LOCAL UDP
broadcast itself and publishes those readings as its own entities, so it no
longer needs — and no longer reads anything from — the HACS `tempest`
integration it was originally built to sit beside. That coupling was nine
hard-coded `sensor.tempest_*` entity ids in `local.py`; it is gone, and with it
the unit conversions it forced.

SCOPE. One station is the ONLY source: nothing here backstops the Tempest with
a second provider, and both halves of what it publishes — current conditions
and forecast — come from that same station. Measurements come from the
station's local radio and fall back to its cloud; condition and forecast are
cloud-only, because the radio cannot produce them.

WHAT STILL NEEDS THE CLOUD, AND WHY SETUP DOES. The token, the forecast, the
condition string and the derived cloud sensors all need the WAN, and the first
refresh gates setup, so a Home Assistant started while the internet is down
retries this entry rather than loading it half-alive. That is deliberate: an
entry that loaded with no condition and no forecast would be a weather entity
in name only, and the retry is what makes the eventual load a complete one.
Once loaded, a WAN outage AGES this entry instead of emptying it — the radio
keeps every measurement current while the forecast goes stale.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TempestApi, TempestApiError
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_HUB_SERIAL,
    CONF_STATION_ID,
    LOGGER,
)
from .coordinator import TempestForecastCoordinator
from .local import TempestLocalStation
from .udp import station_serials

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.WEATHER,
]


@dataclass
class TempestRuntimeData:
    """The two sources one station has: its cloud poll and its radio."""

    coordinator: TempestForecastCoordinator
    station: TempestLocalStation


type TempestConfigEntry = ConfigEntry[TempestRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TempestConfigEntry) -> bool:
    """Set up a Tempest station from a config entry."""
    api = TempestApi(async_get_clientsession(hass), entry.data[CONF_TOKEN])
    coordinator = TempestForecastCoordinator(
        hass, entry, api, entry.data[CONF_STATION_ID]
    )
    await coordinator.async_config_entry_first_refresh()

    await _async_learn_serials(hass, entry, api)

    station = TempestLocalStation(
        hass,
        entry.entry_id,
        device_serial=entry.data.get(CONF_DEVICE_SERIAL),
        hub_serial=entry.data.get(CONF_HUB_SERIAL),
    )
    await station.async_start()
    entry.async_on_unload(station.async_stop)

    entry.runtime_data = TempestRuntimeData(coordinator=coordinator, station=station)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TempestConfigEntry) -> bool:
    """Unload exactly the platforms that were forwarded.

    The socket closes through `entry.async_on_unload`, so it goes whether the
    platforms unload cleanly or not — a listener left holding port 50222 after
    a failed unload would refuse the next setup's bind and present as a
    hardware problem.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_learn_serials(
    hass: HomeAssistant, entry: TempestConfigEntry, api: TempestApi
) -> None:
    """Fill in the station's hardware serials, once, for entries created without them.

    NOT A MIGRATION HOOK AND NOT A VERSION BUMP. Nothing about an entry's shape
    changed — a key was added — so the entry is simply topped up in place the
    first time it is set up by a version that wants it. An entry that cannot
    reach the API here is left exactly as it was and tries again on the next
    reload; the listener runs unfiltered in the meantime and says so.
    """
    if CONF_DEVICE_SERIAL in entry.data or CONF_HUB_SERIAL in entry.data:
        return

    station_id = entry.data[CONF_STATION_ID]
    try:
        stations = await api.async_get_stations()
    except TempestApiError as err:
        LOGGER.debug("Could not read the station list to learn serials: %s", err)
        return

    for station in stations:
        if station.get("station_id") != station_id:
            continue
        device_serial, hub_serial = station_serials(station)
        if device_serial is None and hub_serial is None:
            # Stored as None rather than left absent, so this lookup is not
            # repeated on every reload for an answer that will not change.
            LOGGER.debug("Station %s lists no ST or HB device", station_id)
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_DEVICE_SERIAL: device_serial,
                CONF_HUB_SERIAL: hub_serial,
            },
        )
        LOGGER.debug(
            "Learned serials for station %s: device=%s hub=%s",
            station_id,
            device_serial,
            hub_serial,
        )
        return
