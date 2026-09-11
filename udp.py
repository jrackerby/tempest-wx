"""Pure parsing of the Tempest's own UDP broadcast, and what is derived from it.

THIS MODULE IMPORTS NOTHING FROM `homeassistant`, deliberately, the same way
`forecast.py` does. A datagram is bytes on a wire and a derived temperature is
arithmetic; neither needs a running Home Assistant to be tested, and a suite
that cannot run without one is a suite nobody runs before pushing.

WHY IT EXISTS. Until now this component read the LOCAL half of its weather
entity out of a DIFFERENT integration's entity ids — nine `sensor.tempest_*`
lookups declared in `local.py`. That coupling made the two components a pair:
removing the other one emptied this one's local path and dropped every reading
back to the cloud. The station already broadcasts everything those entities
carry, unencrypted, to the whole LAN, once a minute. Reading it directly is
fewer moving parts than reading somebody else's rendering of it, and it is
what lets this integration stand alone.

THE WIRE FORMAT IS THE VENDOR'S, NOT A GUESS. Field ORDER inside `obs_st` is
the entire contract — the payload is a bare array with no keys — so the index
table below is transcribed from WeatherFlow's published UDP reference and the
names are checked against the units the station actually emits. An index read
one place out is not a parse error; it is a pressure published as a
temperature, which reads on the glass as a broken station rather than a broken
parser.

UNITS ARRIVE METRIC AND STAY METRIC. The radio emits °C, millibars, m/s and
millimetres — the same natives this component's weather entity declares — so
unlike the entity-id path this replaces there is no conversion step here and
no opportunity for a double conversion. Home Assistant converts once, for
display, at the edge.
"""

from __future__ import annotations

import errno
import json
import math
from typing import Any, Final

# The station broadcasts to the LAN broadcast address on this port. It is the
# vendor's fixed number, not a preference: nothing configures it on the hub.
UDP_PORT: Final = 50222

# --------------------------------------------------------------------------
# obs_st — the once-a-minute observation, as a bare array.
#
# ORDER IS THE CONTRACT. Written out rather than indexed inline so that the
# table can be read against the vendor reference in one pass; an inline
# `obs[11]` three modules away cannot be.
# --------------------------------------------------------------------------
OBS_ST_FIELDS: Final[tuple[str | None, ...]] = (
    "obs_time",  # 0  epoch seconds
    "wind_lull",  # 1  m/s, minimum 3-second sample
    "wind_avg",  # 2  m/s, averaged over the report interval
    "wind_gust",  # 3  m/s, maximum 3-second sample
    "wind_direction_avg",  # 4  degrees
    None,  # 5  wind sample interval, seconds — nothing consumes it
    "station_pressure",  # 6  millibars, AT THE STATION, not sea level
    "air_temperature",  # 7  °C
    "relative_humidity",  # 8  %
    "illuminance",  # 9  lux
    "uv",  # 10 index
    "solar_radiation",  # 11 W/m²
    "precip_accum_last_interval",  # 12 mm over the report interval
    "precipitation_type",  # 13 0 none, 1 rain, 2 hail, 3 rain+hail
    "lightning_avg_distance",  # 14 km
    "lightning_count",  # 15 strikes in the report interval
    "battery_voltage",  # 16 volts
    "report_interval",  # 17 minutes
)

# The vendor's code, spelled. `rain_hail` is documented as experimental and is
# carried through rather than folded into `rain`, because a station that says
# hail has said something a rain total cannot.
PRECIPITATION_TYPES: Final[dict[int, str]] = {
    0: "none",
    1: "rain",
    2: "hail",
    3: "rain_hail",
}

# device_status `sensor_status` is a bit field. Only the FAILURE bits are
# listed as faults: `lightning noise` and `lightning disturber` are the
# lightning detector reporting that it rejected an interference source, which
# is the detector working, not the detector broken.
SENSOR_FAULT_BITS: Final[dict[int, str]] = {
    0x00000001: "lightning_failed",
    0x00000008: "pressure_failed",
    0x00000010: "temperature_failed",
    0x00000020: "humidity_failed",
    0x00000040: "wind_failed",
    0x00000080: "precip_failed",
    0x00000100: "light_uv_failed",
    0x00008000: "power_booster_depleted",
}

