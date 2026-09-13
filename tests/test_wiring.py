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


# --- the options flow -------------------------------------------------------


def _const_strings() -> dict[str, str]:
    """`const.py`'s string constants, by name, so a schema key can be resolved.

    The flow names its fields through `CONF_*`, never as literals, so a check
    that only understood literals would report zero fields and pass vacuously
    over the whole join below.
    """
    tree = ast.parse((ROOT / "const.py").read_text())
    values: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[node.target.id] = node.value.value
    return values


def _options_flow_class() -> ast.ClassDef:
    """The one `OptionsFlow` subclass in config_flow.py."""
    tree = ast.parse((ROOT / "config_flow.py").read_text())
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(getattr(base, "id", "") == "OptionsFlow" for base in node.bases)
    ]
    assert len(classes) == 1, [node.name for node in classes]
    return classes[0]


def _schema_fields(node: ast.AST) -> set[str]:
    """Every `vol.Required`/`vol.Optional` key under this node, resolved."""
    consts = _const_strings()
    fields: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("Required", "Optional") or not call.args:
            continue
        key = call.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            fields.add(key.value)
        elif isinstance(key, ast.Name) and key.id in consts:
            fields.add(consts[key.id])
    return fields


def _option_merges(node: ast.AST) -> list[bool]:
    """For each `async_create_entry` under this node, whether it MERGES options.

    An options flow's `async_create_entry(data=...)` replaces `entry.options`
    wholesale rather than updating it, so a step that returns only its own keys
    deletes every other step's — silently, and invisibly until a second step
    exists to be deleted. The one-step case reads as correct either way, which
    is exactly why this is asserted rather than left to a comment.
    """
    merges: list[bool] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "async_create_entry":
            continue
        data = next((kw.value for kw in call.keywords if kw.arg == "data"), None)
        merges.append(_unpacks_existing_options(data))
    return merges


def _unpacks_existing_options(data: ast.AST | None) -> bool:
    """Whether a `data=` argument spreads the entry's existing options into itself."""
    if not isinstance(data, ast.Dict):
        return False
    return any(
        # A `**` unpack is the only entry in an ast.Dict with no key.
        key is None and ast.unparse(value).replace(" ", "") == (
            "self.config_entry.options"
        )
        for key, value in zip(data.keys, data.values)
    )


def test_every_options_step_merges_over_the_existing_options() -> None:
    """The trap this component is allowed to meet exactly once."""
    merges = _option_merges(_options_flow_class())
    assert merges, "parsed no async_create_entry out of the options flow"
    assert all(merges), merges


def test_selftest_the_options_merge_check_can_fail() -> None:
    """Prove the check above separates a merge from a replacement.

    Three shapes, because the wrong two are the ones a step is actually written
    as by accident: the user input passed straight through, and a literal dict
    of this step's own keys. Both look right in isolation.
    """
    def merges(body: str) -> list[bool]:
        return _option_merges(
            ast.parse(
                "class F(OptionsFlow):\n"
                "    def s(self, user_input):\n"
                f"        return self.async_create_entry({body})\n"
            )
        )

    assert merges("data={**self.config_entry.options, **user_input}") == [True]
    assert merges("data=user_input") == [False]
    assert merges('data={"local_udp": True}') == [False]


def test_every_options_field_is_named_and_every_name_is_used(strings: dict) -> None:
    """The join, both directions, over the options step's own fields."""
    fields = _schema_fields(_options_flow_class())
    step = strings["options"]["step"]["init"]
    declared = set(step["data"])
    described = set(step["data_description"])

    assert fields, "parsed no fields out of the options flow's schema"
    assert fields == declared, fields ^ declared
    # A description for a field that is not there is a name with no entity by
    # another route: it renders nowhere and nothing ever notices it is stale.
    assert described <= declared, described - declared


# --- what setup does with the option ----------------------------------------


def _init_tree() -> ast.Module:
    """`__init__.py`, parsed.

    Parsed with the running interpreter's own grammar, so the module's PEP 695
    `type` statement needs Python 3.12 or newer. Deliberately not guarded: Home
    Assistant 2026.x requires an interpreter well past that, so a run old enough
    to choke here could not be making a claim about this component anyway, and a
    skip would turn that into a quiet pass. CI runs this suite on 3.13.
    """
    return ast.parse((ROOT / "__init__.py").read_text())


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    """One top-level async function, by name."""
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no async def {name} at module level")


