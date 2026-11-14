import json
import sys
import tomllib
from pathlib import Path

import pytest

from tools import setup_config as sc

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "tools"))
import setup_steps as steps

GUILD, ROLE, OWNER = "123456789012345678", "223456789012345678", "323456789012345678"
PROBE = {"timezone": "America/Los_Angeles", "ups": ["ups@198.51.100.2:3493"], "netdata": "http://198.51.100.2:19999",
         "jellyfin": "http://198.51.100.2:8096",
         "containers": [{"name": "nextcloud", "webui": "http://198.51.100.2:8443/"},
                        {"name": "immich", "webui": "ftp://bad"}, {"name": "mariadb", "webui": ""},
                        {"name": "bad name", "webui": ""}, "junk", {"name": "home\x1b", "webui": ""}]}
ENVS = ("SHAZZZBOT_PROBE", "SHAZZZBOT_TIMEZONE", "SHAZZZBOT_UPS", "SHAZZZBOT_NETDATA_URL", "SHAZZZBOT_JELLYFIN_URL",
        "SHAZZZBOT_JELLYFIN_KEY", "SHAZZZBOT_RESTART_ALLOW", "SHAZZZBOT_PERMS_ADMIN_ROLES",
        "SHAZZZBOT_PERMS_OPERATE_ROLES", "SHAZZZBOT_PERMS_VIEW_ROLES", "JELLYFIN_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)


def example():
    return (ROOT / "config.example.toml").read_text().splitlines(), (ROOT / ".env.example").read_text().splitlines()


def probe_file(tmp_path, monkeypatch, data):
    path = tmp_path / "probe.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    monkeypatch.setenv("SHAZZZBOT_PROBE", str(path))


def feed(monkeypatch, answers):
    replies = iter(answers)
    monkeypatch.setattr(sc, "input_line", lambda label: next(replies) + "\n")
    monkeypatch.setattr(sc.getpass, "getpass", lambda label: next(replies))


def lookups(monkeypatch, roles):
    calls = []

    def lookup(token, argv):
        calls.append(argv)
        return roles if argv[0] == "roles" else [{"id": OWNER}]
    monkeypatch.setattr(sc, "lookup", lookup)
    return calls


def test_probe_reads_only_a_json_object(tmp_path, monkeypatch):
    assert steps.probe() == {}
    probe_file(tmp_path, monkeypatch, "[1]")
    assert steps.probe() == {}
    probe_file(tmp_path, monkeypatch, "{bad")
    assert steps.probe() == {}
    monkeypatch.setenv("SHAZZZBOT_PROBE", str(tmp_path / "missing"))
    assert steps.probe() == {}
    assert steps.containers({"containers": "x"}) == []
    assert [c["name"] for c in steps.containers(PROBE)] == ["nextcloud", "immich", "mariadb"]


@pytest.mark.parametrize(("url", "ok"), [("http://h:1", True), ("https://h/p", True), ("ftp://h", False),
                                         ("http://u@h", False), ("http://h?q", False), ("h:1", False),
                                         ("http://[broken", False), ("http://h:port", False), ("http://", False)])
def test_http_check(url, ok):
    assert (steps.http(url) is None) == ok


def test_prompted_run_takes_every_detected_value(tmp_path, monkeypatch, capsys):
    probe_file(tmp_path, monkeypatch, PROBE)
    calls = lookups(monkeypatch, [{"id": ROLE, "name": "Mods"}])
    # time zone, UPS, Netdata, Jellyfin, Jellyfin key, restarts, admin/operate/view roles, owner, /blowup, checks
    feed(monkeypatch, ["", "", "y", "", "jf-key", "1,3", "1", "", "1", "", "y", "1"])
    cfg, env = example()
    detected = steps.run(sc, sc.Wizard(True), cfg, env, {".env", "config.toml"}, "t", GUILD)
    data = tomllib.loads("\n".join(cfg))
    assert data["timezone"] == "America/Los_Angeles" and data["ups"] == {"target": "ups@198.51.100.2:3493"}
    assert data["netdata"]["url"] == "http://198.51.100.2:19999" and data["jellyfin"]["url"] == "http://198.51.100.2:8096"
    assert data["restarts"]["allow"] == ["nextcloud", "mariadb"] and data["blowup_user_id"] == int(OWNER)
    assert data["perms"]["admin"] == {"roles": [int(ROLE)], "users": [int(OWNER)]}
    assert data["perms"]["view"]["roles"] == [int(ROLE)] and data["perms"]["operate"]["roles"] == []
    assert "JELLYFIN_KEY=jf-key" in env
    assert detected == [{"name": "nextcloud", "container": "nextcloud", "url": "http://198.51.100.2:8443/"}]
    assert calls == [["roles", GUILD], ["owner", GUILD]]
    err = capsys.readouterr().err
    assert "immich  ftp" not in err and "must reach" in err and "jf-key" not in err


def test_declined_and_missing_values_change_nothing(tmp_path, monkeypatch, capsys):
    probe_file(tmp_path, monkeypatch, {**PROBE, "timezone": "Mars/Base", "netdata": 5, "jellyfin": "http://u@h"})
    lookups(monkeypatch, None)
    # time zone, UPS no, Netdata URL, Jellyfin URL, restarts none, no service check
    feed(monkeypatch, ["", "n", "", "", "", ""])
    cfg, env = example()
    before = list(cfg)
    assert steps.run(sc, sc.Wizard(True), cfg, env, {".env", "config.toml"}, "t", GUILD) == []
    assert cfg == before
    err = capsys.readouterr().err
    assert "wiki/Configuration#permissions" in err and "Installation#optional-netdata" in err


def test_unattended_uses_only_environment_values(monkeypatch, capsys):
    monkeypatch.setenv("SHAZZZBOT_NETDATA_URL", "http://n:19999")
    monkeypatch.setenv("SHAZZZBOT_JELLYFIN_URL", "http://j:8096")
    monkeypatch.setenv("SHAZZZBOT_JELLYFIN_KEY", "k")
    monkeypatch.setenv("SHAZZZBOT_RESTART_ALLOW", "web, db*")
    monkeypatch.setenv("SHAZZZBOT_PERMS_OPERATE_ROLES", f"{ROLE} {OWNER}")
    monkeypatch.setattr(sc, "lookup", lambda *a: pytest.fail("unattended: no Discord lookup"))
    cfg, env = example()
    assert steps.run(sc, sc.Wizard(False), cfg, env, {"config.toml"}, "t", GUILD) == []
    data = tomllib.loads("\n".join(cfg))
    assert data["netdata"]["url"] == "http://n:19999" and data["jellyfin"]["url"] == ""
    assert data["restarts"]["allow"] == ["web", "db*"] and "ups" not in data
    assert data["perms"]["operate"]["roles"] == [int(ROLE), int(OWNER)]
    assert "JELLYFIN_KEY=k" not in env


def test_bad_role_ids_stop_the_wizard(monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_PERMS_ADMIN_ROLES", "12")
    cfg, env = example()
    with pytest.raises(SystemExit, match="17 to 20 digits"):
        steps.run(sc, sc.Wizard(False), cfg, env, set(), "", GUILD)


def test_set_line_respects_the_section():
    lines = ["[a]", "url = 1", "[b]", "x = 1"]
    sc.set_line(lines, "url = ", "url = 2", section="[a]")
    assert lines[1] == "url = 2"
    with pytest.raises(SystemExit, match="in \\[b\\]"):
        sc.set_line(lines, "url = ", "url = 3", section="[b]")
    with pytest.raises(SystemExit, match="in \\[c\\]"):
        sc.set_line(lines, "url = ", "url = 3", section="[c]")
    with pytest.raises(SystemExit, match="in \\[a\\]"):
        sc.set_line(["[a]", "x = 1", "[b]", "url = 1"], "url = ", "url = 2", section="[a]")


def test_menu_without_manual_entry_treats_m_as_invalid(monkeypatch):
    replies = iter(["m", "1"])
    monkeypatch.setattr(sc, "input_line", lambda label: next(replies))
    assert sc.menu("t", [{"id": "x", "name": "x"}], manual=False) == ["x"]


def test_jellyfin_without_a_key_is_skipped(monkeypatch, capsys):
    monkeypatch.setenv("SHAZZZBOT_JELLYFIN_URL", "http://j:8096")
    _cfg, env = example()
    assert steps.jellyfin(sc, sc.Wizard(False), {}, env, {".env"}) == ""
    assert "Jellyfin skipped" in capsys.readouterr().err


def test_detected_check_that_fails_validation_is_skipped(monkeypatch, capsys):
    from shazzzbot import services

    def reject(_checks):
        raise ValueError("bad check")
    monkeypatch.setattr(services, "validate", reject)
    feed(monkeypatch, ["1"])
    found = [{"name": "nextcloud", "webui": "http://198.51.100.2:8443/"}]
    assert steps.checks(sc, sc.Wizard(True), found) == []
    assert "skipped nextcloud: bad check" in capsys.readouterr().err
