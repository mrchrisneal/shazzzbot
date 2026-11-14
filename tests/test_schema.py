import math
import os
from dataclasses import dataclass, fields

import pytest

from shazzzbot import config, previewcfg, schema


def test_schema_names_cover_every_config_and_output_setting():
    internal = {"token", "jellyfin_key", "relays"}
    tables = {table for table, _ in config.PATHS.values()} - {"tasks"}
    flat = {name for name in schema.names() if name not in tables} | set(config.PATHS)
    assert flat == {field.name for field in fields(config.Config)} - internal
    assert set(schema.names("outputs")) == {field.name for field in fields(config.Output)}
    assert set(schema.names("preview")) == {field.name for field in fields(config.Preview)} | {"enabled"}


def test_names_and_defaults_are_complete_isolated_and_toml_shaped():
    assert schema.names() == tuple(schema.SCHEMAS["config"])
    values = schema.defaults()
    assert "guild_id" not in values and "helper" not in values and "outputs" not in values
    assert values["thresholds"]["disk_busy"] == 95
    values["order"].clear()
    values["thresholds"]["resource"][0] = 999
    assert schema.default("config", "order") == list(schema.SECTIONS)
    assert schema.defaults()["thresholds"]["resource"] == [80, 90]
    with pytest.raises(KeyError, match="has no default"):
        schema.default("config", "guild_id")


@pytest.mark.parametrize("provider", ["forgejo", "github"])
def test_provider_defaults_are_valid_and_independent(provider):
    values = schema.defaults("forges")
    values.update(type=provider, token_env="FORGE_TOKEN", repos=["example/project"])
    for key in ("name", "url", "show"):
        values[key] = schema.default("forges", key, discriminator=provider)
    schema.validate("forges", values)
    assert values["name"] == ("Forgejo" if provider == "forgejo" else "GitHub")
    assert ("runners" in values["show"]) == (provider == "forgejo")
    values["show"].clear()
    assert schema.default("forges", "show", discriminator=provider)


def test_all_config_defaults_validate_and_load(tmp_path, monkeypatch):
    values = schema.defaults()
    values["guild_id"] = 1
    values["outputs"] = [{"channel": 2}]
    schema.validate("config", values)
    outputs = values.pop("outputs")
    path = tmp_path / "config.toml"
    path.write_text(previewcfg.dumps(values) + "\n[[outputs]]\nchannel = 2\n")
    monkeypatch.setenv("DISCORD_TOKEN", "fixture")
    for key in list(os.environ):
        if key.startswith("RELAY_"):
            monkeypatch.delenv(key)
    cfg = config.load(str(path))
    assert cfg.outputs[0].channel == outputs[0]["channel"]
    assert cfg.thresholds == schema.threshold_defaults()
    assert cfg.tasks.long_running_minutes == schema.default("tasks", "long_running_minutes")


@pytest.mark.parametrize("section,required", [
    ("outputs", {"channel": 2}), ("service_check", {"name": "Local check"}), ("services", {}), ("system", {}),
    ("history", {}), ("host", {}), ("jellyfin", {}), ("ups", {}), ("netdata", {}), ("restarts", {}),
    ("helper", {"socket": "/run/helper.sock", "key_env": "HELPER_KEY"}),
    ("node_update", {"hostname": "example", "command": "true"}),
    ("preview", {}), ("tasks", {}), ("thresholds", {}), ("perms", {}), ("permission", {}),
])
def test_nested_defaults_validate(section, required):
    schema.validate(section, {**schema.defaults(section), **required})


@pytest.mark.parametrize("section,values,path,hint", [
    ("config", {"timezon": "x"}, "config", "timezone"),
    ("system", {"titlle": "x"}, "config.system", "title"),
    ("outputs", {"channe": 1}, "outputs[0]", "channel"),
    ("thresholds", {"disk_bus": 95}, "config.thresholds", "disk_busy"),
    ("config", {"not_a_setting": 1}, "config", None),
])
def test_unknown_names_fail_with_path_and_available_hint(section, values, path, hint):
    with pytest.raises(SystemExit) as caught:
        schema.unknown(section, values, path)
    assert str(caught.value).startswith(path + ": unknown setting")
    assert ("did you mean" in str(caught.value)) == (hint is not None)
    if hint:
        assert hint in str(caught.value)


@pytest.mark.parametrize("section,raw,message", [
    ("config", [], "expected a table"), ("config", {}, "missing required setting config_format"),
    ("outputs", {}, "missing required setting channel"),
    ("helper", {"socket": "/run/helper.sock", "key_env": "bad name"}, "expected string"),
    ("config", {"config_format": 1, "guild_id": 1, "outputs": [{"channel": 2}],
                "tasks": {"long_runnning_minutes": 1}}, "did you mean"),
])
def test_validation_reports_structural_and_nested_errors(section, raw, message):
    with pytest.raises(SystemExit, match=message):
        schema.validate(section, raw)


@pytest.mark.parametrize("section,raw", [
    ("text_map", {"Original": "Display"}), ("restart_groups", {"web": ["web*"]}),
    ("node_updates", {"bot": {"hostname": "example", "command": "true"}}),
])
def test_dynamic_tables_validate_values_without_restricting_names(section, raw):
    schema.unknown(section, raw, "config")
    schema.validate(section, raw)


@pytest.mark.parametrize("raw", [{"": "x"}, {" ": "x"}, {1: "x"}])
def test_dynamic_table_names_are_nonblank_text(raw):
    with pytest.raises(SystemExit, match="names must be non-empty text"):
        schema.validate("text_map", raw)


