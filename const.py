"""Constants for the Tempest Weather integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "tempest_wx"
MANUFACTURER: Final = "WeatherFlow"
ATTRIBUTION: Final = "Weather data from WeatherFlow Tempest"

LOGGER: Final = logging.getLogger(__package__)

CONF_STATION_ID: Final = "station_id"
CONF_STATION_NAME: Final = "station_name"

# The hardware serials behind that station, learned from the cloud station
# record and stored so the UDP listener can tell this station's broadcasts from
# a neighbour's. Absent on an entry created before the listener existed, which
# `local.py` treats as "accept everything" rather than as a failure.
CONF_DEVICE_SERIAL: Final = "device_serial"
CONF_HUB_SERIAL: Final = "hub_serial"

# Whether this entry listens to the station's own radio at all. An OPTION, not
# entry data: it is a preference about how this entry runs, changeable after
# setup, and nothing about which station the entry identifies. Defaults ON, so
# an entry created before the toggle existed keeps the behaviour it had.
CONF_LOCAL_UDP: Final = "local_udp"
DEFAULT_LOCAL_UDP: Final = True

API_ROOT: Final = "https://swd.weatherflow.com/swd/rest"

# The endpoint the Tempest app itself renders from. Enumerated off the vendor's
# own documentation index (apidocs.tempestwx.com/llms.txt), not guessed: a probe
# of invented endpoint names proves nothing about what exists.
FORECAST_URL: Final = f"{API_ROOT}/better_forecast"
STATIONS_URL: Final = f"{API_ROOT}/stations"

# THIS IS THE CADENCE IN PRACTICE, not the fallback it was written as.
# `forecast.refresh_interval` honours a payload's own
# `refresh_interval_seconds` where one is present — but station 197799's live
# response does not carry that key at all, so every poll resolves to this
# number. Kept because the field is documented and may appear, and because
# following the vendor's own answer is right when it gives one; described
# honestly because a comment claiming the cadence is server-driven would send
# the next reader looking for a value that is not in the payload.
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
