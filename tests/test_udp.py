"""Tests for the local radio's wire format and everything derived from it.

`udp.py` imports nothing from `homeassistant`, so this suite loads it by file
path and runs it with no Home Assistant, no network and no station — the same
way `test_forecast.py` treats the cloud transform layer.

WHAT THIS SUITE IS ACTUALLY GUARDING. The observation is a BARE ARRAY: its
field order is the entire contract, and an index read one place out is not a
parse error, it is a pressure published as a temperature. Nothing downstream
would reject it, nothing would log, and the station would simply read wrong on
the glass. So the order is asserted positionally, against a row whose every
value is distinguishable, rather than against a realistic-looking row where two
plausible numbers could swap unnoticed.

The derived quantities are checked against values computed INDEPENDENTLY here
or taken from the publishing body's own worked example — never against the
module's own output, which would only prove the module agrees with itself.
"""

from __future__ import annotations

import errno
import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Load one pure module by path, with no package import behind it."""
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


udp = _load("udp")


# --------------------------------------------------------------------------
# The observation array.
# --------------------------------------------------------------------------

# Every value distinct and none of them plausible as another field, so a
# transposition cannot be hidden by two readings that look alike. Indices run
# 0..17 in the vendor's documented order.
MARKED_ROW = [
    1700000000,  # 0  time
    1.1,  # 1  wind lull
    2.2,  # 2  wind avg
    3.3,  # 3  wind gust
    44,  # 4  wind direction
    6,  # 5  sample interval — dropped on purpose
    1006.6,  # 6  station pressure
    17.7,  # 7  air temperature
    58.8,  # 8  relative humidity
    9999,  # 9  illuminance
    1.0,  # 10 uv
    111,  # 11 solar radiation
    0.5,  # 12 precip accumulation
    1,  # 13 precipitation type: rain
    14.4,  # 14 lightning average distance
    15,  # 15 lightning count
    2.66,  # 16 battery
    1,  # 17 report interval
]


def _obs_st(row: list | None = None) -> dict:
    return {
        "serial_number": "ST-00057870",
        "type": "obs_st",
        "hub_sn": "HB-00054235",
        "obs": [list(MARKED_ROW if row is None else row)],
        "firmware_revision": 176,
    }


def test_every_obs_st_field_lands_on_its_documented_index() -> None:
    """The whole contract, asserted positionally."""
    kind, readings = udp.parse(_obs_st())
    assert kind == "obs_st"
    assert readings["obs_time"] == 1700000000
    assert readings["wind_lull"] == 1.1
    assert readings["wind_avg"] == 2.2
    assert readings["wind_gust"] == 3.3
    assert readings["wind_direction_avg"] == 44
    assert readings["station_pressure"] == 1006.6
    assert readings["air_temperature"] == 17.7
    assert readings["relative_humidity"] == 58.8
    assert readings["illuminance"] == 9999
    assert readings["uv"] == 1.0
    assert readings["solar_radiation"] == 111
    assert readings["precip_accum_last_interval"] == 0.5
    assert readings["precipitation_type"] == "rain"
    assert readings["lightning_avg_distance"] == 14.4
    assert readings["lightning_count"] == 15
    assert readings["battery_voltage"] == 2.66
    assert readings["report_interval"] == 1


def test_the_wind_sample_interval_is_dropped_not_shifted_onto_its_neighbour() -> None:
    """Index 5 has no entity.

    A `None` in the field table must SKIP the index, not close the gap: closing
    it would slide station pressure into the sample interval's place and every
    field after it one to the left, which still parses and is entirely wrong.
    """
    _, readings = udp.parse(_obs_st())
    assert "wind_sample_interval" not in readings
    assert readings["station_pressure"] == 1006.6


def test_selftest_a_transposed_row_is_caught() -> None:
    """Prove the positional assertions above CAN fail.

    Swapping pressure and temperature produces a row that still parses, still
    has every field, and is wrong — which is exactly the failure mode the
    positional check exists for. If this suite could not tell the difference,
    the check above would be decoration.
    """
    row = list(MARKED_ROW)
    row[6], row[7] = row[7], row[6]
    _, readings = udp.parse(_obs_st(row))
    assert readings["station_pressure"] != 1006.6
    assert readings["air_temperature"] != 17.7


def test_a_batch_publishes_its_newest_row() -> None:
    """`obs` is a list oldest-first; the state is the last row, not the first."""
    old = list(MARKED_ROW)
    old[7] = -99.0
    message = _obs_st()
    message["obs"] = [old, list(MARKED_ROW)]
    _, readings = udp.parse(message)
    assert readings["air_temperature"] == 17.7


@pytest.mark.parametrize(
    "obs", [None, [], "not a list", [{"not": "a row"}], [[]]]
)
def test_a_malformed_observation_is_none_not_a_partial_reading(obs) -> None:
    """Half a parse is worse than none: it publishes some fields and not others."""
    message = _obs_st()
    message["obs"] = obs
    assert udp.parse(message) is None or udp.parse(message)[1] == {}


def test_a_short_row_publishes_what_it_has_and_nothing_it_does_not() -> None:
    """Firmware that stops the array early must not shift everything left."""
    _, readings = udp.parse(_obs_st(MARKED_ROW[:8]))
    assert readings["air_temperature"] == 17.7
    assert "relative_humidity" not in readings
    assert "battery_voltage" not in readings


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "none"), (1, "rain"), (2, "hail"), (3, "rain_hail")],
)
def test_precipitation_type_is_spelled_from_the_vendors_code(code, expected) -> None:
    row = list(MARKED_ROW)
    row[13] = code
    _, readings = udp.parse(_obs_st(row))
    assert readings["precipitation_type"] == expected


def test_an_unknown_precipitation_code_is_withheld_not_published_raw() -> None:
    """The entity declares its options; anything else is rejected every render."""
    row = list(MARKED_ROW)
    row[13] = 7
    _, readings = udp.parse(_obs_st(row))
    assert "precipitation_type" not in readings


def test_rain_rate_is_scaled_by_the_stations_own_report_interval() -> None:
    """A five-minute station must not publish five times the real intensity."""
    row = list(MARKED_ROW)
    row[12] = 0.5
    row[17] = 5
    _, readings = udp.parse(_obs_st(row))
    assert readings["precipitation_intensity"] == pytest.approx(6.0)

    row[17] = 1
    _, readings = udp.parse(_obs_st(row))
    assert readings["precipitation_intensity"] == pytest.approx(30.0)


def test_a_missing_reading_is_absent_never_zero() -> None:
    """`ok at zero` and `could not read` do not collapse."""
    row = list(MARKED_ROW)
    row[7] = None
    row[8] = "n/a"
    _, readings = udp.parse(_obs_st(row))
    assert "air_temperature" not in readings
    assert "relative_humidity" not in readings
    # And nothing derived from them is invented either.
    assert "dew_point" not in readings
    assert "feels_like" not in readings


def test_a_true_temperature_is_not_one_degree() -> None:
    """`bool` is an `int` in Python, so it has to be rejected explicitly."""
    row = list(MARKED_ROW)
    row[7] = True
    _, readings = udp.parse(_obs_st(row))
    assert "air_temperature" not in readings


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_readings_are_refused(bad) -> None:
    """A NaN temperature serialises to a state nothing downstream can compare."""
    row = list(MARKED_ROW)
    row[7] = bad
    _, readings = udp.parse(_obs_st(row))
    assert "air_temperature" not in readings


# --------------------------------------------------------------------------
# The other message types.
# --------------------------------------------------------------------------


def test_rapid_wind_is_the_three_second_sample() -> None:
    kind, readings = udp.parse(
        {"type": "rapid_wind", "serial_number": "ST-1", "ob": [1700000000, 2.3, 128]}
    )
    assert kind == "rapid_wind"
    assert readings == {"wind_speed": 2.3, "wind_direction": 128}


@pytest.mark.parametrize("ob", [None, [], [1700000000], [1700000000, 2.3]])
def test_a_truncated_rapid_wind_is_refused(ob) -> None:
    assert udp.parse({"type": "rapid_wind", "ob": ob}) is None


def test_boot_time_comes_from_the_devices_own_clock_not_ours() -> None:
    """Uptime is subtracted INSIDE the message, so the answer is a constant.

    Computing it from wall-clock `now()` instead would give a different answer
    every time the message arrived, and a timestamp sensor would then write a
    new state once a minute for a device that has not rebooted in a year.
    """
    kind, readings = udp.parse(
        {
            "type": "device_status",
            "serial_number": "ST-1",
            "timestamp": 1700000000,
            "uptime": 2189,
            "voltage": 2.66,
            "firmware_revision": 176,
            "rssi": -17,
            "hub_rssi": -87,
            "sensor_status": 0,
        }
    )
    assert kind == "device_status"
    assert readings["device_boot_time"] == 1700000000 - 2189
    assert readings["device_rssi"] == -17
    assert readings["device_firmware"] == "176"
    assert readings["sensor_faults"] == []


def test_hub_status_reads_the_hubs_own_radio_not_the_stations() -> None:
    kind, readings = udp.parse(
        {
            "type": "hub_status",
            "serial_number": "HB-1",
            "firmware_revision": "35",
            "uptime": 1670133,
            "rssi": -62,
            "timestamp": 1700000000,
        }
    )
    assert kind == "hub_status"
    assert readings["hub_rssi"] == -62
    assert readings["hub_firmware"] == "35"
    assert readings["hub_boot_time"] == 1700000000 - 1670133


def test_a_status_line_with_no_uptime_publishes_no_boot_time() -> None:
    """Half a subtraction is a wrong timestamp, not a missing one."""
    _, readings = udp.parse(
        {"type": "hub_status", "rssi": -62, "timestamp": 1700000000}
    )
    assert "hub_boot_time" not in readings


def test_sensor_faults_are_named_never_counted() -> None:
    """A directive that knows which sensor failed says which."""
    assert udp.sensor_faults(0) == []
    assert udp.sensor_faults(0x10) == ["temperature_failed"]
    assert set(udp.sensor_faults(0x10 | 0x40)) == {
        "temperature_failed",
        "wind_failed",
    }


def test_lightning_interference_bits_are_not_faults() -> None:
    """Noise and disturber are the detector REJECTING interference, i.e. working."""
    assert udp.sensor_faults(0x02) == []
    assert udp.sensor_faults(0x04) == []


@pytest.mark.parametrize(
    "message",
    [
        {"type": "obs_air", "obs": [[1, 2, 3]]},
        {"type": "obs_sky", "obs": [[1, 2, 3]]},
        {"type": "evt_precip", "evt": [1700000000]},
        {"type": "evt_strike", "evt": [1700000000, 27, 3848]},
        {"type": "something_new"},
        {"no": "type at all"},
        "not a dict",
    ],
)
def test_a_message_this_component_does_not_read_is_none(message) -> None:
    assert udp.parse(message) is None


def test_the_handled_and_ignored_sets_do_not_overlap() -> None:
    """A type cannot be both read and deliberately skipped."""
    assert not udp.HANDLED & udp.IGNORED


# --------------------------------------------------------------------------
# Decoding whatever else is on the port.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00\x01\x02\xff",  # not UTF-8
        b"not json at all",
        b"[1, 2, 3]",  # JSON, but not an object
        b'"a string"',
        b"",
    ],
)
def test_a_stray_datagram_decodes_to_none_rather_than_raising(raw) -> None:
    """The port carries whatever else broadcasts on the LAN.

    A decoder that raised would take the listener down on the first stray
    packet, and the station would go quiet for a reason nothing about the
    station explains.
    """
    assert udp.decode(raw) is None


def test_a_real_datagram_decodes() -> None:
    assert udp.decode(b'{"type":"hub_status","rssi":-62}') == {
        "type": "hub_status",
        "rssi": -62,
    }


# --------------------------------------------------------------------------
# Derived quantities.
# --------------------------------------------------------------------------


def test_dew_point_against_an_independently_computed_value() -> None:
    """Magnus-Tetens, recomputed here from the coefficients rather than called."""
    temperature, humidity = 25.0, 60.0
    a, b = 17.625, 243.04
    gamma = math.log(humidity / 100.0) + a * temperature / (b + temperature)
    expected = b * gamma / (a - gamma)
    assert udp.dew_point(temperature, humidity) == pytest.approx(expected)
    # And it is a physically sane answer, not merely a self-consistent one.
    assert 16.0 < udp.dew_point(temperature, humidity) < 18.0


def test_dew_point_is_never_defined_at_zero_humidity() -> None:
    """The formula takes log(RH); zero is undefined, not very cold."""
    assert udp.dew_point(20.0, 0.0) is None
    assert udp.dew_point(20.0, 101.0) is None


def test_dew_point_cannot_exceed_air_temperature() -> None:
    """A physical invariant, checked across the range rather than at one point."""
    for temperature in range(-15, 45, 5):
        for humidity in range(5, 100, 5):
            value = udp.dew_point(float(temperature), float(humidity))
            assert value is not None
            assert value <= temperature + 1e-6


def test_wet_bulb_sits_between_dew_point_and_air_temperature() -> None:
    """The other physical invariant, and the one Stull's fit can violate."""
    for temperature in range(-10, 45, 5):
        for humidity in range(10, 99, 10):
            wet = udp.wet_bulb(float(temperature), float(humidity))
            dew = udp.dew_point(float(temperature), float(humidity))
            assert wet is not None and dew is not None
            assert dew - 0.6 <= wet <= temperature + 0.6