# Which message carries each reading. Availability is decided per SOURCE, not
# per entity: `rapid_wind` arrives every three seconds and `obs_st` once a
# minute, so one staleness window over both would either call a live station
# dead or keep a dead one alive for a minute after it stopped.
SOURCE_OF: Final[dict[str, str]] = {}


def _source(message_type: str, *keys: str) -> None:
    """Record which message type answers each reading."""
    for key in keys:
        SOURCE_OF[key] = message_type


_source(
    "obs_st",
    *(field for field in OBS_ST_FIELDS if field),
    "dew_point",
    "feels_like",
    "wet_bulb_temperature",
    "air_density",
    "precipitation_intensity",
)
_source("rapid_wind", "wind_speed", "wind_direction")
_source(
    "device_status",
    "device_rssi",
    "device_boot_time",
    "device_firmware",
    "sensor_faults",
)
_source("hub_status", "hub_rssi", "hub_boot_time", "hub_firmware")

# Message types this component reads. Anything else on the port — `obs_air` and
# `obs_sky` from the older two-piece hardware, `evt_precip`, `evt_strike`, the
# hub's own `rapid_wind` relays — is ignored by NAME rather than by falling off
# the end of a chain of ifs, so an unknown type is distinguishable from a type
# we chose not to read.
HANDLED: Final[frozenset[str]] = frozenset(
    {"obs_st", "rapid_wind", "device_status", "hub_status"}
)
IGNORED: Final[frozenset[str]] = frozenset(
    {"obs_air", "obs_sky", "evt_precip", "evt_strike", "light_debug"}
)


# What a failed bind actually means, kept here rather than at the socket so the
# DIAGNOSIS is testable without one. It shipped wrong once: every bind failure
# was reported as "Home Assistant is not on the hub's broadcast domain", which
# is true for a container on a bridge network and completely misleading for the
# case that actually happened — another integration already holding the port.
# A log line that names the wrong cause is worse than one that says nothing,
# because it sends the next reader to the network.
PORT_IN_USE_ADVICE: Final = (
    "another process already holds it and is not sharing it. Only one listener "
    "can bind this port unless BOTH opt into port sharing, so whichever starts "
    "first wins and the other runs cloud-only. If you also run the HACS "
    "`tempest` integration in local-UDP mode, remove it — it is what this "
    "integration replaces"
)
NOT_ON_LAN_ADVICE: Final = (
    "Home Assistant must be on the same broadcast domain as the hub for the "
    "local radio to reach it — a container on a bridge network is not"
)


def bind_failure_advice(error_number: int | None) -> str:
    """Why the socket could not be opened, in the words that fit the cause."""
    return (
        PORT_IN_USE_ADVICE
        if error_number == errno.EADDRINUSE
        else NOT_ON_LAN_ADVICE
    )


