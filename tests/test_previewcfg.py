import json
import logging
import os
import stat
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from shazzzbot import discordlog, panel, previewcfg
from shazzzbot.glyphs import GREEN, OK

BASE = previewcfg.BASE
LIVE = ('# my server\nguild_id = 1\norder = [\n  "system",\n  "disks",\n]\n\n[system]\ntitle = "Old"\n\n'
        '[[outputs]]\nchannel = 2\n\n[thresholds] # mine\ntemp = [60, 70]\n\n[perms.operate]\nusers = [5]\n')
SERVICES = ('guild_id = 1\n\n[services]\n# labels\ncolumns = ["L", "V", "P"]\npublic_host = "h.example"\n'
            'ignore = ["x*"]\n\n[[services.checks]]\nname = "A" # kept\ncontainer = "a"\n')


def test_dumps_writes_every_value_kind():
    values = {"title": "a \"b\"", "green_ok": True, "order": ["x"],
              "thresholds": {"temp": [1.5, float("inf")], "load": [float("-inf"), 2]},
              "tasks": {"shown": 3, "rename": {"odd key": "y"}}, "flag": False}
    text = previewcfg.dumps(values)
    assert tomllib.loads(text) == values and "[tasks.rename]\n" in text
    with pytest.raises(ValueError, match="NoneType"):
        previewcfg.dumps({"title": None})


def test_subset_keeps_only_the_display_settings():
    assert tomllib.loads(previewcfg.subset(LIVE)) == {"system": {"title": "Old"}, "order": ["system", "disks"],
                                                     "thresholds": {"temp": [60, 70]}}
    assert tomllib.loads(previewcfg.subset(SERVICES)) == {"services": {"columns": ["L", "V", "P"]}}
    assert previewcfg.subset('[services]\nignore = ["x"]\n') == ""


def test_schema_editor_fields_preserve_the_existing_write_boundary():
    assert set(previewcfg.KEYS) == {"system", "timezone", "green_ok", "order", "thresholds", "tasks", "services",
                                  "render"}
    assert set(previewcfg.TABLES) == {"system", "thresholds", "tasks", "render"}
    assert previewcfg.LEAVES == {"services": ("columns",)}
    assert set(previewcfg.ATTRS) == {"timezone", "green_ok", "order", "thresholds", "tasks", "render", "title",
                                   "subtitle", "tasks_shown", "tasks_ignore", "tasks_rename", "service_columns"}
    assert previewcfg.SECRETS >= {"password_hash", "DISCORD_TOKEN", "JELLYFIN_KEY", "RELAY_KEY"}


def test_splice_replaces_display_settings_and_keeps_the_rest():
    values = {"system": {"title": "New"}, "thresholds": {"load": [1, 2]},
              "tasks": {"long_running_minutes": 5, "rename": {"a": "B"}}}
    out = previewcfg.splice(LIVE, values)
    assert "# my server" in out and "users = [5]" in out and "Old" not in out and "disks" not in out
    assert tomllib.loads(out) == {"guild_id": 1, "outputs": [{"channel": 2}], "perms": {"operate": {"users": [5]}},
                                  **values}
    assert tomllib.loads(previewcfg.splice("[[outputs]]\nchannel = 2\n", {"timezone": "T"}))["timezone"] == "T"
    assert previewcfg.splice("timezone = \"x\"\n", {}) == "\n"


def test_splice_edits_only_the_editable_leaves_of_a_mixed_table():
    out = previewcfg.splice(SERVICES, {"services": {"columns": ["1", "2", "3"]}})
    assert "# labels" in out and "# kept" in out and '"L"' not in out
    assert tomllib.loads(out)["services"] == {"columns": ["1", "2", "3"], "public_host": "h.example",
                                              "ignore": ["x*"], "checks": [{"name": "A", "container": "a"}]}
    assert "columns" not in tomllib.loads(previewcfg.splice(SERVICES, {}))["services"]
    cleared = previewcfg.splice('guild_id = 1\n\n[services]\ncolumns = ["L", "V", "P"]\n', {})
    assert tomllib.loads(cleared) == {"guild_id": 1, "services": {}}
    added = previewcfg.splice('guild_id = 1\n\n[[services.checks]]\nname = "A"\n', {"services": {"columns": ["1"]}})
    assert tomllib.loads(added)["services"] == {"columns": ["1"], "checks": [{"name": "A"}]}


