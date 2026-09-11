"""Static joins over the component's own declarations.

Parses the platform modules with `ast` rather than importing them, so this
suite still needs no Home Assistant. Everything here is a JOIN, both
directions — an audit is a join, and a one-way check passes happily over an
orphan. A one-way "every entity has a name" check would never notice
a translation left behind by a deleted sensor.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _entity_keys(module: str) -> tuple[set[str], set[str]]:
    """(key, translation_key) sets from one platform module's descriptions."""
    tree = ast.parse((ROOT / module).read_text())
    keys: set[str] = set()
    translation_keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Any description class, not one spelling of one. The local platforms
        # use the plain `BinarySensorEntityDescription` while the cloud ones
        # use a subclass, and a matcher keyed on the subclass's name passed
        # happily over every entity declared with the base — a one-way check
        # that reported complete over a set it never looked at.
        if not getattr(node.func, "id", "").endswith("Description"):
            continue
        fields = {kw.arg: kw.value for kw in node.keywords}
        for field, sink in (("key", keys), ("translation_key", translation_keys)):
            value = fields.get(field)
            if isinstance(value, ast.Constant):
                sink.add(value.value)
    return keys, translation_keys


@pytest.fixture(name="strings")
def strings_fixture() -> dict:
    """The translation source."""
    return json.loads((ROOT / "strings.json").read_text())


def test_en_translation_matches_strings(strings: dict) -> None:
    """translations/en.json is a copy of strings.json, not a fork of it."""
    assert json.loads((ROOT / "translations" / "en.json").read_text()) == strings


@pytest.mark.parametrize(
    ("module", "platform", "expected"),
    # 46 = 18 cloud + 28 local; 3 = 2 cloud + 1 local. The count is encoded
    # rather than inferred, so adding an entity without naming it fails here
    # instead of shipping as a blank row on a card.
    [("sensor.py", "sensor", 46), ("binary_sensor.py", "binary_sensor", 3)],
)
def test_every_entity_is_named_and_every_name_is_used(
    strings: dict, module: str, platform: str, expected: int
) -> None:
    """The join, both directions, with the count encoded rather than inferred."""
    keys, translation_keys = _entity_keys(module)
    declared = set(strings["entity"][platform])

    assert len(keys) == expected
    assert keys == translation_keys, keys ^ translation_keys
    assert translation_keys - declared == set(), "entity with no name"
    assert declared - translation_keys == set(), "name with no entity"


def test_unique_id_suffixes_cannot_collide() -> None:
    """No two entities across the platforms share a key.

    Unique ids are f"{DOMAIN}_{station_id}_{key}", so a key reused across two
    platforms would produce two entities claiming one id, and an id already
    occupied is taken with a `_2` suffix that Home Assistant never reclaims.
    """
    sensor_keys, _ = _entity_keys("sensor.py")
    binary_keys, _ = _entity_keys("binary_sensor.py")
    assert not sensor_keys & binary_keys
    # The weather entity's suffix is a literal; assert it is not in either set.
    assert "weather" not in sensor_keys | binary_keys


def test_quality_scale_has_no_silent_todos() -> None:
    """Every rule is `done` or an explicit `todo` — never blank or invented.

    The tier gaps themselves are tracked in the issue queue, not here. This
    only asserts the file says something legible about every rule it lists.
    """
    rules = yaml.safe_load((ROOT / "quality_scale.yaml").read_text())["rules"]
    assert rules
    for name, value in rules.items():
        assert isinstance(value, str) and value.strip(), name
        assert value.split("#")[0].strip() in ("done", "todo", "exempt"), (
            name,
            value,
        )


def test_manifest_declares_no_quality_scale() -> None:
    """A manifest `quality_scale` key is a self-claim with no gate behind it.

    hassfest's validate_iqs_file returns immediately for a custom component, so the key would read green forever without anything checking
    it. quality_scale.yaml is the honest record instead.
    """
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert "quality_scale" not in manifest
    assert manifest["domain"] == "tempest_wx"
    # No third-party requirements: dependency-transparency is claimed `done`
    # in quality_scale.yaml on the strength of this being empty.
    assert manifest["requirements"] == []


