"""Constants for the estate's Tempest integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "tempest_wx"
MANUFACTURER: Final = "WeatherFlow"
ATTRIBUTION: Final = "Weather data from WeatherFlow Tempest"

LOGGER: Final = logging.getLogger(__package__)

CONF_STATION_ID: Final = "station_id"
CONF_STATION_NAME: Final = "station_name"

API_ROOT: Final = "https://swd.weatherflow.com/swd/rest"

# The endpoint the Tempest app itself renders from. Enumerated off the vendor's
# own documentation index (apidocs.tempestwx.com/llms.txt), not guessed: a probe
# of invented endpoint names proves nothing about what exists.
FORECAST_URL: Final = f"{API_ROOT}/better_forecast"
STATIONS_URL: Final = f"{API_ROOT}/stations"

# Fallback only. The live cadence comes from the payload's own
# `refresh_interval_seconds` — see forecast.refresh_interval.
DEFAULT_SCAN_INTERVAL: Final = 300
MIN_SCAN_INTERVAL: Final = 60

# Ask for metric and let Home Assistant convert. Requesting the user's display
# units instead would bake a unit choice into the wire format and silently
# change every stored value the day that choice changes.
FORECAST_UNITS: Final = {
    "units_temp": "c",
    "units_wind": "mps",
    "units_pressure": "mb",
    "units_precip": "mm",
    "units_distance": "km",
}