def test_wet_bulb_refuses_the_range_its_fit_does_not_cover() -> None:
    """Stull is a regression, not a solver: outside its band it is not wrong slowly."""
    assert udp.wet_bulb(60.0, 50.0) is None
    assert udp.wet_bulb(20.0, 2.0) is None


def test_moist_air_is_less_dense_than_dry_air_at_the_same_pressure() -> None:
    """The defect in the integration this replaces, stated as a test.

    That one divides station pressure by the dry-air gas constant alone. Water
    vapour is lighter than the air it displaces, so leaving humidity out always
    overstates density — the error is one-directional, which is why it never
    looked like noise.
    """
    temperature, pressure = 32.1, 994.4
    dry = pressure * 100.0 / (287.058 * (temperature + 273.15))
    humid = udp.air_density(temperature, 64.07, pressure)
    assert humid is not None
    assert humid < dry
    assert dry - humid == pytest.approx(0.013, abs=0.003)


def test_air_density_is_in_the_right_ballpark_at_sea_level() -> None:
    """1.225 kg/m³ is the ICAO standard atmosphere at 15 °C and 1013.25 mb."""
    value = udp.air_density(15.0, 0.0, 1013.25)
    assert value is not None
    assert value == pytest.approx(1.225, abs=0.002)


def test_heat_index_matches_the_nws_published_table() -> None:
    """The Rothfusz regression, against the body that publishes it.

    The NWS heat index table reads 100 °F at 86 °F and 80 % RH, and 105 °F at
    90 °F and 70 %. Read in Fahrenheit because that is the unit the regression
    is defined in; the component's entry point converts around it rather than
    retyping the coefficients into metric.
    """
    assert udp._heat_index(86.0, 80.0) == pytest.approx(100.0, abs=1.0)
    assert udp._heat_index(90.0, 70.0) == pytest.approx(105.0, abs=1.0)