def _num(value: Any) -> float | None:
    """A finite number, or None.

    None means the station did not say. It is never a zero: `ok at zero` and
    `could not read` are different values at the source, and collapsing them
    publishes a calm, cold, dry day every time the radio goes quiet. `bool` is
    rejected explicitly because it is an `int` in Python and a True temperature
    is not 1 °C.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def decode(raw: bytes) -> dict[str, Any] | None:
    """One datagram to a JSON object, or None.

    Anything that is not a JSON object is not ours. The port carries whatever
    else is broadcasting on the LAN, so a decoder that raised would take the
    listener down on the first stray packet.
    """
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return message if isinstance(message, dict) else None


def parse(message: Any) -> tuple[str, dict[str, Any]] | None:
    """`(message type, readings)` for a message we read, else None.

    The readings dict is keyed the way the entities are, already in this
    component's native units, and carries only what the message actually
    answered — a key absent means the station did not report it, which the
    caller must not turn into a zero.
    """
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in HANDLED:
        return None

    if message_type == "obs_st":
        readings = _obs_st(message)
    elif message_type == "rapid_wind":
        readings = _rapid_wind(message)
    elif message_type == "device_status":
        readings = _device_status(message)
    else:
        readings = _hub_status(message)

    return None if readings is None else (message_type, readings)


def _obs_st(message: dict[str, Any]) -> dict[str, Any] | None:
    """The observation array, by position, plus everything derived from it."""
    obs = message.get("obs")
    if not isinstance(obs, list) or not obs:
        return None
    row = obs[-1]  # A batch is oldest-first; the newest row is the state.
    if not isinstance(row, list):
        return None

    readings: dict[str, Any] = {}
    for index, name in enumerate(OBS_ST_FIELDS):
        if name is None or index >= len(row):
            continue
        value = _num(row[index])
        if value is None:
            continue
        readings[name] = value

    # Type is a code, not a quantity. An unlisted code is withheld rather than
    # published raw: the entity declares its options and HA rejects anything
    # else on every render.
    code = readings.pop("precipitation_type", None)
    if code is not None:
        spelled = PRECIPITATION_TYPES.get(int(code))
        if spelled is not None:
            readings["precipitation_type"] = spelled

    readings.update(_derived(readings))
    # An observation that yielded nothing is not an observation. Returning it
    # anyway would mark the source as heard, so a station sending empty arrays
    # would read as answering while publishing no reading at all.
    return readings or None


def _derived(readings: dict[str, Any]) -> dict[str, Any]:
    """What the radio does not send but the Tempest app shows.

    Every one of these is arithmetic over fields the observation already
    carries. They are computed here rather than read from the cloud so they
    survive a WAN outage along with the measurements they come from — a
    dew point that disappears when the internet does is a dew point published
    from the wrong place.
    """
    out: dict[str, Any] = {}
    temperature = readings.get("air_temperature")
    humidity = readings.get("relative_humidity")
    pressure = readings.get("station_pressure")
    wind = readings.get("wind_avg")

    if temperature is not None and humidity is not None:
        out["dew_point"] = dew_point(temperature, humidity)
        out["wet_bulb_temperature"] = wet_bulb(temperature, humidity)
        out["feels_like"] = apparent_temperature(temperature, humidity, wind)
        if pressure is not None:
            out["air_density"] = air_density(temperature, humidity, pressure)

    # Accumulation over the report interval, expressed as a rate. The interval
    # is READ, never assumed to be one minute: a station reconfigured to report
    # every five would otherwise publish an intensity five times the truth,
    # and the number would look plausible the whole time.
    accumulation = readings.get("precip_accum_last_interval")
    interval = readings.get("report_interval")
    if accumulation is not None and interval:
        out["precipitation_intensity"] = accumulation * 60.0 / interval

    return {key: value for key, value in out.items() if value is not None}


def _rapid_wind(message: dict[str, Any]) -> dict[str, Any] | None:
    """The three-second wind sample: `[epoch, speed m/s, direction degrees]`."""
    ob = message.get("ob")
    if not isinstance(ob, list) or len(ob) < 3:
        return None
    readings: dict[str, Any] = {}
    speed = _num(ob[1])
    direction = _num(ob[2])
    if speed is not None:
        readings["wind_speed"] = speed
    if direction is not None:
        readings["wind_direction"] = direction
    return readings or None


def _device_status(message: dict[str, Any]) -> dict[str, Any] | None:
    """The station's own health line."""
    readings: dict[str, Any] = {}

    rssi = _num(message.get("rssi"))
    if rssi is not None:
        readings["device_rssi"] = rssi

    boot = _boot_time(message)
    if boot is not None:
        readings["device_boot_time"] = boot

    firmware = message.get("firmware_revision")
    if firmware is not None:
        readings["device_firmware"] = str(firmware)

    status = _num(message.get("sensor_status"))
    if status is not None:
        readings["sensor_faults"] = sensor_faults(int(status))

    return readings or None


def _hub_status(message: dict[str, Any]) -> dict[str, Any] | None:
    """The hub's own health line."""
    readings: dict[str, Any] = {}

    rssi = _num(message.get("rssi"))
    if rssi is not None:
        readings["hub_rssi"] = rssi

    boot = _boot_time(message)
    if boot is not None:
        readings["hub_boot_time"] = boot

    firmware = message.get("firmware_revision")
    if firmware is not None:
        readings["hub_firmware"] = str(firmware)

    return readings or None


