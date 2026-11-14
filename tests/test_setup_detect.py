import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import setup_detect as D
from tools.setup_manage import managed_names

IMAGE = "ghcr.io/mrchrisneal/shazzzbot"
ID = "a" * 32
MACHINE = "b" * 32


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value)
    path.chmod(0o600)


def row(app, *, proxy=False, owner=ID, running=True, version="1.2.3", image=IMAGE + ":v1.2.3"):
    return {"id": "2" * 12 if proxy else "1" * 12, "name": "any-proxy" if proxy else "any-bot-name",
            "image": image, "running": running, "proxy": proxy, "mounts": [], "labels": {
                "org.shazzzbot.install": owner, "org.opencontainers.image.source": D.SOURCE,
                "org.opencontainers.image.version": version, "com.docker.compose.project": "custom_project",
                "com.docker.compose.project.working_dir": str(app),
                "com.docker.compose.service": "docker-proxy" if proxy else "shazzzbot"}}


@pytest.fixture
def install(tmp_path, monkeypatch):
    app, helper, hook, marker, machine = (tmp_path / name for name in
                                        ("app", "helper", "user.scripts/scripts/shazzzbot-helper", "unraid", "machine"))
    write(machine, MACHINE)
    write(marker, "version=7.3.4")
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    rows = [row(app), row(app, proxy=True)]
    monkeypatch.setattr(D, "containers", lambda _image: rows)

    def prepare(method="compose-image", role="bot", ref="v1.2.3"):
        names = ((".env", "config.toml") if role == "bot" else (".env",)) + managed_names(role, method)
        for name in names:
            write(app / name, "NEVER_PRINT_THIS_SECRET" if name == ".env" else "fixture\n")
        identity = {"format": 2, "host": MACHINE, "app": str(app), "install_id": ID,
                    "role": role, "method": method, "ref": ref,
                    "files": {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in names}}
        write(app / ".setup-identity", json.dumps(identity))
        write(app / ".setup-state", json.dumps({"format": 1, "method": method, "role": role, "ref": ref}))
        return identity

    def detect(latest="v1.2.3", role="bot", explicit=True):
        return D.detect(str(app) if explicit else "", str(helper), str(hook), IMAGE, latest, role, str(marker))

    return SimpleNamespace(app=app, helper=helper, hook=hook, marker=marker, rows=rows,
                           prepare=prepare, detect=detect)


def test_fresh_detection_has_no_side_effects_or_release_request(install, monkeypatch):
    monkeypatch.setattr(D, "latest_release", lambda _url: pytest.fail("fresh install must not query releases"))
    install.rows.clear()
    before = sorted(install.app.parent.rglob("*"))
    report = install.detect()
    assert report["state"] == "not-installed" and report["summary"] == "Shazzzbot: not installed"
    assert report["platform"] == "unraid" and not report["current"]
    assert report["app"] == report["role"] == report["method"] == report["ref"] == ""
    assert sorted(install.app.parent.rglob("*")) == before
    assert "Helper: not installed" in report["lines"]
    assert "platform\tunraid" in D.emit(report) and "current\t0" in D.emit(report)


def test_owned_any_name_install_is_current_and_never_exposes_environment(install):
    install.prepare()
    report = install.detect()
    assert report["state"] == "installed" and report["current"]
    assert report["app"] == str(install.app) and report["role"] == "bot"
    assert report["summary"] == "Shazzzbot: installed v1.2.3 (current)"
    assert "Compose project: custom_project" in report["lines"]
    assert "any-bot-name" in D.emit(report) and "NEVER_PRINT_THIS_SECRET" not in D.emit(report)
    assert "current\t1" in D.emit(report)
    (install.app / "config.toml").write_text("changed")
    assert not install.detect()["current"]


@pytest.mark.parametrize(("latest", "expected"), [("v1.3.0", "v1.3.0 available"), ("v1.1.0", "current")])
def test_detect_compares_numeric_versions(install, latest, expected):
    install.prepare()
    report = install.detect(latest)
    assert expected in report["summary"] and not report["current"]