def test_feels_like_uses_the_heat_index_when_it_is_hot_and_humid() -> None:
    """Checked against a reading the live station and its old integration agreed on."""
    value = udp.apparent_temperature(32.1, 64.07, 0.3)
    # 101.5 °F, which is what the station's own app showed for this observation.
    assert value * 9.0 / 5.0 + 32.0 == pytest.approx(101.5, abs=0.3)


def test_feels_like_uses_wind_chill_when_it_is_cold_and_windy() -> None:
    """The NWS wind chill chart reads 27 °F at 35 °F in a 10 mph wind."""
    value = udp.apparent_temperature((35.0 - 32.0) * 5.0 / 9.0, 50.0, 10.0 / 2.236936)
    assert value * 9.0 / 5.0 + 32.0 == pytest.approx(27.0, abs=1.0)


def test_a_calm_cold_day_gets_no_wind_chill() -> None:
    """Below 3 mph the regression does not apply and the air temperature is the answer."""
    assert udp.apparent_temperature(0.0, 50.0, 0.5) == 0.0
    assert udp.apparent_temperature(0.0, 50.0, None) == 0.0


def test_the_middle_of_the_range_is_left_alone() -> None:
    """Neither branch applies at 15 °C, and inventing one would publish a
    difference nobody can feel."""
    assert udp.apparent_temperature(15.0, 50.0, 2.0) == 15.0