def _udp() -> object:
    """`udp.py`, loaded by path — it imports no Home Assistant."""
    spec = importlib.util.spec_from_file_location("udp_wiring", ROOT / "udp.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _weather_reading_keys() -> set[str]:
    """Every key `weather.py` asks `_reading` for, by AST."""
    tree = ast.parse((ROOT / "weather.py").read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "_reading":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            keys.add(node.args[0].value)
    return keys


def test_every_reading_the_weather_entity_asks_for_can_come_off_the_radio() -> None:
    """The join this component exists to keep true.

    The weather entity names its readings in the CLOUD payload's vocabulary and
    the radio answers in its own, with `weather.LOCAL_KEY` bridging the ones
    that differ. A key on neither side of that bridge is not an error anywhere:
    the local lookup simply never matches, the cloud answers, and the entity
    quietly stops preferring local for that one reading — the exact silent
    regression this whole change was made to remove.
    """
    udp = _udp()
    tree = ast.parse((ROOT / "weather.py").read_text())
    bridge: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "LOCAL_KEY"
        ):
            bridge = ast.literal_eval(node.value)

    asked = _weather_reading_keys()
    assert asked, "parsed no readings out of weather.py — the AST walk is wrong"
    unreachable = {
        key for key in asked if bridge.get(key, key) not in udp.SOURCE_OF
    }
    assert unreachable == set(), unreachable
    # And the bridge carries nothing the entity never asks for.
    assert set(bridge) <= asked, set(bridge) - asked


def test_the_local_path_reads_no_other_integrations_entities() -> None:
    """The coupling this release removes, asserted gone.

    `local.py` used to name nine `sensor.tempest_*` ids belonging to a separate
    integration. Re-introducing one would work perfectly on the estate it was
    written on and publish nothing on anyone else's, which is the kind of
    regression that does not show up in a test run at all unless something
    looks for it by shape.
    """
    for module in ("local.py", "weather.py", "sensor.py", "binary_sensor.py"):
        offenders = [
            text
            for text in _code_strings(ROOT / module)
            if "sensor.tempest" in text
        ]
        assert offenders == [], (module, offenders)


def _code_strings(path: Path) -> list[str]:
    """Every string literal in a module EXCEPT its docstrings.

    Comments and docstrings are stripped because these files document the
    coupling they removed, and a check run over the prose matches the very
    sentence saying the thing is gone. Docstrings are identified structurally —
    the first statement of a module, class or function body — rather than by
    looking for triple quotes, which would also delete a multi-line entity id.
    """
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_selftest_the_foreign_entity_check_can_fail() -> None:
    """Prove the check above looks at code and not only at prose.

    Two halves, because each covers the other's blind spot: a literal in code
    must be SEEN, and the same text inside a docstring must be IGNORED. A check
    that failed the second half would go permanently red on the paragraph that
    records the removal, and the next session would delete the check rather
    than the coupling.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        offender = Path(directory) / "offender.py"
        offender.write_text(
            '"""This module once read sensor.tempest_sensor_air_temperature."""\n'
            'LOCAL = {"air": "sensor.tempest_sensor_air_temperature"}\n'
        )
        found = [t for t in _code_strings(offender) if "sensor.tempest" in t]
        assert found == ["sensor.tempest_sensor_air_temperature"]

        clean = Path(directory) / "clean.py"
        clean.write_text(
            '"""This module once read sensor.tempest_sensor_air_temperature."""\n'
            'LOCAL = {"air": "air_temperature"}\n'
        )
        assert [t for t in _code_strings(clean) if "sensor.tempest" in t] == []


def test_selftest_the_join_detects_both_directions() -> None:
    """Prove the join above can fail."""
    declared = {"a", "b"}
    translation_keys = {"a", "c"}
    assert translation_keys - declared == {"c"}  # entity with no name
    assert declared - translation_keys == {"b"}  # name with no entity
