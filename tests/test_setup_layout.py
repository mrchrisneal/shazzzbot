import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from shazzzbot.helper import footprint
from tools import setup_layout as layout
from tools import setup_pack as pack


@pytest.mark.parametrize("returncode,length", [(1, 0), (0, 16 * 1024**2 + 1), (0, 1)])
def test_pack_git_is_bounded_and_disables_hooks(tmp_path, monkeypatch, returncode, length):
    commands = []

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=b"x" * length)

    monkeypatch.setattr(pack.subprocess, "run", run)
    if returncode or length > 16 * 1024**2:
        with pytest.raises(ValueError, match="operation failed"):
            pack.git(tmp_path, "status")
    else:
        assert pack.git(tmp_path, "status") == b"x"
    argv, options = commands[0]
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
    assert options["timeout"] == 120 and options["env"]["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.fixture
def source_pack(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    archive = tmp_path / "source.pack"
    archive.write_bytes(b"PACK")
    commands = []
    answers = {}

    def git(directory, *args, **kwargs):
        assert directory == root
        commands.append(args)
        if args[0] == "init":
            (root / ".git").mkdir(mode=0o700)
        if args[0] == "unpack-objects":
            assert kwargs["input_file"].read() == b"PACK"
        defaults = {"cat-file": b"commit\n", "ls-tree": b"tools/setup.py\0"}
        return answers.get(args[0], defaults.get(args[0], b""))

    monkeypatch.setattr(pack, "git", git)
    return root, archive, commands, answers


def test_pack_apply_initializes_private_source_and_keeps_shallow_boundaries(source_pack):
    root, archive, commands, _answers = source_pack
    revision = "a" * 40
    pack.apply(root, archive, revision, "https://example.invalid/project.git")
    assert ("init", "-q", "-b", "main") in commands
    assert ("reset", "--hard", revision) in commands
    assert (root / ".git/shallow").read_text() == revision + "\n"
    pack.apply(root, archive, "b" * 40, "https://example.invalid/project.git")
    assert commands.count(("init", "-q", "-b", "main")) == 1
    assert (root / ".git/shallow").read_text().splitlines() == [revision, "b" * 40]


@pytest.mark.parametrize("failure", ["revision", "dirty", "large", "object", "reserved", "unidentified"])
def test_pack_apply_refuses_invalid_source(source_pack, monkeypatch, failure):
    root, archive, _commands, answers = source_pack
    revision = "not-a-sha" if failure == "revision" else "a" * 40
    if failure == "unidentified":
        (root / "unowned").write_bytes(b"retain")
    elif failure == "dirty":
        answers["status"] = b" M tools/setup.py\n"
    elif failure == "object":
        answers["cat-file"] = b"tree\n"
    elif failure == "reserved":
        answers["ls-tree"] = b".env\0"
    elif failure == "large":
        actual = os.fstat
        monkeypatch.setattr(pack.os, "fstat", lambda fd: SimpleNamespace(
            st_size=512 * 1024**2 + 1, st_uid=actual(fd).st_uid, st_mode=actual(fd).st_mode))
    with pytest.raises(ValueError):
        pack.apply(root, archive, revision, "https://example.invalid/project.git")


def test_private_git_environment_ignores_ambient_git_redirects(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/public/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/public/source")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/public/config")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/public/objects")
    monkeypatch.setenv("SHAZZZBOT_TEST_KEEP", "kept")
    env = pack.environment()
    assert env["SHAZZZBOT_TEST_KEEP"] == "kept"
    assert {name: value for name, value in env.items() if name.startswith("GIT_")} == {
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_ATTR_NOSYSTEM": "1"}


@pytest.mark.parametrize("name", ["commondir", "objects/info/alternates",
                                  "objects/info/http-alternates", "info/grafts"])
def test_private_pack_rejects_external_git_object_metadata(source_pack, name):
    root, archive, commands, _answers = source_pack
    path = root / ".git" / name
    path.parent.mkdir(parents=True)
    path.write_text("/public/objects\n")
    with pytest.raises(ValueError, match="outside"):
        pack.apply(root, archive, "a" * 40, "https://example.invalid/project.git")
    assert commands == []



IDENTIFIER = "a" * 32
IMAGE = "sha256:" + "b" * 64


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", IDENTIFIER)
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ROLE", "bot")
    root = tmp_path / "app"
    root.mkdir(mode=0o700)
    return root


@pytest.mark.parametrize("role,project", [("bot", "shazzzbot"), ("relay", "shazzzbot_relay")])
def test_context_has_only_the_explicit_install_identity_and_role(app, monkeypatch, role, project):
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ROLE", role)
    repository = "shazzzbot-" + hashlib.sha256(IDENTIFIER.encode()).hexdigest()[:16]
    assert layout.context() == {"install_id": IDENTIFIER, "role": role, "project": project,
                                "dev_image": repository + ":dev", "prev_image": repository + ":prev"}
    assert list(app.iterdir()) == []


@pytest.mark.parametrize("identifier", [None, "", "bad", "A" * 32, "a" * 32 + ":" + "b" * 64])
def test_context_refuses_missing_or_invalid_ids(app, monkeypatch, identifier):
    if identifier is None:
        monkeypatch.delenv("SHAZZZBOT_INSTALL_ID")
    else:
        monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", identifier)
    with pytest.raises(ValueError, match="install id"):
        layout.context()
    assert list(app.iterdir()) == []


@pytest.mark.parametrize("role", [None, "", "other"])
def test_context_refuses_missing_or_unknown_roles(app, monkeypatch, role):
    if role is None:
        monkeypatch.delenv("SHAZZZBOT_INSTALL_ROLE")
    else:
        monkeypatch.setenv("SHAZZZBOT_INSTALL_ROLE", role)
    with pytest.raises(ValueError, match="role"):
        layout.context()


def test_state_reads_only_the_private_deployment_state(app):
    assert layout.state(app) == ""
    state = app / ".setup-state"
    state.write_bytes(layout.state_data("compose-build", "bot", "a" * 40))
    state.chmod(0o600)
    assert layout.state(app) == "compose-build bot " + "a" * 40
    state.chmod(0o644)
    with pytest.raises(ValueError, match="secret file"):
        layout.state(app)


@pytest.mark.parametrize("change", [{"extra": 1}, {"method": "unknown"}, {"role": "other"},
                                    {"role": "relay", "method": "bot-only"}, {"ref": 1}, {"ref": "bad"}])
def test_deployment_state_rejects_malformed_records_without_rewriting(app, change):
    value = {"format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.3", **change}
    path = app / ".setup-state"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid deployment state"):
        layout.state(app)
    assert path.read_bytes() == before


def test_deployment_state_future_format_refuses_without_rewrite(app):
    path = app / ".setup-state"
    path.write_text(json.dumps({"format": 2}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="written by a newer Shazzzbot"):
        layout.state(app)
    assert json.loads(path.read_bytes()) == {"format": 2}


def test_environment_normalization_keeps_values_and_uses_the_fixed_ram_socket(app):
    env = app / ".env"
    special = ' space # hash "quoted" \\path \u00e9 '
    env.write_text("SPECIAL=" + json.dumps(special, ensure_ascii=False) + "\n")
    env.chmod(0o600)
    layout.normalize_environment(app, app, "bot")
    assert layout.dotenv(env.read_text()) == {"SPECIAL": special,
                                               "HELPER_SOCKET_DIR": str(footprint.WORK / "socket")}


def test_relay_environment_is_not_rewritten_and_missing_env_is_not_created(app):
    env = app / ".env"
    original = "CUSTOM_KEY=private\nHELPER_SOCKET_DIR=/private/socket\n"
    env.write_text(original)
    env.chmod(0o600)
    layout.normalize_environment(app, app, "relay")
    assert env.read_text() == original
    layout.normalize_environment(app, app, "bot")
    values = layout.dotenv(env.read_text())
    assert values == {"CUSTOM_KEY": "private", "HELPER_SOCKET_DIR": str(footprint.WORK / "socket")}
    env.unlink()
    layout.normalize_environment(app, app, "bot")
    assert not env.exists()


@pytest.mark.parametrize("role,file", [("bot", "compose.yaml"), ("relay", "compose.relay.yaml")])
def test_current_compose_files_use_owned_images_data_and_networks(app, monkeypatch, role, file):
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ROLE", role)
    value = layout.context()
    compiled = layout.compose(Path(file).read_text(), value)
    assert "    build:" not in compiled
    assert "    image: " + value["dev_image"] in compiled
    assert "      - ./data:/data" in compiled
    assert '    name: "' + value["project"] + '_default"' in compiled
    assert IDENTIFIER in compiled and "${SHAZZZBOT_INSTALL_ID:?run setup.sh}" in compiled


@pytest.mark.parametrize("role,service", [("bot", "shazzzbot"), ("relay", "relay")])
def test_rollback_pins_the_verified_image_and_never_builds(app, monkeypatch, role, service):
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ROLE", role)
    file = app / "compose.yaml"
    file.write_text("services:\n  " + service + ":\n    build: {context: /source}\n"
                    "    image: shazzzbot:dev\n  other:\n    image: keep\n")
    layout.rollback_compose(app, file, IMAGE)
    assert "    image: " + IMAGE in file.read_text()
    assert "    image: keep" in file.read_text() and "    build:" not in file.read_text()
    with pytest.raises(ValueError, match="rollback image"):
        layout.rollback_compose(app, file, "latest")
    file.write_text("services:\n  other:\n    image: keep\n")
    with pytest.raises(ValueError, match="no application image"):
        layout.rollback_compose(app, file, IMAGE)