def test_dry_heat_is_adjusted_downward_the_way_the_nws_says() -> None:
    """The low-humidity correction, which the common shortcut leaves out."""
    plain = (
        -42.379
        + 2.04901523 * 100.0
        + 10.14333127 * 10.0
        - 0.22475541 * 100.0 * 10.0
        - 0.00683783 * 100.0**2
        - 0.05481717 * 10.0**2
        + 0.00122874 * 100.0**2 * 10.0
        + 0.00085282 * 100.0 * 10.0**2
        - 0.00000199 * 100.0**2 * 10.0**2
    )
    assert udp._heat_index(100.0, 10.0) < plain


# --------------------------------------------------------------------------
# The joins.
# --------------------------------------------------------------------------


def test_every_reading_any_message_can_emit_has_a_source() -> None:
    """Both directions.

    Availability is decided by looking a reading up in `SOURCE_OF`, so a
    reading the parser emits but the table does not know reads as permanently
    unavailable — present in the state machine, never published. And an entry
    in the table with nothing emitting it is a staleness window guarding
    nothing.
    """
    emitted: set[str] = set()
    for message in (
        _obs_st(),
        {"type": "rapid_wind", "ob": [1, 2.3, 128]},
        {
            "type": "device_status",
            "timestamp": 1,
            "uptime": 1,
            "rssi": -1,
            "firmware_revision": 1,
            "sensor_status": 0,
        },
        {
            "type": "hub_status",
            "timestamp": 1,
            "uptime": 1,
            "rssi": -1,
            "firmware_revision": "1",
        },
    ):
        parsed = udp.parse(message)
        assert parsed is not None
        emitted |= set(parsed[1])

    assert emitted - set(udp.SOURCE_OF) == set(), "reading with no source"
    assert set(udp.SOURCE_OF) - emitted == set(), "source with no reading"


