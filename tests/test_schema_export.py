import json
import re
import runpy
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from shazzzbot import config, schema, schemaview
from tools import schema_export

ROOT = Path(__file__).resolve().parent.parent
PATHS = {"config.example.toml", "docs/Configuration.md", "docs/demo/schema.json"}


def test_exports_are_deterministic_and_share_the_public_schema_document():
    first = schema_export.outputs()
    assert first == schema_export.outputs()
    assert set(first) == PATHS
    assert first["config.example.toml"] == schema_export.example()
    assert first["docs/Configuration.md"] == schema_export.reference()
    assert first["docs/demo/schema.json"] == schemaview.dumps()
    assert json.loads(first["docs/demo/schema.json"]) == schemaview.document()
    assert all(text.isascii() for text in first.values())
    for name in schema.names("config"):
        assert name in first["docs/Configuration.md"]


def test_committed_outputs_match_the_generator_byte_for_byte():
    for relative, expected in schema_export.outputs().items():
        assert (ROOT / relative).read_bytes() == expected.encode(), relative


def test_example_uses_real_defaults_and_validates_with_only_required_ids_replaced(tmp_path, monkeypatch):
    text = schema_export.example()
    raw = tomllib.loads(text)
    assert raw["guild_id"] == 0 and raw["outputs"][0]["channel"] == 0
    for key, field in schema.SCHEMAS["config"].items():
        if key in raw and field.kind in {"string", "integer", "boolean", "duration"} \
                and not field.required and field.default is not None:
            assert raw[key] == schema.default("config", key), key
    text = re.sub(r"(?m)^(guild_id|channel)\s*=\s*0\s*$", r"\1 = 1", text)
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    cfg = config.load(str(path))
    assert cfg.guild_id == 1 and cfg.outputs[0].channel == 1
    assert cfg.interval == schema.default("config", "interval")
    assert cfg.title == schema.default("system", "title")
    assert cfg.service_columns == schema.default("services", "columns")
    assert cfg.services == []


def test_new_metadata_fields_appear_automatically_without_generator_edits(monkeypatch):
    name = "schema_fixture_field"
    monkeypatch.setitem(schema.SCHEMAS["config"], name, schema.Field("integer", "Fixture field description.", 17))
    assert tomllib.loads(schema_export.example())[name] == 17
    assert name in schema_export.reference() and "Fixture field description." in schema_export.reference()
    assert name in schemaview.document()["sections"]["config"]


@pytest.mark.parametrize("field,expected", [
    (schema.Field("string", "Required choice.", required=True, choices=("first", "second")), "first"),
    (schema.Field("integer", "Required number.", required=True, minimum=7), 7),
    (schema.Field("list", "Required choices.", required=True,
                  item=schema.Field("string", "Choice.", choices=("first", "second"))), ["first", "second"]),
    (schema.Field("list", "Required list.", required=True), []),
    (schema.Field("string", "Required text.", required=True), "example"),
])
def test_new_required_fields_get_examples_without_handwritten_defaults(monkeypatch, field, expected):
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_sample", field)
    assert tomllib.loads(schema_export.example())["fixture_sample"] == expected


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_metadata_values_cannot_produce_a_config_example(monkeypatch, value):
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_sample", schema.Field("duration", "Invalid number.", value))
    with pytest.raises(ValueError, match="finite"):
        schema_export.example()


@pytest.mark.parametrize("value", [None, object()])
def test_unsupported_metadata_examples_fail_instead_of_emitting_invalid_toml(monkeypatch, value):
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_sample",
                        schema.Field("string", "Invalid example.", example=value))
    with pytest.raises(TypeError, match="unsupported"):
        schema_export.example()


