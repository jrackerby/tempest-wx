"""Shared entity bases: one for the cloud poll, one for the radio.

TWO BASES, ONE DEVICE. The station is a single piece of hardware and every
entity here belongs to it, so both bases build the same `DeviceInfo` from the
same helper. What differs is how a reading arrives: the cloud entities hang off
a `DataUpdateCoordinator` and refresh when it polls, while the local ones are
pushed to by a datagram and subscribe to a dispatcher signal instead. Giving
the local entities a coordinator anyway would mean either polling a value that
is already in memory, or a coordinator whose `_async_update_data` never runs —
a shape that reads like a bug to everyone who meets it later.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_STATION_ID, CONF_STATION_NAME, DOMAIN, MANUFACTURER
from .coordinator import TempestForecastCoordinator
from .local import TempestLocalStation, signal_update
from .udp import SOURCE_OF


def station_device(entry: ConfigEntry) -> DeviceInfo:
    """The one device every entity on this entry belongs to."""
    station_id = entry.data[CONF_STATION_ID]
    return DeviceInfo(
        identifiers={(DOMAIN, str(station_id))},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer=MANUFACTURER,
        model="Tempest",
        name=entry.data.get(CONF_STATION_NAME, f"Tempest {station_id}"),
        configuration_url=f"https://tempestwx.com/station/{station_id}/grid",
    )


class TempestEntity(CoordinatorEntity[TempestForecastCoordinator]):
    """One Tempest station's cloud-backed entities."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: TempestForecastCoordinator) -> None:
        """Bind to the station's device."""
        super().__init__(coordinator)
        self._attr_device_info = station_device(coordinator.config_entry)


class TempestLocalEntity(Entity):
    """One reading pushed in off the station's own radio.

    AVAILABILITY IS PER SOURCE, NOT PER ENTITY OR PER ENTRY. The station sends
    wind every three seconds, an observation every minute and a status line
    every few, so a single window over the whole entry would call a healthy
    station dead between observations or keep a dead one alive for as long as
    the slowest message. Each entity therefore asks whether the message that
    carries ITS reading is still current.

    Never available-with-no-value: an entity whose reading has never arrived is
    unavailable rather than `unknown`, because the two say different things —
    `unknown` claims the station reported and the value was not a number.
    """

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, station: TempestLocalStation, key: str
    ) -> None:
        """Bind one reading key to the station's device."""
        self._station = station
        self._entry = entry
        self._key = key
        self._attr_device_info = station_device(entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_STATION_ID]}_local_{key}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to the message type that carries this reading.

        Filtered at the subscriber rather than by one signal per message type:
        the dispatcher is a broadcast either way, and a signal name per type
        would mean an entity reading a DERIVED value having to know which raw
        message it came out of. `SOURCE_OF` already knows, in one place.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._async_sourced
            )
        )

    @callback
    def _async_sourced(self, message_type: str) -> None:
        """Write state only when the message that feeds this reading arrived."""
        if SOURCE_OF.get(self._key) == message_type:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Whether the radio has answered for this reading, recently enough."""
        return (
            self._station.listening
            and self._station.get(self._key) is not None
            and self._station.is_fresh(self._key)
        )

    @property
    def _value(self) -> Any:
        """The reading itself, straight off the station."""
        return self._station.get(self._key)