def test_splice_refuses_what_it_cannot_merge_exactly():
    with pytest.raises(ValueError, match="cannot merge safely"):
        previewcfg.splice(BASE, {"timezone": float("nan")})
    with pytest.raises(ValueError, match="end of a setting"):
        previewcfg.splice('timezone = "x\n[a]\n', {})


def diags(text, base=BASE):
    found, cfg = previewcfg.check(text, base)
    return [(d["line"], d["severity"], d["message"]) for d in found], cfg


def test_check_reports_toml_errors_at_their_line_and_column():
    found, cfg = previewcfg.check('title = "a"\norder = [1,,]\n', BASE)
    assert cfg is None and len(found) == 1
    assert found[0]["line"] == 2 and found[0]["col"] == 12 and found[0]["end_col"] > found[0]["col"]
    assert diags("x" * (previewcfg.MAX_TEXT + 1))[0][0][2].startswith("the settings are over")


@pytest.mark.parametrize("text", ['title = "a"\noops = [\n', 'oops =', 'oops =\n'])
def test_check_reports_errors_at_the_end_of_the_document(text):
    found, cfg = previewcfg.check(text, BASE)
    assert cfg is None and found[0]["severity"] == "error" and found[0]["end_col"] >= 2


@pytest.mark.parametrize("value", ['["Local"]', '["A", "B", "C", "D"]'])
def test_list_length_errors_name_the_count_and_hide_the_temporary_path(value):
    found, cfg = previewcfg.check(f"[services]\ncolumns = {value}\n", BASE)
    assert cfg is None and found[0]["line"] == 2
    assert found[0]["message"].startswith("[services].columns: expected a list of exactly 3 items")
    assert "/" not in found[0]["message"].split(";")[0]


def test_check_rejects_settings_the_page_may_not_edit():
    found, cfg = diags('timezone = "UTC"\nguild_id = 5\ntoken = "x"\n')
    assert cfg is None
    assert found[0] == (2, "error", "guild_id cannot be edited here; change it in config.toml")
    assert found[1][:2] == (3, "error") and found[1][2].startswith("unknown setting 'token';")
    assert diags("[system]\ntitle = nan\n")[0] == [
        (1, "error", "config.toml has a layout this editor cannot merge safely; edit it by hand")]
    found, cfg = diags('[services]\ncolumns = ["a", "b", "c"]\npublic_host = "x"\n')
    assert cfg is None
    assert found == [(3, "error", "services.public_host cannot be edited here; change it in config.toml")]
    found, cfg = diags('services = 1\n')
    assert cfg is None and found == [(1, "error", "services must be a table")]
    base = 'config_format = 1\n' + SERVICES.replace('public_host = "h.example"\n', '') + '[[outputs]]\nchannel = 2\n'
    found, cfg = diags('[services]\ncolumns = ["a", "b", "c"]\n', base)
    assert found == [] and cfg.service_columns == ["a", "b", "c"] and cfg.services_ignore == ["x*"]


def test_check_names_a_misspelled_setting_and_suggests_the_real_one():
    found, _ = diags('green_ok = true\n\ntimezon = "b"\nzzz = 1\n')
    assert found[0] == (3, "error", "unknown setting 'timezon'; did you mean 'timezone'?")
    assert found[1][0] == 4 and found[1][2].startswith("unknown setting 'zzz'; this editor accepts timezone, ")
    found, _ = diags('[system]\ntitel = "a"\n')
    assert found[0][0] == 2 and "did you mean 'title'" in found[0][2]
    found, _ = diags('[thresholds]\ncpu_pctt = 1\n')
    assert found[0][0] == 2 and "unknown setting 'cpu_pctt'" in found[0][2]


def test_check_reports_the_config_loaders_own_messages_at_the_setting():
    found, cfg = diags('green_ok = true\n\n[tasks]\nheavy_kinds = ["nope"]\n')
    assert cfg is None and found[0][0] == 4 and found[0][1] == "error" and "heavy_kinds" in found[0][2]
    found, cfg = diags('timezone = "Nowhere/Zone"\n')
    assert cfg is None and found[0][0] == 1 and "timezone" in found[0][2]
    found, cfg = diags('[system]\ntitle = "a"\nsubtitle = "b"\n')
    assert found == [] and cfg.title == "a" and cfg.subtitle == "b"


