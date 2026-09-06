"""Tests for the local-radio read path.

`local.py` imports `homeassistant` (it uses HA's own unit converters rather
than retyping the constants), so unlike the forecast suite this one cannot run
against the real module without HA installed. It runs against a stub of the
two HA surfaces local.py touches — `hass.states.get` and the converters — which
is enough to test the thing that actually goes wrong: the UNIT handling.

The conversion arithmetic is checked against values computed independently
here, not against the converter's own output, so a converter that silently
started returning its input unchanged would fail this suite rather than agree
with itself.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# A stub of exactly the homeassistant surface local.py imports.
# --------------------------------------------------------------------------
def _install_ha_stub() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    util = types.ModuleType("homeassistant.util")
    conv = types.ModuleType("homeassistant.util.unit_conversion")

    class UnitOfTemperature:
        CELSIUS = "°C"
        FAHRENHEIT = "°F"

    class UnitOfPressure:
        MBAR = "mbar"
        INHG = "inHg"

    class UnitOfSpeed:
        METERS_PER_SECOND = "m/s"
        MILES_PER_HOUR = "mph"

    const.UnitOfTemperature = UnitOfTemperature
    const.UnitOfPressure = UnitOfPressure
    const.UnitOfSpeed = UnitOfSpeed

    class HomeAssistant:  # only used as a type annotation
        pass

    core.HomeAssistant = HomeAssistant

    class _Converter:
        _to_c = staticmethod(lambda v: (v - 32.0) * 5.0 / 9.0)

        @classmethod
        def convert(cls, value, source, target):
            if source == target:
                return value
            key = (source, target)
            table = cls.TABLE
            if key not in table:
                raise ValueError(f"cannot convert {source} to {target}")
            return table[key](value)

    class TemperatureConverter(_Converter):
        TABLE = {("°F", "°C"): lambda v: (v - 32.0) * 5.0 / 9.0}

    class PressureConverter(_Converter):
        TABLE = {("inHg", "mbar"): lambda v: v * 33.86389}

    class SpeedConverter(_Converter):
        TABLE = {("mph", "m/s"): lambda v: v * 0.44704}

    conv.TemperatureConverter = TemperatureConverter
    conv.PressureConverter = PressureConverter
    conv.SpeedConverter = SpeedConverter

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.unit_conversion"] = conv


_install_ha_stub()

# const.py imports only stdlib + homeassistant.const, both satisfied above.
_pkg = types.ModuleType("tempest_wx_pkg")
_pkg.__path__ = [str(ROOT)]
sys.modules["tempest_wx_pkg"] = _pkg
for name in ("const", "local"):
    spec = importlib.util.spec_from_file_location(
        f"tempest_wx_pkg.{name}", ROOT / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"tempest_wx_pkg.{name}"] = mod
    spec.loader.exec_module(mod)

local = sys.modules["tempest_wx_pkg.local"]


# --------------------------------------------------------------------------
# A fake state machine
# --------------------------------------------------------------------------
class _State:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {} if unit is None else {"unit_of_measurement": unit}


class _Hass:
    def __init__(self, mapping):
        self._m = mapping

    @property
    def states(self):
        return self

    def get(self, entity_id):
        return self._m.get(entity_id)


def hass(**by_key):
    """Build a hass whose local sensors hold the given (value, unit) pairs."""
    mapping = {}
    for key, value in by_key.items():
        entity_id = local.LOCAL_SOURCES[key]
        mapping[entity_id] = value if isinstance(value, _State) else _State(*value)
    return _Hass(mapping)


# --------------------------------------------------------------------------
# The unit trap
# --------------------------------------------------------------------------


def test_fahrenheit_is_converted_not_passed_through() -> None:
    """THE FAULT THIS SUITE EXISTS FOR.

    The local platform publishes °F; the weather entity declares °C natives and
    lets HA convert for display. Handing 79.5 °F straight to a °C property has
    HA convert it a second time and render 26° on a 79° day — which reads as a
    broken sensor, not a broken unit.
    """
    h = hass(air_temperature=(79.52, "°F"))
    got = local.read_local(h, "air_temperature")
    assert got == pytest.approx(26.4, abs=0.05)
    assert got != pytest.approx(79.52)


def test_pressure_inhg_becomes_mbar() -> None:
    """29.23 inHg is about 990 mbar, not 29.23 mbar (a near-vacuum)."""
    h = hass(station_pressure=(29.2273, "inHg"))
    assert local.read_local(h, "station_pressure") == pytest.approx(989.7, abs=1.0)


def test_wind_mph_becomes_metres_per_second() -> None:
    """The 1.0 threshold in weather.py is m/s, so this conversion gates it."""
    h = hass(wind_avg=(10.0, "mph"))
    assert local.read_local(h, "wind_avg") == pytest.approx(4.4704, abs=0.001)


def test_matching_units_are_not_converted_twice() -> None:
    """A source already in the target unit passes through untouched."""
    h = hass(air_temperature=(21.0, "°C"))
    assert local.read_local(h, "air_temperature") == 21.0


def test_unitless_readings_pass_through() -> None:
    """Humidity, UV and a compass bearing have no conversion and need none."""
    h = hass(
        relative_humidity=(81.86, "%"),
        uv=(0.02, "UV index"),
        wind_direction=(213.0, "°"),
    )
    assert local.read_local(h, "relative_humidity") == pytest.approx(81.86)
    assert local.read_local(h, "uv") == pytest.approx(0.02)
    assert local.read_local(h, "wind_direction") == pytest.approx(213.0)


def test_an_unconvertible_unit_falls_through_rather_than_lying() -> None:
    """A unit the converter does not know yields None, not the raw number.

    None sends the caller to the cloud. Returning the raw value would publish a
    kelvin reading as celsius, which is worse than having no local reading.
    """
    h = hass(air_temperature=(300.0, "K"))
    assert local.read_local(h, "air_temperature") is None


# --------------------------------------------------------------------------
# Unreadable states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["unknown", "unavailable", "none", "", "None", "  "])
def test_unreadable_states_are_none_never_zero(bad: str) -> None:
    """`could not read` must not collapse into `ok at zero` (LAW.md §11)."""
    h = hass(air_temperature=(bad, "°F"))
    assert local.read_local(h, "air_temperature") is None


def test_a_missing_entity_is_none() -> None:
    """No local integration at all is the cloud-fallback case, not a crash."""
    assert local.read_local(_Hass({}), "air_temperature") is None


def test_a_non_numeric_state_is_none() -> None:
    """A string that is not a number does not raise out of the property."""
    h = hass(air_temperature=("warm", "°F"))
    assert local.read_local(h, "air_temperature") is None


def test_an_unmapped_key_is_none() -> None:
    """Asking for something the radio does not carry is None, not KeyError."""
    assert local.read_local(hass(), "sea_level_pressure") is None


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_local_is_answering_tracks_temperature() -> None:
    """The availability signal the weather entity ORs against the coordinator."""
    assert local.local_is_answering(hass(air_temperature=(79.52, "°F"))) is True
    assert local.local_is_answering(hass(air_temperature=("unavailable",))) is False
    assert local.local_is_answering(_Hass({})) is False


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_every_source_the_weather_entity_asks_for_exists() -> None:
    """Join weather.py's `_reading` calls against LOCAL_SOURCES.

    A key the entity asks for that local.py does not map is not an error — it
    falls through to the cloud — but it is almost always a typo, and a typo
    here is invisible: the reading simply stops preferring local and nothing
    says so.
    """
    import ast

    tree = ast.parse((ROOT / "weather.py").read_text())
    asked = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "_reading"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert asked, "found no _reading calls — the join is vacuous"
    unmapped = asked - set(local.LOCAL_SOURCES)
    # sea_level_pressure is genuinely cloud-only; the radio does not derive it.
    assert unmapped <= {"sea_level_pressure"}, unmapped


def test_selftest_the_conversion_check_can_fail() -> None:
    """Prove the °F assertion above discriminates (LAW.md §4)."""
    raw = 79.52
    converted = (raw - 32.0) * 5.0 / 9.0
    assert converted != pytest.approx(raw)
    assert converted == pytest.approx(26.4, abs=0.05)
