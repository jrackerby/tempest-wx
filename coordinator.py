"""Cloud coordinator for the Tempest `better_forecast` endpoint.

THIS COORDINATOR RAISES `UpdateFailed`, and that is not a contradiction of the
never-raise contract a monitoring coordinator keeps. That contract governs a
coordinator reading OTHER ENTITIES: it has no remote service to lose, and a
monitor that disappears along with its subject cannot report the subject down.
This coordinator reads a remote HTTP API. When that API is unreachable the
entities genuinely have no value, so the quality scale's `entity-unavailable`
rule governs here on its own terms — a rule is applied where it governs, not
where it merely sounds relevant.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TempestApi, TempestApiError, TempestAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, LOGGER, MIN_SCAN_INTERVAL
from .forecast import refresh_interval


class TempestForecastCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls `better_forecast` at the cadence the payload itself asks for."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: TempestApi,
        station_id: int,
    ) -> None:
        """Set up the coordinator against one station."""
        self.api = api
        self.station_id = station_id
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{station_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch one forecast payload."""
        try:
            payload = await self.api.async_get_forecast(self.station_id)
        except TempestAuthError as err:
            # Re-auth, not retry: nothing about waiting fixes a rejected token.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TempestApiError as err:
            raise UpdateFailed(str(err)) from err

        # Re-pace to whatever the payload says, so a vendor that widens its own
        # refresh window is followed rather than hammered at a rate this file
        # picked. Only ever assigned when it actually changes, or every single
        # poll rewrites the coordinator's timer for no reason.
        wanted = timedelta(
            seconds=refresh_interval(
                payload, DEFAULT_SCAN_INTERVAL, floor=MIN_SCAN_INTERVAL
            )
        )
        if wanted != self.update_interval:
            LOGGER.debug(
                "Station %s asked for a %s refresh interval; was %s",
                self.station_id,
                wanted,
                self.update_interval,
            )
            self.update_interval = wanted

        return payload
