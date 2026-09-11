"""Config flow for the Tempest Weather integration.

THE SETUP CHECK EXERCISES THE CHANNEL THAT WILL ACTUALLY BE USED. A check that
probes a different channel than the one the integration polls
certifies nothing, and it certifies nothing GREEN, which is worse than no
check at all. So this flow does not merely list stations to prove the token
parses — it calls `better_forecast` against the station being configured, the
same endpoint, with the same token, that the coordinator will poll a minute
later. An entry is only created once that specific call has come back with a
forecast in it.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TempestApi, TempestApiError, TempestAuthError
from .const import CONF_STATION_ID, CONF_STATION_NAME, DOMAIN, LOGGER
from .forecast import daily_forecast

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class TempestConfigFlow(ConfigFlow, domain=DOMAIN):
    """Token, then station, then a real forecast read before anything is saved."""

    VERSION = 1

    def __init__(self) -> None:
        """Carry the token and the station list between the two steps."""
        self._token: str = ""
        self._stations: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a personal access token and list what it can see."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            api = TempestApi(async_get_clientsession(self.hass), token)
            try:
                stations = await api.async_get_stations()
            except TempestAuthError:
                errors["base"] = "invalid_auth"
            except TempestApiError as err:
                LOGGER.debug("Station list failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not stations:
                    errors["base"] = "no_stations"
                else:
                    self._token = token
                    self._stations = {
                        str(station["station_id"]): str(
                            station.get("name") or station["station_id"]
                        )
                        for station in stations
                        if station.get("station_id") is not None
                    }
                    return await self.async_step_station()

        return self.async_show_form(
            step_id="user", data_schema=TOKEN_SCHEMA, errors=errors
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a station, then prove the forecast endpoint answers for it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            station_id = int(user_input[CONF_STATION_ID])
            await self.async_set_unique_id(f"{DOMAIN}_{station_id}")
            self._abort_if_unique_id_configured()

            api = TempestApi(async_get_clientsession(self.hass), self._token)
            try:
                payload = await api.async_get_forecast(station_id)
            except TempestAuthError:
                errors["base"] = "invalid_auth"
            except TempestApiError as err:
                LOGGER.debug("Forecast probe failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                # A 200 with no usable rows is not a working setup. Saying so
                # here is the whole point of probing the real endpoint: the
                # station list already answered, so without this the entry
                # would be created green over a forecast that is not there.
                if not daily_forecast(payload):
                    errors["base"] = "no_forecast"
                else:
                    name = self._stations.get(str(station_id), str(station_id))
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_TOKEN: self._token,
                            CONF_STATION_ID: station_id,
                            CONF_STATION_NAME: name,
                        },
                    )

        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=label)
                                for key, label in self._stations.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """A rejected token needs a new one, not a retry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement token and re-probe the configured station."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            api = TempestApi(async_get_clientsession(self.hass), token)
            try:
                await api.async_get_forecast(entry.data[CONF_STATION_ID])
            except TempestAuthError:
                errors["base"] = "invalid_auth"
            except TempestApiError as err:
                LOGGER.debug("Reauth probe failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )
