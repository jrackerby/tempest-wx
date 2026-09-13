"""Cloud coordinator for the Tempest `better_forecast` endpoint.

THIS COORDINATOR RAISES `UpdateFailed`, and that is not a contradiction of the
never-raise contract a monitoring coordinator keeps. That contract governs a
coordinator reading OTHER ENTITIES: it has no remote service to lose, and a
monitor that disappears along with its subject cannot report the subject down.
This coordinator reads a remote HTTP API. When that API is unreachable the
entities genuinely have no value, so the quality scale's `entity-unavailable`
rule governs here on its own terms — a rule is applied where it governs, not
where it merely sounds relevant.

THE RATE LIMIT IS ENFORCED HERE, ON EVERY PATH IN. Three things can make this
coordinator call the API, and each is held to `MIN_SCAN_INTERVAL`:

  * The SCHEDULE — paced by the payload's own `refresh_interval_seconds`,
    floored at the minimum (`forecast.refresh_interval`).
  * A REQUEST — `homeassistant.update_entity`, an entity being added, anything
    that calls `async_request_refresh`. Core debounces that path at 10 seconds
    by default, which is six calls a minute from one automation that loops.
    The debouncer here cools down for the full floor instead: the first request
    goes now, everything inside the window coalesces into one poll at its end.
  * A RETRY — after a failure, the next attempt is placed by `pacing.py`, never
    by this file's own guess and never below the floor. What the vendor refused
    is retried later than the cadence (honouring `Retry-After` where given);
    what never answered is retried sooner, from the floor, never past the
    cadence. The two shapes and the reasoning are in that module's docstring.

Core honours `UpdateFailed(retry_after=...)` for exactly the next scheduled
refresh and then falls back to `update_interval`, so a single bad poll never
re-paces the healthy schedule — only the consecutive-failure count carries
across attempts, and a success resets it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TempestApi, TempestApiError, TempestAuthError, TempestRateLimitError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, LOGGER, MIN_SCAN_INTERVAL
from .forecast import refresh_interval
from .pacing import throttled_retry, unreachable_retry


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
        # Consecutive failed polls, this one included while it is being paced.
        # Reset on the first payload that lands. Read by the retry policies
        # only; never by an entity.
        self.consecutive_failures = 0
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{station_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            request_refresh_debouncer=Debouncer(
                hass,
                LOGGER,
                cooldown=MIN_SCAN_INTERVAL,
                immediate=True,
            ),
        )

    @property
    def _interval_seconds(self) -> float:
        """The healthy cadence, as a number the policies can double."""
        interval = self.update_interval
        if interval is None:
            return DEFAULT_SCAN_INTERVAL
        return interval.total_seconds()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch one forecast payload, or say when to try again."""
        try:
            payload = await self.api.async_get_forecast(self.station_id)
        except TempestAuthError as err:
            # Re-auth, not retry: nothing about waiting fixes a rejected token.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TempestRateLimitError as err:
            self.consecutive_failures += 1
            wait = throttled_retry(
                self.consecutive_failures,
                self._interval_seconds,
                MIN_SCAN_INTERVAL,
                retry_after=err.retry_after,
            )
            LOGGER.debug(
                "Station %s rate limited (%s); attempt %d, next in %.0fs",
                self.station_id,
                err,
                self.consecutive_failures,
                wait,
            )
            raise UpdateFailed(str(err), retry_after=wait) from err
        except TempestApiError as err:
            self.consecutive_failures += 1
            wait = unreachable_retry(
                self.consecutive_failures, self._interval_seconds, MIN_SCAN_INTERVAL
            )
            LOGGER.debug(
                "Station %s unreachable (%s); attempt %d, next in %.0fs",
                self.station_id,
                err,
                self.consecutive_failures,
                wait,
            )
            raise UpdateFailed(str(err), retry_after=wait) from err

        self.consecutive_failures = 0

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