def test_check_reports_loader_warnings_and_ignores_other_logs(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "real")
    load = previewcfg.config.load

    def warning_load(path):
        cfg = load(path)
        previewcfg.log.handle(logging.LogRecord("shazzzbot", logging.WARNING, "config.py", 1,
                                              "title: fixture warning", None, None))
        return cfg

    monkeypatch.setattr(previewcfg.config, "load", warning_load)
    found, cfg = diags('[system]\ntitle = "a"\n', "config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\n")
    assert cfg is not None and os.environ["DISCORD_TOKEN"] == "real"
    assert [(f[1], f[2]) for f in found] == [("warning", "title: fixture warning")]
    handler = previewcfg._Warnings()
    rec = logging.LogRecord("shazzzbot", logging.WARNING, "bot.py", 1, "other", None, None)
    handler.emit(rec)
    rec = logging.LogRecord("shazzzbot", logging.WARNING, "config.py", 1, "elsewhere", None, None)
    worker = threading.Thread(target=handler.emit, args=(rec,))
    worker.start()
    worker.join()
    assert handler.messages == []


def test_diagnostics_fall_back_to_the_first_line():
    assert previewcfg._where("", "no match") == (1, 1)
    assert previewcfg._where("a = 1\n", "thresholds.temp is wrong") == (1, 6)
    assert previewcfg._where("[thresholds]\ntemp = 1\n", "thresholds.temp is wrong") == (2, 9)
    d = previewcfg._diag("abc\n", "m", line=1, col=2)
    assert (d["line"], d["col"], d["end_col"]) == (1, 2, 4)


def test_apply_updates_a_running_bot():
    _, cfg = previewcfg.check('timezone = "America/Los_Angeles"\ngreen_ok = true\n[system]\ntitle = "T"\n'
                              '[render]\nsystem = "text"\ndisks = "embed"\n', BASE)
    boards = [SimpleNamespace(render={}), SimpleNamespace(render={})]
    outputs = [SimpleNamespace(render={"disks": "image"}), SimpleNamespace(render={})]
    bot = SimpleNamespace(cfg=SimpleNamespace(outputs=outputs), boards=boards, tz=None,
                          rate_budget=discordlog.Budget())
    for _ in range(3):
        bot.rate_budget.auth.finish(bot.rate_budget.auth.begin("/channels/2/messages"), 403)
    assert bot.rate_budget.auth.blocked(2)
    try:
        previewcfg.apply(bot, cfg)
        assert bot.cfg.title == "T" and str(bot.tz) == "America/Los_Angeles" and panel.SLOT[0] == GREEN
        assert not bot.rate_budget.auth.blocked(2)
        assert [b.render for b in boards] == [{"system": "text", "disks": "image"},
                                              {"system": "text", "disks": "embed"}]
    finally:
        panel.use_ok_glyph(OK)


@pytest.mark.parametrize("mode", [0o640, 0o666])
def test_write_replaces_atomically_and_makes_the_file_private(tmp_path, monkeypatch, mode):
    p = tmp_path / "config.toml"
    p.write_text("old\n")
    p.chmod(mode)
    previewcfg.write(str(p), "new\n")
    assert p.read_text() == "new\n" and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [x.name for x in tmp_path.iterdir()] == ["config.toml"]

    def fail(*_a):
        raise OSError("busy")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        previewcfg.write(str(p), "lost\n")
    assert p.read_text() == "new\n" and [x.name for x in tmp_path.iterdir()] == ["config.toml"]


@pytest.mark.parametrize("mode", [0o640, 0o666])
def test_write_rewrites_privately_in_place_when_the_folder_is_not_writable(tmp_path, monkeypatch, mode):
    p = tmp_path / "config.toml"
    p.write_text("a much longer old text\n")
    p.chmod(mode)

    def no_temp(**_kw):
        raise OSError("read-only folder")
    monkeypatch.setattr(previewcfg.tempfile, "mkstemp", no_temp)
    previewcfg.write(str(p), "short\n")
    assert p.read_text() == "short\n" and stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.parametrize("in_place", [False, True])
def test_write_refuses_to_change_content_when_private_permissions_cannot_be_set(tmp_path, monkeypatch, in_place):
    p = tmp_path / "config.toml"
    p.write_text("old\n")
    p.chmod(0o666)

    def fail(*_args, **_kwargs):
        raise OSError("permissions denied")

    if in_place:
        monkeypatch.setattr(previewcfg.tempfile, "mkstemp", fail)
    monkeypatch.setattr(previewcfg.os, "fchmod", fail)
    with pytest.raises(OSError, match="permissions denied"):
        previewcfg.write(str(p), "new\n")
    assert p.read_text() == "old\n" and stat.S_IMODE(p.stat().st_mode) == 0o666
    assert [file.name for file in tmp_path.iterdir()] == ["config.toml"]