def test_sensitive_defaults_and_examples_never_enter_any_export(monkeypatch):
    default = "private-default-fixture"
    example = "private-example-fixture"
    choice = "private-choice-fixture"
    child_choice = "private-child-choice-fixture"
    monkeypatch.setitem(schema.SCHEMAS["system"], "title",
                        replace(schema.SCHEMAS["system"]["title"], default=default, example=example,
                                choices=(choice,), sensitive=True))
    monkeypatch.setitem(schema.ENVIRONMENT, "DISCORD_TOKEN",
                        replace(schema.ENVIRONMENT["DISCORD_TOKEN"], default=default, example=example,
                                choices=(choice,)))
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_private_items",
                        schema.Field("list", "Private item metadata.", [],
                                     item=schema.Field("string", "Private item.", sensitive=True,
                                                       choices=(child_choice,))))
    for text in schema_export.outputs().values():
        assert all(value not in text for value in (default, example, choice, child_choice))


def test_reference_explains_declared_text_numeric_and_item_constraints(monkeypatch):
    text = schema.Field("string", "Text constraints.", "sample", choices=("sample", "second"),
                        pattern=r"[a-z]+", printable=True, nonblank=True, min_length=1, max_length=10)
    number = schema.Field("duration", "Number constraints.", 1.5, minimum=1, maximum=2, finite=True)
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_text", text)
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_number", number)
    monkeypatch.setitem(schema.SCHEMAS["config"], "fixture_items",
                        schema.Field("list", "Item constraints.", [], item=text, unique=True))
    reference = schema_export.reference()
    for constraint in ('Choices: `"sample"`, `"second"`.', "Pattern: `[a-z]+`.", "Printable text.",
                       "Must include a non-space character.", "Minimum length: 1.", "Maximum length: 10.",
                       "Minimum: 1.", "Maximum: 2.", "Finite number.", "Each item: choices:", "No duplicates.",
                       "Patterns must match the whole value."):
        assert constraint in reference


def test_reference_links_resolve_and_constraints_use_reader_facing_wording():
    reference = schema_export.reference()
    headings = re.findall(r"(?m)^#{1,3} (.+)$", reference)
    anchors = {re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-") for heading in headings}
    links = set(re.findall(r"\]\(#([^)]+)\)", reference))
    assert links and links <= anchors
    assert "Each item: see [service_check](#service_check)." in reference
    assert "Fields:" not in reference and "Item rules:" not in reference
    for name in schema.DYNAMIC:
        assert f"### {name}\n" in reference
    guild = next(line for line in reference.splitlines() if line.startswith("| `guild_id` |"))
    assert "| Required |" in guild and guild.count("Required") == 1
    token = next(line for line in reference.splitlines() if line.startswith("| `DISCORD_TOKEN` |"))
    assert "| Not shown |" in token and "Required." in token


def test_write_creates_complete_outputs_and_check_never_modifies_stale_or_missing_files(tmp_path):
    root = tmp_path / "generated"
    assert set(schema_export.check(root)) == PATHS
    assert not root.exists()
    schema_export.write(root)
    assert schema_export.check(root) == []
    assert {relative: (root / relative).read_text() for relative in PATHS} == schema_export.outputs()
    missing = root / "config.example.toml"
    stale = root / "docs" / "Configuration.md"
    missing.unlink()
    stale.write_text("stale\n")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert set(schema_export.check(root)) == {"config.example.toml", "docs/Configuration.md"}
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    schema_export.write(root)
    assert schema_export.check(root) == []


def test_cli_writes_then_checks_and_refuses_stale_outputs_without_writing(tmp_path, capsys):
    root = tmp_path / "cli"
    schema_export.main(["--out", str(root)])
    schema_export.main(["--check", "--out", str(root)])
    path = root / "config.example.toml"
    path.write_text("stale\n")
    with pytest.raises(SystemExit) as error:
        schema_export.main(["--check", "--out", str(root)])
    assert error.value.code == 1 and path.read_text() == "stale\n"
    captured = capsys.readouterr()
    assert "config.example.toml" in captured.out + captured.err


def test_script_entry_uses_the_same_check_path(monkeypatch):
    monkeypatch.setattr(sys, "argv", [schema_export.__file__, "--check", "--out", str(ROOT)])
    runpy.run_path(schema_export.__file__, run_name="__main__")
