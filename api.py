"""Thin async client for the Tempest REST API.

Deliberately NOT `weatherflow4py`. That library is where two of the defects
this component exists to fix actually live — the `Icon.ha_icon` map that
disagrees with the calling integration's own map on 6 of 19 icons, and the
unguarded `self.icon.ha_icon` that turns one iconless hourly row into zero
forecast. Depending on it would mean re-inheriting both and then working
around them. The endpoint returns plain JSON; parsing it is `forecast.py`,
which is pure and tested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

from .const import FORECAST_URL, FORECAST_UNITS, STATIONS_URL
from .pacing import parse_retry_after

REQUEST_TIMEOUT = ClientTimeout(total=30)


class TempestApiError(Exception):
    """The API could not be read."""


class TempestAuthError(TempestApiError):
    """The token was rejected. Distinct because it needs a human, not a retry."""


class TempestRateLimitError(TempestApiError):
    """The API answered and said stop. Distinct because it needs PATIENCE.

    Carries the wait the vendor asked for, in seconds, where the response named
    one. A retry at the normal cadence is exactly what a rate limit punishes,
    so the coordinator paces this one differently from a connection that
    simply never answered.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Keep the vendor's own wait alongside the message."""
        super().__init__(message)
        self.retry_after = retry_after


class TempestApi:
    """Reads the two endpoints this component needs."""

    def __init__(self, session: ClientSession, token: str) -> None:
        """Hold the shared session and the personal access token."""
        self._session = session
        self._token = token

    async def async_get_stations(self) -> list[dict[str, Any]]:
        """Return the stations this token can see."""
        payload = await self._get(STATIONS_URL, {})
        stations = payload.get("stations")
        if not isinstance(stations, list):
            return []
        return [station for station in stations if isinstance(station, dict)]

    async def async_get_forecast(self, station_id: int) -> dict[str, Any]:
        """Return the raw `better_forecast` payload for one station."""
        return await self._get(
            FORECAST_URL, {"station_id": str(station_id), **FORECAST_UNITS}
        )

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        """GET and decode, mapping transport failures onto our two errors."""
        try:
            # `async with` so the response is released even on a raise. Without
            # it an auth failure leaks the connection back into the shared HA
            # session on every retry.
            async with self._session.get(
                url,
                params={**params, "token": self._token},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                # 401 and 403 both mean the token will not work until somebody
                # changes it. Raising the same error as a timeout would put the
                # entry into an endless retry that can never succeed.
                if response.status in (401, 403):
                    raise TempestAuthError(f"token rejected ({response.status})")
                # 429 is a refusal whatever it carries. 503 is one only when it
                # names a wait — a bare 503 is a server that is down, which is
                # the UNREACHABLE shape, and paced as such by the caller.
                if response.status in (429, 503):
                    retry_after = parse_retry_after(
                        response.headers.get("Retry-After"), datetime.now(UTC)
                    )
                    if response.status == 429 or retry_after is not None:
                        raise TempestRateLimitError(
                            f"HTTP {response.status} from {url}: rate limited",
                            retry_after=retry_after,
                        )
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (TempestAuthError, TempestRateLimitError):
            raise
        except ClientResponseError as err:
            raise TempestApiError(f"HTTP {err.status} from {url}") from err
        except (ClientError, TimeoutError) as err:
            raise TempestApiError(f"could not reach {url}: {err}") from err

        if not isinstance(payload, dict):
            raise TempestApiError(f"{url} did not return a JSON object")

        # better_forecast reports its own failures in-band with HTTP 200 — a
        # `status_code` other than 0 alongside a payload that otherwise looks
        # fine. Reading the body as success because the transport succeeded is
        # how a caller ends up publishing an empty forecast as a real one.
        status = payload.get("status")
        if isinstance(status, dict):
            code = status.get("status_code")
            if isinstance(code, int) and code != 0:
                message = status.get("status_message", "unknown error")
                if code in (1, 2):
                    raise TempestAuthError(f"{message} (status_code {code})")
                raise TempestApiError(f"{message} (status_code {code})")

        return payload