def _calls_named(node: ast.AST, attr: str) -> list[ast.Call]:
    """Every call to a method of this name under a node."""
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attr
    ]


def _enclosing_ifs(node: ast.AST, target: ast.Call) -> list[ast.If]:
    """Every `if` statement under a node that contains this exact call."""
    return [
        branch
        for branch in ast.walk(node)
        if isinstance(branch, ast.If)
        and any(inner is target for inner in ast.walk(branch))
    ]


def test_the_listener_starts_only_under_the_options_own_test() -> None:
    """An unconditional `async_start()` is the defect the toggle exists to remove.

    Asserted on the CONDITION and not only on the presence of one, because an
    `if` on something else — a stored serial, a platform list — would satisfy a
    check that merely counted branches while leaving the listener ungoverned.
    """
    setup = _function(_init_tree(), "async_setup_entry")
    starts = _calls_named(setup, "async_start")
    assert len(starts) == 1, len(starts)

    guards = _enclosing_ifs(setup, starts[0])
    assert guards, "async_start() is not inside any conditional"
    tests = {ast.unparse(branch.test) for branch in guards}
    assert any("local_udp" in test for test in tests), tests


def test_selftest_the_listener_gate_check_can_fail() -> None:
    """Prove the gate check sees an ungated start, and a wrongly gated one."""
    ungated = _function(
        ast.parse(
            "async def async_setup_entry(hass, entry):\n"
            "    await station.async_start()\n"
        ),
        "async_setup_entry",
    )
    starts = _calls_named(ungated, "async_start")
    assert len(starts) == 1
    assert _enclosing_ifs(ungated, starts[0]) == []

    wrong = _function(
        ast.parse(
            "async def async_setup_entry(hass, entry):\n"
            "    if entry.data.get('device_serial'):\n"
            "        await station.async_start()\n"
        ),
        "async_setup_entry",
    )
    branches = _enclosing_ifs(wrong, _calls_named(wrong, "async_start")[0])
    assert branches
    assert not any(
        "local_udp" in ast.unparse(branch.test) for branch in branches
    )


def test_the_update_listener_is_registered_after_the_serial_top_up() -> None:
    """Order is load-bearing here, and nothing about it is visible at runtime.

    `_async_learn_serials` writes `entry.data` through `async_update_entry`,
    which fires every registered update listener as a task. Registered first,
    this entry's listener is scheduled mid-setup and runs at the next await —
    reading a `runtime_data` that is not assigned yet, over a write the setup
    made itself. The entry still finishes loading, so the symptom is an
    exception in a task nobody is watching and a reload nobody asked for.
    """
    setup = _function(_init_tree(), "async_setup_entry")
    lines = {
        fragment: [
            call.lineno
            for call in ast.walk(setup)
            if isinstance(call, ast.Call) and fragment in ast.unparse(call.func)
        ]
        for fragment in ("_async_learn_serials", "add_update_listener")
    }
    assert all(len(found) == 1 for found in lines.values()), lines
    assert lines["add_update_listener"][0] > lines["_async_learn_serials"][0], lines


def test_the_update_listener_reloads_only_when_the_toggle_moved() -> None:
    """A blanket reload would restart the entry for every write to it.

    Including the serial top-up above and a reauth, which reloads itself — so
    the listener compares what setup ACTED on against what the options now say,
    and returns without doing anything when they agree.
    """
    listener = _function(_init_tree(), "_async_entry_updated")
    guards = [
        ast.unparse(branch.test)
        for branch in ast.walk(listener)
        if isinstance(branch, ast.If)
        and any(isinstance(statement, ast.Return) for statement in branch.body)
    ]
    assert guards, "the update listener has no early return; it reloads on any write"
    assert any(
        "local_udp" in guard and "runtime_data" in guard for guard in guards
    ), guards
    assert len(_calls_named(listener, "async_reload")) == 1


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