def test_secrets_are_masked_and_the_sentinel_keeps_clears_or_replaces_them():
    S = previewcfg.SENTINEL
    old = {"title": "t", "preview": {"user": "u", "password_hash": "scrypt$real"}}
    shown = previewcfg.mask(old)
    assert shown["preview"] == {"user": "u", "password_hash": S} and "scrypt" not in repr(shown)
    assert previewcfg.mask({"preview": {"password_hash": ""}})["preview"]["password_hash"] == ""
    assert previewcfg.unmask(shown, old) == old
    assert previewcfg.unmask({"preview": {"user": "u", "password_hash": ""}}, old) == {"preview": {"user": "u"}}
    new = previewcfg.unmask({"preview": {"password_hash": "scrypt$new"}}, old)
    assert new["preview"]["password_hash"] == "scrypt$new"
    with pytest.raises(ValueError, match="keep it exactly"):
        previewcfg.unmask({"preview": {"password_hash": "x" + S}}, old)
    real = 'title = "t"\n\n[preview]\nuser = "u"\npassword_hash = "scrypt$real"\n'
    assert "scrypt$real" not in previewcfg.subset(real)


@pytest.mark.parametrize("old", [{}, {"preview": {"user": "viewer"}}])
def test_stale_secret_sentinel_does_not_create_a_missing_stored_password(old):
    submitted = {"preview": {"user": "viewer", "password_hash": previewcfg.SENTINEL}}
    before = repr(old)
    result = previewcfg.unmask(submitted, old)
    assert result == {"preview": {"user": "viewer"}}
    assert previewcfg.SENTINEL not in repr(result) and repr(old) == before
    assert submitted["preview"]["password_hash"] == previewcfg.SENTINEL


def test_schema_secrets_are_redacted_inside_arbitrarily_nested_lists_before_container_recursion():
    secret = "private-fixture-value"
    old = {"records": [{"password_hash": secret, "children": [{"DISCORD_TOKEN": secret},
                                                              [{"RELAY_KEY": secret}, "visible"]]},
                       {"JELLYFIN_KEY": {"inner": [secret]}}, {"password_hash": [secret]}],
           "empty": [{"password_hash": "", "DISCORD_TOKEN": None, "JELLYFIN_KEY": [], "RELAY_KEY": {}}]}
    shown = previewcfg.mask(old)
    assert secret not in repr(shown)
    assert shown == {"records": [{"password_hash": previewcfg.SENTINEL,
                                  "children": [{"DISCORD_TOKEN": previewcfg.SENTINEL},
                                               [{"RELAY_KEY": previewcfg.SENTINEL}, "visible"]]},
                                {"JELLYFIN_KEY": previewcfg.SENTINEL}, {"password_hash": previewcfg.SENTINEL}],
                     "empty": [{"password_hash": "", "DISCORD_TOKEN": "", "JELLYFIN_KEY": "", "RELAY_KEY": ""}]}
    assert old["records"][0]["password_hash"] == secret


@pytest.mark.parametrize("name", ["password_hash", "DISCORD_TOKEN", "JELLYFIN_KEY", "RELAY_KEY"])
def test_schema_sensitive_fields_keep_existing_sentinel_semantics(name):
    old = {"nested": {name: "stored-fixture"}}
    assert previewcfg.unmask(previewcfg.mask(old), old) == old
    assert previewcfg.unmask({"nested": {name: ""}}, old) == {"nested": {}}
    assert previewcfg.unmask({"nested": {name: "replacement"}}, old) == {"nested": {name: "replacement"}}
    with pytest.raises(ValueError, match="keep it exactly"):
        previewcfg.unmask({"nested": {name: previewcfg.SENTINEL + "suffix"}}, old)


def test_every_example_config_is_clean_and_the_sample_is_the_demo():
    demo = Path(__file__).resolve().parents[1] / "docs" / "demo"
    examples = json.loads((demo / "examples.json").read_text())
    assert [e["name"] for e in examples] and all(e["help"] for e in examples)
    for e in examples:
        diagnostics, cfg = previewcfg.check((demo / e["file"]).read_text(), BASE)
        assert diagnostics == [] and cfg is not None, e["file"]
    assert (demo / "examples" / "sample.toml").read_text() == (demo / "demo.toml").read_text()