@pytest.mark.parametrize("spec,value", [
    (schema.text("Text."), ""), (schema.text("Text.", min_length=1, max_length=2), "ab"),
    (schema.text("Text.", pattern="[a-z]+", printable=True, nonblank=True), "abc"),
    (schema.integer("Count.", minimum=0, maximum=2), 0),
    (schema.integer("Count.", minimum=0, maximum=2), 2),
    (schema.flag("Flag."), True), (schema.flag("Flag."), False),
    (schema.Field("number", "Number."), math.inf),
    (schema.Field("number", "Number.", finite=True), 1.25),
    (schema.Field("duration", "Duration."), False), (schema.Field("duration", "Duration."), 0),
    (schema.Field("duration", "Duration."), 1.5),
    (schema.Field("threshold", "Threshold."), 95),
    (schema.Field("threshold", "Threshold."), [80, 90]),
    (schema.Field("list", "List."), [1]),
    (schema.sequence("List.", schema.NAME, min_length=1, max_length=2, unique=True), ["a", "b"]),
    (schema.table("Table.", "tasks"), {}),
    (schema.text("Optional.", nullable=True), None),
])
def test_generic_fields_accept_their_documented_values(spec, value):
    schema._value(spec, value, "fixture.value")


@pytest.mark.parametrize("minimum,maximum,value,expected", [
    (1, None, 0, "at least 1"), (None, 2, 3, "at most 2"), (1, 2, 3, "from 1 to 2"),
])
def test_numeric_range_errors_name_only_the_bounds_that_exist(minimum, maximum, value, expected):
    with pytest.raises(SystemExit) as error:
        schema._value(schema.integer("Count.", minimum=minimum, maximum=maximum), value, "fixture.value")
    assert str(error.value) == f"fixture.value: value must be {expected}"
    assert "None" not in str(error.value)


@pytest.mark.parametrize("spec,value,message", [
    (schema.text("Text."), 1, "expected string"),
    (schema.text("Text.", min_length=2), "a", "expected string"),
    (schema.text("Text.", max_length=1), "ab", "expected string"),
    (schema.text("Text.", pattern="[a-z]+"), "1", "expected string"),
    (schema.text("Text.", printable=True), "a\nb", "expected string"),
    (schema.text("Text.", nonblank=True), " ", "expected string"),
    (schema.integer("Count."), True, "expected integer"),
    (schema.integer("Count."), 1.0, "expected integer"),
    (schema.integer("Count.", minimum=1), 0, "value must be at least 1"),
    (schema.integer("Count.", maximum=1), 2, "value must be at most 1"),
    (schema.flag("Flag."), 1, "expected boolean"),
    (schema.Field("number", "Number."), True, "expected number"),
    (schema.Field("number", "Number."), "1", "expected number"),
    (schema.Field("number", "Number.", finite=True), math.inf, "expected number"),
    (schema.Field("number", "Number.", finite=True), math.nan, "expected number"),
    (schema.Field("number", "Number.", finite=True), 10**1000, "expected number"),
    (schema.Field("number", "Number.", minimum=1), 0.5, "value must be at least 1"),
    (schema.Field("number", "Number.", maximum=1), 1.5, "value must be at most 1"),
    (schema.Field("duration", "Duration."), True, "expected duration"),
    (schema.Field("duration", "Duration."), -1, "expected duration"),
    (schema.Field("duration", "Duration."), math.inf, "expected duration"),
    (schema.Field("threshold", "Threshold."), [90, 80], "expected threshold"),
    (schema.Field("threshold", "Threshold."), [80], "expected threshold"),
    (schema.Field("threshold", "Threshold."), [80, "90"], "expected threshold"),
    (schema.Field("threshold", "Threshold."), "80", "expected threshold"),
    (schema.sequence("List.", schema.NAME), "a", "expected list"),
    (schema.sequence("List.", schema.NAME, min_length=1), [], "expected a list of at least 1 item"),
    (schema.sequence("List.", schema.NAME, max_length=1), ["a", "b"], "expected a list of at most 1 item"),
    (schema.sequence("List.", schema.NAME), [1], "fixture.value"),
    (schema.sequence("List.", schema.NAME, unique=True), ["a", "a"], "items must be unique"),
    (schema.text("Choice.", choices=("a", "b")), "c", "choose from"),
    (schema.table("Table.", "tasks"), [], "expected a table"),
])
def test_generic_fields_fail_closed(spec, value, message):
    with pytest.raises(SystemExit, match=message):
        schema._value(spec, value, "fixture.value")


def test_unknown_schema_field_kind_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown schema field kind"):
        schema._value(schema.Field("typo", "Invalid schema kind."), None, "fixture.value")


def test_validate_field_checks_nested_items_by_default():
    with pytest.raises(SystemExit, match="services.checks.*expected a table"):
        schema.validate_field("services", "checks", ["not a service table"], "services.checks")
    schema.validate_field("services", "checks", [{"name": "example"}], "services.checks")


def test_validate_field_outer_only_keeps_type_and_parent_cap_checks():
    schema.validate_field("config", "forges", ["deferred nested validation"], "forges", items=False)
    with pytest.raises(SystemExit, match="expected list"):
        schema.validate_field("config", "forges", {}, "forges", items=False)
    with pytest.raises(SystemExit, match="expected a list of at most 3 items"):
        schema.validate_field("config", "forges", [{}] * 4, "forges", items=False)


def test_model_values_discards_internal_root_fields_but_keeps_nested_names():
    @dataclass
    class Service:
        name: str
        token: str

    @dataclass
    class Model:
        token: str
        services: tuple[Service, ...]

    result = schema.model_values(Model("internal", (Service("example", "nested"),)))
    assert result == {"services": [{"name": "example", "token": "nested"}]}
    assert schema.model_values(Service, root=False) is Service
    assert schema.model_values({"token": "kept"}, root=False) == {"token": "kept"}
