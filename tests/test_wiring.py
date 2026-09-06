"""Static joins over the component's own declarations.

Parses the platform modules with `ast` rather than importing them, so this
suite still needs no Home Assistant. Everything here is a JOIN, both
directions (LAW.md §5: an audit is a join, and a one-way check passes happily
over an orphan). A one-way "every entity has a name" check would never notice
a translation left behind by a deleted sensor.
"""

from __future__ import annotations

import ast
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
        if "SensorDescription" not in getattr(node.func, "id", ""):
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
    [("sensor.py", "sensor", 18), ("binary_sensor.py", "binary_sensor", 2)],
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
    platforms would produce two entities claiming one id. TOOLS.md records what
    that costs: an id already occupied is taken with a `_2` suffix that is
    never reclaimed.
    """
    sensor_keys, _ = _entity_keys("sensor.py")
    binary_keys, _ = _entity_keys("binary_sensor.py")
    assert not sensor_keys & binary_keys
    # The weather entity's suffix is a literal; assert it is not in either set.
    assert "weather" not in sensor_keys | binary_keys


def test_quality_scale_has_no_silent_todos() -> None:
    """Every rule is `done` or an explicit `todo` — never blank or invented.

    The tier gaps themselves are tracked in GH-587 and GH-491, not here. This
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

    TOOLS.md: hassfest's validate_iqs_file returns immediately for a custom
    component, so the key would read green forever without anything checking
    it. quality_scale.yaml is the honest record instead.
    """
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert "quality_scale" not in manifest
    assert manifest["domain"] == "tempest_wx"
    # No third-party requirements: dependency-transparency is claimed `done`
    # in quality_scale.yaml on the strength of this being empty.
    assert manifest["requirements"] == []


def test_selftest_the_join_detects_both_directions() -> None:
    """Prove the join above can fail (LAW.md §4)."""
    declared = {"a", "b"}
    translation_keys = {"a", "c"}
    assert translation_keys - declared == {"c"}  # entity with no name
    assert declared - translation_keys == {"b"}  # name with no entity
