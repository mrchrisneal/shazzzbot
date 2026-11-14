import math
import tomllib

import pytest

from shazzzbot import schema
from tools import setup_schema


def test_audit_preserves_original_bytes_and_reports_only_inactive_defaults():
    original = (b'config_format = 1\r\n# Keep this comment exactly.\r\nguild_id = 1  # same spacing\r\n'
                b'[system]\r\nsubtitle = """First line\r\n# default interval = 10\r\n[not_a_table]\r\nLast line"""\r\n'
                b'[[outputs]]\r\nchannel = 2\r\n[preview]\r\npassword_hash = "private-fixture"\r\n')
    report = setup_schema.audit(original)
    assert not report.errors
    assert report.candidate.startswith(original)
    assert b'# default interval = 10\r\n' in report.candidate[len(original):]
    assert tomllib.loads(report.candidate.decode()) == tomllib.loads(original.decode())
    assert "private-fixture" not in report.summary()
    assert "password_hash" not in report.summary()
    assert "+ interval = 10 (commented)" in report.summary()
    assert b"\n" not in report.candidate[len(original):].replace(b"\r\n", b"")
    repeated = setup_schema.audit(report.candidate)
    assert not repeated.errors and not repeated.additions
    assert repeated.candidate == report.candidate
    assert repeated.summary() == "Config is current."


def test_audit_collects_every_unknown_type_range_and_missing_error():
    original = (b'config_format = 1\ninterval = "wrong"\nedit_every = 2\n[history]\ndays = 8\n'
                b'[system]\ntitlle = "private-fixture"\n[[outputs]]\nchanne = 3\nrender = "wrong"\n'
                b'[tasks]\nlong_runnning_minutes = 8\nheavy_kinds = ["unknown"]\n')
    report = setup_schema.audit(original)
    assert len(report.errors) == 10
    joined = report.summary()
    assert "missing required setting guild_id" in joined
    assert "did you mean 'title'" in joined
    assert "missing required setting channel" in joined
    assert "interval: expected integer" in joined
    assert "edit_every: value must be at least 5" in joined
    assert "days: value must be from 1 to 7" in joined
    assert "outputs[0].render: expected a table" in joined
    assert "private-fixture" not in joined
    assert report.candidate == original


def test_nested_dynamic_table_errors_and_default_values_follow_the_schema():
    report = setup_schema.audit(
        b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]\n[restarts.groups]\n"" = ["x"]\nbad = []\n'
        b'[node_updates.bot]\nhostname = "example"\ncommand = "true"\n'
        b'[[forges]]\ntype = "github"\ntoken_env = "FORGE_KEY"\nrepos = ["example/repository"]\n')
    assert len(report.errors) == 2
    values = {addition.path: addition.value for addition in report.additions}
    assert values["forges[0].url"] == '"https://api.github.com"'
    assert values["forges[0].name"] == '"GitHub"'
    assert "runners" not in values["forges[0].show"]
    assert "node_updates.bot.run_on" not in values
    assert "[" not in values["forges[0].filter"]


@pytest.mark.parametrize("original", [b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]',
                                      b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]\n',
                                      b'config_format = 1\r\nguild_id=1\r\noutputs=[{channel=2}]\r\n'])
def test_catalogue_works_with_each_line_ending(original):
    report = setup_schema.audit(original)
    assert report.candidate.startswith(original)
    assert not setup_schema.audit(report.candidate).additions


def test_catalogue_can_acknowledge_multiple_schema_updates(monkeypatch):
    monkeypatch.setitem(schema.SCHEMAS, "config", {"config_format": schema.SCHEMAS["config"]["config_format"],
                                                "one": schema.integer("One.", 1)})
    first = setup_schema.audit(b"config_format = 1\n").candidate
    monkeypatch.setitem(schema.SCHEMAS["config"], "two", schema.integer("Two.", 2))
    second = setup_schema.audit(first)
    assert [addition.path for addition in second.additions] == ["two"]
    assert not setup_schema.audit(second.candidate).additions


@pytest.mark.parametrize("original", [b'guild_id = "unclosed', b"\xff"])
def test_invalid_toml_has_no_replacement_or_value_disclosure(original):
    report = setup_schema.audit(original)
    assert report.errors == ("config: invalid TOML",)
    assert not report.additions
    assert report.candidate == original


def test_no_optional_defaults_or_schema_mutations_are_invented(monkeypatch):
    fields = {"config_format": schema.SCHEMAS["config"]["config_format"],
              "required": schema.integer("Required.", required=True),
              "secret": schema.text("Secret.", "private-fixture", sensitive=True),
              "absent": schema.text("No default.", schema.MISSING),
              "nullable": schema.text("Nullable.", None, nullable=True)}
    monkeypatch.setitem(schema.SCHEMAS, "config", fields)
    missing = setup_schema.audit(b"config_format = 1\n")
    assert missing.errors == ("config: missing required setting required",)
    assert not missing.additions and missing.candidate == b"config_format = 1\n"
    present = setup_schema.audit(b"config_format = 1\nrequired = 1\n")
    assert not present.errors and not present.additions
    assert present.summary() == "Config is current."


def test_field_handles_nullable_values_and_untyped_lists():
    errors, additions = [], []
    setup_schema._field(schema.text("Optional.", None, nullable=True), None, "optional", errors, additions)
    setup_schema._field(schema.Field("list", "Unrestricted list."), [1], "list", errors, additions)
    setup_schema._field(schema.sequence("Typed list.", schema.NAME), "wrong", "wrong", errors, additions)
    assert len(errors) == 1 and not additions


def test_dynamic_quoted_names_remain_escaped_in_default_paths():
    report = setup_schema.audit(
        b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]\n'
        b'[node_updates."odd.name"]\nhostname="example"\ncommand="true"\n')
    assert not report.errors
    assert not any(item.path.startswith("node_updates") for item in report.additions)
    assert setup_schema._key("odd.name") == '"odd.name"'


def test_invalid_unhashable_items_report_errors_instead_of_crashing():
    report = setup_schema.audit(b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]\n'
                                b'order=[{invalid="item"}, "system", "system"]\n')
    assert len(report.errors) == 1
    assert "order[0]: expected string" in report.errors[0]
    duplicate = setup_schema.audit(b'config_format = 1\nguild_id=1\noutputs=[{channel=2}]\n'
                                   b'order=["system", "system"]\n')
    assert duplicate.errors == ("order: items must be unique",)


def test_default_formatter_is_finite_and_toml_compatible():
    value = {"key.name": [True, False, "quoted\\value", 3, 0.5], "empty": {}}
    encoded = setup_schema._toml(value)
    assert tomllib.loads("value = " + encoded)["value"] == value
    with pytest.raises(ValueError, match="finite"):
        setup_schema._toml(math.inf)
    with pytest.raises(TypeError, match="unsupported"):
        setup_schema._toml(None)


def test_catalogue_handles_only_its_comment_lines_and_empty_input():
    line = "# default interval = 10"
    assert setup_schema._catalogue("") == set()
    assert setup_schema._catalogue(setup_schema.HEADER + "\n" + line + "\n") == {line}