def test_every_source_names_a_message_type_the_parser_handles() -> None:
    assert set(udp.SOURCE_OF.values()) <= udp.HANDLED


def test_station_serials_picks_the_station_and_the_hub() -> None:
    station = {
        "station_id": 197799,
        "devices": [
            {"device_type": "HB", "serial_number": "HB-00054235"},
            {"device_type": "ST", "serial_number": "ST-00057870"},
        ],
    }
    assert udp.station_serials(station) == ("ST-00057870", "HB-00054235")


@pytest.mark.parametrize(
    "station",
    [
        None,
        "not a dict",
        {},
        {"devices": "not a list"},
        {"devices": [{"device_type": "AR", "serial_number": "AR-1"}]},
    ],
)
def test_a_station_record_with_no_tempest_yields_no_serials(station) -> None:
    """No serial is a real state: the listener runs unfiltered and says so."""
    assert udp.station_serials(station) == (None, None)


def test_selftest_the_source_join_detects_both_directions() -> None:
    """Prove the join above can fail."""
    emitted = {"a", "b"}
    sources = {"a", "c"}
    assert emitted - sources == {"b"}  # reading with no source
    assert sources - emitted == {"c"}  # source with no reading


# --------------------------------------------------------------------------
# Why a bind failed.
#
# This is here because it SHIPPED WRONG. One release reported every bind
# failure as "Home Assistant is not on the hub's broadcast domain" — correct
# for a container on a bridge network, and completely misleading for the case
# that actually occurred, which was another integration already holding the
# port. The wrong cause in a log line is worse than no line at all: it sends
# the reader to the network and they stay there.
# --------------------------------------------------------------------------


def test_a_held_port_is_diagnosed_as_a_held_port() -> None:
    """The errno decides the wording, not the first plausible explanation."""
    advice = udp.bind_failure_advice(errno.EADDRINUSE)
    assert advice == udp.PORT_IN_USE_ADVICE
    assert "already holds it" in advice
    # And it names the concrete thing a user can actually do about it.
    assert "tempest" in advice


@pytest.mark.parametrize(
    "error_number",
    [errno.EACCES, errno.EAFNOSUPPORT, errno.ENODEV, 0, None],
)
def test_any_other_failure_keeps_the_networking_explanation(error_number) -> None:
    """Everything that is not a collision is still most likely the network."""
    assert udp.bind_failure_advice(error_number) == udp.NOT_ON_LAN_ADVICE


def test_selftest_the_two_explanations_are_actually_different() -> None:
    """Prove the check above can fail.

    A refactor that collapsed both branches onto one string would leave every
    assertion here passing while restoring exactly the defect this pair of
    messages exists to fix.
    """
    assert udp.PORT_IN_USE_ADVICE != udp.NOT_ON_LAN_ADVICE