def _boot_time(message: dict[str, Any]) -> float | None:
    """When the device came up, as an epoch — NOT how long it has been up.

    An uptime published as a duration is a number that changes every time it is
    read, so a timestamp sensor rendered from `now() - uptime` jitters by a
    second on every message and writes a new state for a device that has not
    moved. Subtracting inside the message, from the device's OWN clock reading
    in that same message, gives a constant that only changes when the device
    actually reboots — which is the event anybody reading this wants to see.
    """
    timestamp = _num(message.get("timestamp"))
    uptime = _num(message.get("uptime"))
    if timestamp is None or uptime is None:
        return None
    return timestamp - uptime


def station_serials(station: Any) -> tuple[str | None, str | None]:
    """`(device serial, hub serial)` out of one cloud station record.

    A CLOUD FIELD READ FOR THE RADIO'S SAKE, which is why it lives here rather
    than beside the rest of the API parsing: the serials are the only way a
    datagram can be attributed, because the broadcast carries no station id at
    all. A LAN with two Tempests puts both stations' observations on one port,
    and without this filter the second entry configured would publish the first
    station's weather under the second station's name — plausible, wrong, and
    invisible until somebody compares them.

    `ST` is the Tempest itself; `HB` the hub. `AR` and `SK` are the older
    two-piece Air and Sky, which broadcast message types this component does
    not read, so they are not matched here either.
    """
    if not isinstance(station, dict):
        return None, None
    devices = station.get("devices")
    if not isinstance(devices, list):
        return None, None

    device_serial: str | None = None
    hub_serial: str | None = None
    for device in devices:
        if not isinstance(device, dict):
            continue
        serial = device.get("serial_number")
        if not isinstance(serial, str):
            continue
        kind = device.get("device_type")
        if kind == "ST" and device_serial is None:
            device_serial = serial
        elif kind == "HB" and hub_serial is None:
            hub_serial = serial
    return device_serial, hub_serial


def sensor_faults(status: int) -> list[str]:
    """The failure bits set in `sensor_status`, spelled, in bit order.

    An empty list is a station reporting that every sensor is fine, and is a
    different thing from no `device_status` message at all. The caller keeps
    that difference: absent means the station has not said.
    """
    return [name for bit, name in SENSOR_FAULT_BITS.items() if status & bit]


# --------------------------------------------------------------------------
# Derived quantities.
#
# Each is a published formula, named, with its domain of validity stated. The
# alternative — a constant retyped from memory into a one-line expression — is
# how a reading ends up plausible and wrong, which is the hardest kind to find
# because nothing about it looks broken.
# --------------------------------------------------------------------------

# Magnus-Tetens coefficients over water, the WMO-recommended pair.
_MAGNUS_A: Final = 17.625
_MAGNUS_B: Final = 243.04


def dew_point(temperature: float, humidity: float) -> float | None:
    """Dew point in °C by Magnus-Tetens. None below 1 % RH.

    The formula takes the logarithm of the humidity fraction, so zero humidity
    is not a cold dew point, it is undefined — a station reporting 0 % has a
    broken hygrometer, and publishing minus-infinity-ish nonsense for it would
    look like weather.
    """
    if humidity < 1.0 or humidity > 100.0:
        return None
    gamma = math.log(humidity / 100.0) + (
        _MAGNUS_A * temperature / (_MAGNUS_B + temperature)
    )
    return _MAGNUS_B * gamma / (_MAGNUS_A - gamma)


def wet_bulb(temperature: float, humidity: float) -> float | None:
    """Wet-bulb temperature in °C, by Stull (2011). None outside its domain.

    Stull's fit is an empirical regression against a psychrometric solver, good
    to about ±0.3 °C between roughly -20 °C and 50 °C at 5-99 % RH and at
    sea-level pressure. Outside that band it does not degrade gracefully, so
    this returns None there rather than a number carrying an error nobody can
    see. THIS IS NOT WBGT: `sensor.wet_bulb_globe_temperature`, next door, is
    the heat-stress index the cloud reports and includes a radiant term this
    has no way to know.
    """
    if not -20.0 <= temperature <= 50.0 or not 5.0 <= humidity <= 99.0:
        return None
    return (
        temperature * math.atan(0.151977 * (humidity + 8.313659) ** 0.5)
        + math.atan(temperature + humidity)
        - math.atan(humidity - 1.676331)
        + 0.00391838 * humidity**1.5 * math.atan(0.023101 * humidity)
        - 4.686035
    )