def test_missing_stopped_or_foreign_containers_make_an_install_partial(install):
    install.prepare()
    install.rows[0]["running"] = False
    assert "running application container" in install.detect()["summary"]
    install.rows[0]["labels"]["org.shazzzbot.install"] = "c" * 32
    report = install.detect()
    assert "owned application container" in report["summary"] and report["app"] == ""
    assert "Unowned candidate: any-bot-name; left unchanged" in report["lines"]
    install.rows[0]["labels"]["org.shazzzbot.install"] = ID
    install.rows[0]["running"] = True
    install.rows[1]["running"] = False
    assert "running Docker socket proxy" in install.detect()["summary"]


def test_missing_config_environment_and_identity_are_reported(install):
    install.prepare()
    (install.app / "config.toml").unlink()
    assert "config.toml (missing)" in install.detect()["summary"]
    (install.app / ".env").unlink()
    assert ".env (missing)" in install.detect()["summary"]
    (install.app / ".setup-identity").unlink()
    write(install.app / "config.toml", "fixture")
    assert "install identity" in install.detect()["summary"]


def test_bad_identity_is_unavailable_not_adopted(install):
    identity = install.prepare()
    write(install.app / ".setup-identity", json.dumps({**identity, "format": 1}))
    before = (install.app / ".setup-identity").read_bytes()
    with pytest.raises(ValueError, match="format is unsupported"):
        install.detect()
    assert (install.app / ".setup-identity").read_bytes() == before
    for changes in ({"host": "c" * 32}, {"app": "/somewhere"}, {"install_id": "bad"},
                    {"role": "other"}, {"method": "other"}, {"ref": None}, {"ref": "not-a-ref"}):
        write(install.app / ".setup-identity", json.dumps({**identity, **changes}))
        report = install.detect()
        assert report["state"] == "unavailable" and not report["app"]
    write(install.app / ".setup-identity", "[]")
    assert install.detect()["state"] == "unavailable"


