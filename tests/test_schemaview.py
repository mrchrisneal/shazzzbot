import json
import math

import pytest

from shazzzbot import __version__, schema, schemaview


def test_metadata_is_generated_from_fields_without_runtime_values(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "runtime-secret-not-in-metadata")
    value = schemaview.document()
    assert value["format"] == 1 and value["version"] == __version__ and value["root"] == "config"
    assert value["editor"]["keys"] == list(schemaview.editable())
    assert value["editor"]["tables"] == list(schemaview.tables())
    assert set(value["sections"]) == set(schema.SCHEMAS)
    assert set(value["dynamic"]) == set(schema.DYNAMIC)
    assert set(value["environment"]) == set(schema.ENVIRONMENT)
    assert "runtime-secret-not-in-metadata" not in schemaview.dumps()
    assert json.loads(schemaview.dumps()) == value
    assert schemaview.dumps() == schemaview.dumps()


def test_every_reference_resolves_and_editor_permissions_are_unchanged():
    assert set(schemaview.editable()) == {"system", "timezone", "green_ok", "order", "thresholds", "tasks",
                                         "services", "render"}
    assert set(schemaview.tables()) == {"system", "thresholds", "tasks", "render"}
    assert schemaview.leaves() == {"services": ("columns",)}
    assert schemaview.sensitive() == {"password_hash", "DISCORD_TOKEN", "JELLYFIN_KEY", "RELAY_KEY"}
    value = schemaview.document()
    for section in value["sections"].values():
        for spec in section.values():
            ref = spec["ref"]
            assert not ref or ref in value["sections"] or ref in value["dynamic"]


def test_sensitive_defaults_examples_and_choices_never_leave_the_schema():
    spec = schema.text("Private setting.", "secret-default", sensitive=True, example="secret-example",
                       choices=("secret-choice",), default_by={"kind": "secret-provider"})
    value = schemaview.describe(spec)
    assert value["sensitive"] and value["description"] == "Private setting."
    assert not {"default", "example", "default_by", "choices"}.intersection(value)
    assert "secret-" not in json.dumps(value)
    required = schemaview.describe(schema.text("Required.", schema.MISSING, required=True))
    assert "default" not in required and "example" not in required


def test_provider_metadata_does_not_publish_invalid_internal_fallback_defaults():
    value = schemaview.describe(schema.SCHEMAS["forges"]["name"])
    assert "default" not in value
    assert value["default_by"] == {"forgejo": "Forgejo", "github": "GitHub"}


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, {"nested": [math.inf]}])
def test_nonfinite_metadata_is_refused(value):
    with pytest.raises(ValueError, match="finite"):
        schemaview._data(value)


@pytest.mark.parametrize("value", [object(), b"bytes", {1: "non-text key"}])
def test_unknown_metadata_types_fail_closed(value):
    with pytest.raises(TypeError, match="unsupported"):
        schemaview._data(value)


def test_metadata_copies_are_independent_and_json_numbers_are_finite():
    value = schemaview.document()
    value["sections"]["config"]["order"]["default"].append("not-a-section")
    assert "not-a-section" not in schemaview.document()["sections"]["config"]["order"]["default"]
    assert schemaview._data([None, "text", True, 1, 1.5]) == [None, "text", True, 1, 1.5]
    assert "Infinity" not in schemaview.dumps() and "NaN" not in schemaview.dumps()