def air_density(
    temperature: float, humidity: float, station_pressure: float
) -> float | None:
    """Density of the air in kg/m³, MOIST, from the ideal gas law.

    DELIBERATELY NOT THE DRY-AIR FIGURE. The integration this replaces divides
    station pressure by the dry-air gas constant alone and ignores humidity
    entirely, which on a warm humid day overstates density by about 1 %: water
    vapour is lighter than the air it displaces, so wet air is LESS dense, and
    a formula that leaves it out always errs in the same direction. Partial
    pressures are split here and each gets its own constant.
    """
    if station_pressure <= 0 or not 0.0 <= humidity <= 100.0:
        return None
    kelvin = temperature + 273.15
    if kelvin <= 0:
        return None
    # Tetens, over water, in hectopascals; × 100 for pascals.
    saturation = 6.1078 * 10 ** (7.5 * temperature / (temperature + 237.3)) * 100.0
    vapour = saturation * humidity / 100.0
    dry = station_pressure * 100.0 - vapour
    if dry <= 0:
        return None
    return dry / (287.058 * kelvin) + vapour / (461.495 * kelvin)


def apparent_temperature(
    temperature: float, humidity: float, wind: float | None
) -> float:
    """"Feels like" in °C: heat index when hot, wind chill when cold, else air.

    Both branches are the US National Weather Service's own published
    regressions and both are defined in FAHRENHEIT and MILES PER HOUR, so the
    conversion happens here, once, around the formula — rather than retyping
    the coefficients into metric, which is where such a rewrite goes wrong.

    Neither branch applies in the middle of the range, and that is not a gap:
    at 15 °C in a light breeze the air temperature IS what it feels like, and
    inventing an adjustment there would publish a difference nobody can feel.
    """
    fahrenheit = temperature * 9.0 / 5.0 + 32.0

    if fahrenheit >= 80.0 and humidity >= 40.0:
        return (_heat_index(fahrenheit, humidity) - 32.0) * 5.0 / 9.0

    if fahrenheit <= 50.0 and wind is not None:
        mph = wind * 2.236936
        if mph > 3.0:
            return (_wind_chill(fahrenheit, mph) - 32.0) * 5.0 / 9.0

    return temperature


def _heat_index(fahrenheit: float, humidity: float) -> float:
    """NWS heat index in °F, with the simple form and both adjustments.

    The Rothfusz regression it is built on was fitted against the middle of the
    table and overshoots at its edges, which is why the NWS publishes it with a
    low-end fallback and two corrections rather than on its own. All three are
    here; leaving them out is the common shortcut and it shows up exactly where
    the reading matters most — a very dry 105 °F afternoon and a very humid
    85 °F one.
    """
    simple = 0.5 * (
        fahrenheit + 61.0 + ((fahrenheit - 68.0) * 1.2) + (humidity * 0.094)
    )
    if (simple + fahrenheit) / 2.0 < 80.0:
        return simple

    index = (
        -42.379
        + 2.04901523 * fahrenheit
        + 10.14333127 * humidity
        - 0.22475541 * fahrenheit * humidity
        - 0.00683783 * fahrenheit**2
        - 0.05481717 * humidity**2
        + 0.00122874 * fahrenheit**2 * humidity
        + 0.00085282 * fahrenheit * humidity**2
        - 0.00000199 * fahrenheit**2 * humidity**2
    )

    if humidity < 13.0 and 80.0 <= fahrenheit <= 112.0:
        index -= ((13.0 - humidity) / 4.0) * math.sqrt(
            (17.0 - abs(fahrenheit - 95.0)) / 17.0
        )
    elif humidity > 85.0 and 80.0 <= fahrenheit <= 87.0:
        index += ((humidity - 85.0) / 10.0) * ((87.0 - fahrenheit) / 5.0)

    return index


def _wind_chill(fahrenheit: float, mph: float) -> float:
    """NWS wind chill in °F. Valid at or below 50 °F above 3 mph."""
    factor = mph**0.16
    return 35.74 + 0.6215 * fahrenheit - 35.75 * factor + 0.4275 * fahrenheit * factor