def test_future_deployment_state_refuses_before_repair_or_metadata_write(install):
    install.prepare()
    path = install.app / ".setup-state"
    write(path, json.dumps({"format": 2}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="written by a newer Shazzzbot"):
        install.detect()
    assert path.read_bytes() == before


def test_unknown_versions_and_builds_do_not_claim_a_current_install(install, monkeypatch):
    install.prepare(method="compose-build", ref="d" * 40)
    assert not install.detect()["current"]
    for item in install.rows:
        item["labels"]["org.opencontainers.image.version"] = ""
        item["image"] = IMAGE + ":dev"
    monkeypatch.setattr(D, "latest_release", lambda _url: "")
    assert "version unknown (latest unavailable)" in install.detect("")["summary"]
    install.prepare()
    assert "installed v1.2.3" in install.detect()["summary"]


def test_compose_build_reports_release_version_from_oci_label_and_keeps_revision(install):
    version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    revision = "c" * 40
    install.prepare(method="compose-build", ref=revision)
    application = install.rows[0]
    application["image"] = "shazzzbot:dev"
    labels = application["labels"]
    labels["org.opencontainers.image.version"] = version
    labels["org.opencontainers.image.revision"] = revision
    report = install.detect("v" + version)
    assert report["state"] == "installed" and report["method"] == "compose-build"
    assert report["summary"] == f"Shazzzbot: installed v{version} (current)"
    assert report["ref"] == revision
    assert json.loads((install.app / ".setup-identity").read_bytes())["ref"] == revision
    assert labels["org.opencontainers.image.revision"] == revision
    assert application["image"] == "shazzzbot:dev"
    labels["org.opencontainers.image.version"] = ""
    assert "version unknown" in install.detect("v" + version)["summary"]


def test_role_filter_and_multiple_owned_installs(install, tmp_path):
    first = install.prepare()
    other = tmp_path / "other"
    for name in (".env", ".setup-state"):
        write(other / name, "compose-image relay v1.2.3")
    write(other / ".setup-identity", json.dumps({**first, "app": str(other), "role": "relay", "install_id": "c" * 32}))
    install.rows.append(row(other, owner="c" * 32))
    assert install.detect(role="", explicit=False)["state"] == "partial"
    report = install.detect(role="relay", explicit=False)
    assert report["app"] == str(other) and report["role"] == "relay"
    assert install.detect(role="bot")["app"] == str(install.app)


def test_unowned_container_is_reported_without_adopting_it(install):
    report = install.detect()
    assert report["state"] == "partial" and report["app"] == ""
    assert "unowned containers found" in report["summary"]
    assert "Unowned candidate: any-bot-name; left unchanged" in report["lines"]


def test_container_mount_locations_and_default_paths(install, monkeypatch):
    install.prepare(method="bot-only")
    install.rows[0]["labels"]["com.docker.compose.project.working_dir"] = ""
    install.rows[0]["labels"]["com.docker.compose.project"] = ""
    install.rows[0]["mounts"] = [{"source": str(install.app), "destination": "/config"},
                                 {"source": str(install.app / "config.toml"), "destination": "/config/config.toml"}]
    monkeypatch.setattr(D, "DEFAULTS", (str(install.app.parent / "absent"),))
    report = install.detect(explicit=False)
    assert report["app"] == str(install.app) and report["method"] == "bot-only"
    assert D._locations([], str(install.app)) == [str(install.app)]


def test_helper_detection_reports_complete_and_partial_states(install):
    install.prepare()
    for name in ("helper.pyz", "config.json", "api.key", "policy.json", ".identity.json"):
        write(install.helper / name, "{}")
    write(install.hook / "script", "#!/bin/bash\n")
    schedule = install.hook.parent.parent / "schedule.json"
    write(schedule, json.dumps({"shazzzbot-helper": {"frequency": "boot", "script": str(install.hook / "script")}}))
    assert "Helper: installed; startup schedule present" in install.detect()["lines"]
    (install.helper / "api.key").unlink()
    (install.hook / "script").unlink()
    schedule.unlink()
    report = install.detect()
    assert not report["current"]
    assert any("api.key, startup hook, startup schedule" in line for line in report["lines"])


def test_untrusted_files_and_unverifiable_current_state_are_not_ignored(install):
    identity = install.prepare()
    (install.app / "config.toml").chmod(0o666)
    assert "config.toml (untrusted)" in install.detect()["summary"]
    install.prepare()
    (install.app / "config.toml").unlink()
    (install.app / "config.toml").symlink_to(install.app / ".env")
    assert "config.toml (untrusted)" in install.detect()["summary"]
    (install.app / "config.toml").unlink()
    install.prepare()
    write(install.app / ".setup-state", "different")
    assert not install.detect()["current"]
    (install.app / ".setup-state").unlink()
    assert not install.detect()["current"]
    write(install.app / ".setup-identity", json.dumps({**identity, "files": []}))
    assert not install.detect()["current"]
    with pytest.raises(ValueError):
        write(install.app / "metadata.json", " " * 65537)
        D._read_json(install.app / "metadata.json")


def test_run_is_bounded_and_rejects_command_failures(monkeypatch):
    assert D._run([sys.executable, "-c", "print('ok')"]) == b"ok\n"
    with pytest.raises(ValueError):
        D._run([sys.executable, "-c", "print('x' * 100000)"], 1)
    with pytest.raises(ValueError):
        D._run([sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(D, "WAIT", 0.03)
    with pytest.raises(subprocess.TimeoutExpired):
        D._run([sys.executable, "-c", "import time; time.sleep(5)"])


def test_container_discovery_uses_only_safe_selected_fields(monkeypatch, tmp_path):
    labels = row(tmp_path)["labels"]
    raw = {"id": "a" * 12, "name": "/renamed", "image": IMAGE + ":latest", "labels": labels,
           "mounts": [{"Source": str(tmp_path), "Destination": "/config"},
                      {"Source": "/ignored", "Destination": "/other"}], "running": True}
    calls = []

    def run(argv, *_args):
        calls.append(argv)
        return ("a" * 12).encode() if argv[1] == "ps" else json.dumps(raw).encode()

    monkeypatch.setattr(D, "_run", run)
    found = D.containers(IMAGE)
    assert found[0]["name"] == "renamed" and found[0]["mounts"] == [{"source": str(tmp_path), "destination": "/config"}]
    assert calls[1][2:4] == ["--format", D.INSPECT] and ".Env" not in D.INSPECT
    raw["image"], raw["labels"] = "other:latest", {}
    assert D.containers(IMAGE) == []
    raw["labels"] = {"com.docker.compose.service": "docker-proxy"}
    assert D.containers(IMAGE)[0]["proxy"]
    raw["labels"] = None
    raw["image"] = "shazzzbot-" + "a" * 16 + ":dev"
    assert D.containers(IMAGE)[0]["name"] == "renamed"


@pytest.mark.parametrize("bad", ["not-an-id", "a" * 65])
def test_invalid_container_ids_are_rejected(monkeypatch, bad):
    monkeypatch.setattr(D, "_run", lambda *_args: bad.encode())
    with pytest.raises(ValueError):
        D.containers(IMAGE)


def test_empty_and_excessive_container_lists(monkeypatch):
    monkeypatch.setattr(D, "_run", lambda *_args: b"")
    assert D.containers(IMAGE) == []
    monkeypatch.setattr(D, "MAX_CONTAINERS", 0)
    monkeypatch.setattr(D, "_run", lambda *_args: b"aaaaaaaaaaaa")
    with pytest.raises(ValueError):
        D.containers(IMAGE)


@pytest.mark.parametrize("value", [[], {"labels": [], "mounts": [], "running": True},
                                  {"labels": {}, "mounts": None, "running": True},
                                  {"labels": {}, "mounts": [], "running": 1},
                                  {"labels": {}, "mounts": [], "running": True, "health": "unknown"},
                                  {"labels": {}, "mounts": [], "running": True, "state": "unknown"}])
def test_bad_container_shapes_are_rejected(monkeypatch, value):
    monkeypatch.setattr(D, "_run", lambda args: b"aaaaaaaaaaaa" if args[1] == "ps" else json.dumps(value).encode())
    with pytest.raises(ValueError):
        D.containers(IMAGE)


@pytest.mark.parametrize("mount", [None, {"Source": "relative", "Destination": "/config"}])
def test_bad_config_mounts_are_rejected(monkeypatch, tmp_path, mount):
    value = row(tmp_path)
    value["mounts"] = [mount]
    monkeypatch.setattr(D, "_run", lambda args: b"aaaaaaaaaaaa" if args[1] == "ps" else json.dumps(value).encode())
    with pytest.raises((TypeError, ValueError)):
        D.containers(IMAGE)


def test_versions_repositories_and_control_characters():
    assert D._repo("registry:5000/path/image:v1") == "registry:5000/path/image"
    assert D._repo("image@sha256:abc") == "image"
    assert D._version("1.10.0") == "v1.10.0" and D._version(None) == D._version("v01.0.0") == ""
    for value in (None, "bad\nvalue", "bad" + chr(0x202E) + "value", "x" * 4097):
        with pytest.raises(ValueError):
            D._text(value)
    for value in ("relative", "/", "/x/../y"):
        with pytest.raises(ValueError):
            D._path(value)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https:///none",
                                 "https://example.com?key=fixture", "https://example.com#fragment", "https://["])
def test_release_checks_refuse_unsafe_urls(url):
    assert D.latest_release(url) == ""


@pytest.mark.parametrize("body", [{"tag_name": "v1.2.3"}, {}, [], {"tag_name": "not-a-version"}])
def test_release_json_is_validated(monkeypatch, body):
    monkeypatch.setattr(D, "_run", lambda *_args: json.dumps(body).encode())
    expected = "v1.2.3" if body == {"tag_name": "v1.2.3"} else ""
    assert D.latest_release("https://example.com/releases/latest") == expected


def test_release_errors_and_detection_errors_are_clean(install, monkeypatch):
    for error in (OSError(), ValueError(), subprocess.TimeoutExpired("docker", 5)):
        def fail(*_args, error=error):
            raise error
        monkeypatch.setattr(D, "_run", fail)
        assert D.latest_release("https://example.com") == ""
    monkeypatch.setattr(D, "containers", fail)
    assert install.detect()["state"] == "unavailable"


def test_platform_detection_uses_only_host_files(monkeypatch):
    def read(path, **_kwargs):
        if path == Path("/marker"):
            raise FileNotFoundError
        return b'ID="ubuntu"\n' if path.name == "os-release" else b"systemd\n"
    monkeypatch.setattr(D, "read_safe", read)
    assert D.platform("/marker") == "ubuntu (init systemd)"
    monkeypatch.setattr(D, "read_safe", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert D.platform("/marker") == "unknown (init unknown)"


def test_legitimate_os_release_symlink_is_resolved_with_trusted_target(tmp_path, monkeypatch):
    target, link, init = tmp_path / "release", tmp_path / "os-release", tmp_path / "init"
    write(target, "ID=debian\n")
    write(init, "systemd\n")
    link.symlink_to(target)
    monkeypatch.setattr(D, "OS_RELEASE", link)
    monkeypatch.setattr(D, "INIT", init)
    assert D.platform(str(tmp_path / "missing")) == "debian (init systemd)"


def test_standalone_proxy_and_data_mount_discovery(monkeypatch, tmp_path):
    value = row(tmp_path, image="haproxy:3.2.0")
    value["labels"]["org.opencontainers.image.source"] = ""
    value["labels"]["com.docker.compose.service"] = ""
    value["mounts"] = [{"Source": str(tmp_path / "data"), "Destination": "/data"}]
    monkeypatch.setattr(D, "_run", lambda args: b"aaaaaaaaaaaa" if args[1] == "ps" else json.dumps(value).encode())
    assert D.containers(IMAGE)[0]["proxy"]
    value["image"] = IMAGE + ":v1.2.3"
    value["labels"]["com.docker.compose.project.working_dir"] = ""
    assert str(tmp_path) in D._locations(D.containers(IMAGE), "")


def test_only_the_unique_running_container_supplies_the_version(install):
    install.prepare()
    stopped = row(install.app, running=False, version="9.9.9")
    install.rows.insert(0, stopped)
    assert "installed v1.2.3" in install.detect()["summary"]
    stopped["running"] = True
    report = install.detect()
    assert report["state"] == "partial" and not report["current"]
    assert "unambiguous running application container" in report["summary"]


@pytest.mark.parametrize(("health", "state"), [("unhealthy", "running"), ("starting", "running"),
                                               ("healthy", "restarting"), (None, "dead")])
def test_unhealthy_or_restarting_application_is_not_current(install, health, state):
    install.prepare()
    install.rows[0].update(health=health, state=state)
    report = install.detect()
    assert report["state"] == "partial" and not report["current"]
    assert "healthy application container" in report["summary"]


def test_pool_paths_resolve_read_only_and_missing_default_does_not_hide_relay(monkeypatch, tmp_path):
    from shazzzbot.helper import footprint

    monkeypatch.setattr(footprint, "resolve_home", lambda path: tmp_path / path.name)
    assert D._install_path("/mnt/user/appdata/unit") == str(tmp_path / "unit")
    monkeypatch.setattr(footprint, "resolve_home", lambda _path: (_ for _ in ()).throw(ValueError("not mounted")))
    monkeypatch.setattr(D, "DEFAULTS", ("/mnt/user/appdata/missing", "/var/lib/shazzzbot-relay"))
    assert D._locations([], "") == ["/var/lib/shazzzbot-relay"]
    with pytest.raises(ValueError):
        D._locations([], "/mnt/user/appdata/missing")


def test_fuse_mounts_resolve_the_application_directory_not_its_files_or_data(install, monkeypatch):
    from shazzzbot.helper import footprint

    install.prepare()
    fuse = Path("/mnt/user/appdata/app")
    seen = []

    def resolve(path):
        seen.append(path)
        assert path == fuse
        return install.app

    monkeypatch.setattr(footprint, "resolve_home", resolve)
    install.rows[0]["labels"]["com.docker.compose.project.working_dir"] = ""
    install.rows[0]["mounts"] = [{"source": str(fuse / "config.toml"), "destination": "/config/config.toml"},
                                 {"source": str(fuse / "data"), "destination": "/data"}]
    report = D.detect(str(fuse), str(install.helper), str(install.hook), IMAGE, "v1.2.3", "bot", str(install.marker))
    assert report["app"] == str(install.app) and report["current"]
    assert seen and all(path == fuse for path in seen)
