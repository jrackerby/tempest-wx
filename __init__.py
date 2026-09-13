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

CLOUD-ONLY IS A CONFIGURED MODE, NOT A DEGRADED ONE. The listener can be
switched off per entry (`local_udp`, default on), because UDP 50222 is exclusive
in practice: with no way to decline it, which of two listeners holds the port is
decided by Home Assistant's setup order rather than by the user. Switched off,
the cloud half publishes exactly as it always did and the local entities are
unavailable — the same shape a refused bind already produces. That sameness is
deliberate: one code path, one fact (`station.listening`) deciding it, and no
second definition of what cloud-only means.
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
from .local import TempestLocalStation, local_udp_enabled
from .udp import UDP_PORT, station_serials

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.WEATHER,
]


@dataclass
class TempestRuntimeData:
    """The two sources one station has: its cloud poll and its radio.

    `local_udp` records what this setup ACTED ON, which is not the same fact as
    what the options currently say — the update listener below is the one reader
    that needs both.
    """

    coordinator: TempestForecastCoordinator
    station: TempestLocalStation
    local_udp: bool


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
    local_udp = local_udp_enabled(entry)
    if local_udp:
        await station.async_start()
        entry.async_on_unload(station.async_stop)
    else:
        # DEBUG, not a warning. Nobody has to act on it: this is the state the
        # user asked for, and a warning for a setting working as configured is
        # how a log stops being read. The local entities go unavailable, exactly
        # as they do when the bind is refused, and `station.listening` is the one
        # fact that decides it either way.
        LOGGER.debug(
            "Local UDP listening is switched off for this entry; the station's "
            "own radio on port %s is not read and this entry runs cloud-only",
            UDP_PORT,
        )

    entry.runtime_data = TempestRuntimeData(
        coordinator=coordinator, station=station, local_udp=local_udp
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # LAST, AND THAT ORDER IS LOAD-BEARING. `_async_learn_serials` above writes
    # `entry.data` through `async_update_entry`, which fires every registered
    # update listener as a TASK — and it fires them only when the entry really
    # changed, which that top-up does: it adds two keys. Registered any earlier,
    # this entry's own listener would therefore be scheduled mid-setup, run at
    # the next await, read a `runtime_data` that is not assigned yet, and ask for
    # a reload of an entry still setting up — over a write setup made itself.
    # Registering here is what makes the top-up unable to reach it.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(
    hass: HomeAssistant, entry: TempestConfigEntry
) -> None:
    """Reload the entry when the toggle MOVED, and not merely when it was written.

    An update listener is told the entry changed, never what changed, so a
    blanket reload here would restart this entry for any write at all —
    including the serial top-up and a reauth, which reloads itself anyway. The
    socket's state is the thing a reload exists to change, so comparing what
    setup acted on against what the options now say is the discriminator, and it
    is exact: `local_udp` in runtime data is the value the running listener was
    started (or not started) from.
    """
    if entry.runtime_data.local_udp == local_udp_enabled(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)


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
