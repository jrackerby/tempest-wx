"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_STATION_NAME, DOMAIN, MANUFACTURER
from .coordinator import TempestForecastCoordinator


class TempestEntity(CoordinatorEntity[TempestForecastCoordinator]):
    """One Tempest station, as a service device."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: TempestForecastCoordinator) -> None:
        """Bind to the station's device."""
        super().__init__(coordinator)
        station_id = coordinator.station_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(station_id))},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer=MANUFACTURER,
            model="Tempest",
            name=coordinator.config_entry.data.get(
                CONF_STATION_NAME, f"Tempest {station_id}"
            ),
            configuration_url=f"https://tempestwx.com/station/{station_id}/grid",
        )
