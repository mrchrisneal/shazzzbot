from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shazzzbot.helper import formats
from tools import setup_artifacts as artifacts
from tools import setup_config, setup_helper
from tools import setup_transaction as transaction
from tools.setup_files import (
    host_identity,
    inspect_file,
    install_identity,
    opened,
    read_safe,
    select_pinned_helper,
    trusted_code,
    verify_pin,
    write_safe,
)


def managed_names(role: str, method: str) -> tuple[str, ...]:
    if role not in {"bot", "relay"} or method not in {"compose-build", "compose-image", "docker-run", "bot-only"}:
        raise ValueError("unknown install role or method")
    if role == "relay" and method == "bot-only":
        raise ValueError("unknown install role or method")
    names: tuple[str, ...] = ()
    if method in {"compose-build", "compose-image"}:
        names = ("compose.relay.yaml" if method == "compose-build" and role == "relay" else "compose.yaml",)
    if role == "bot" and method != "bot-only":
        names += ("docker-proxy/haproxy.cfg",)
    return names


def identity(app: Path, role: str, method: str, ref: str) -> bytes:
    identifier = os.environ.get("SHAZZZBOT_INSTALL_ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
        raise ValueError("invalid install id")
    values = {}
    names = (".env", "config.toml") if role == "bot" else (".env",)
    for name in names + managed_names(role, method):
        try:
            data = read_safe(app / name, private=name in {".env", "config.toml"})
        except FileNotFoundError:
            continue
        values[name] = hashlib.sha256(data).hexdigest()
    return json.dumps(formats.dump({"host": host_identity(),
                       "install_id": identifier,
                       "app": str(app), "role": role, "method": method,
                       "ref": ref, "files": values}, 2), sort_keys=True).encode()


def managed_files(app: Path, role: str, method: str) -> None:
    value = formats.load(json.loads(read_safe(app / ".setup-identity", private=True)), "install identity", 2)
    if not isinstance(value.get("files"), dict):
        raise TypeError("unsupported install identity")
    for name in managed_names(role, method):
        try:
            data = read_safe(app / name)
        except FileNotFoundError:
            continue
        if name not in value["files"] or hashlib.sha256(data).hexdigest() != value["files"][name]:
            raise ValueError(f"managed file {name} changed; reconcile it before upgrading")


def manage(argv: list[str]) -> None:
    action, *args = argv
    path = Path(args[0])
    if action == "detect":
        from tools import setup_detect

        if len(args) != 7:
            raise ValueError("install detection requires seven arguments")
        print(setup_detect.emit(setup_detect.detect(*args)))
    elif action == "state-fingerprint":
        record: dict[str, object] = {}
        for name, target in (("state", path / ".setup-state"), ("identity", path / ".setup-identity"),
                             ("pending", Path(args[1]) / "pending")):
            try:
                data = read_safe(target, private=True)
            except FileNotFoundError:
                record[name] = None
            else:
                record[name] = hashlib.sha256(data).hexdigest()
        try:
            with opened(path, directory=True) as fd:
                info = os.fstat(fd)
                record["directory"] = [info.st_dev, info.st_ino]
        except FileNotFoundError:
            record["directory"] = None
        print(hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest())
    elif action == "trust":
        inspect_file(path) if path.is_file() or path.is_symlink() else _directory(path)
    elif action in {"lock-parent", "external-root"}:
        with opened(path, directory=True):
            pass
    elif action == "path":
        if path.anchor != "/" or path == Path("/") or ".." in path.parts \
                or any(c in str(path) for c in "\n\r\0:,"):
            raise ValueError("invalid installation path")
        print(path)
    elif action in {"resolve-helper", "init-helper", "work-root", "socket-directory"}:
        from shazzzbot.helper import footprint

        if action == "resolve-helper":
            print(footprint.resolve_home(path))
        elif action == "init-helper":
            footprint.initialize(path, args[1])
        elif action == "socket-directory":
            if path != footprint.WORK / "socket":
                raise ValueError("socket must stay in the helper work directory")
            footprint.ensure_socket(int(args[1]))
        else:
            footprint.ensure_workdir()
    elif action == "layout-state":
        from tools.setup_layout import state

        print(state(path))
    elif action == "layout-state-data":
        from tools.setup_layout import state_data

        sys.stdout.buffer.write(state_data(*args[1:]))
    elif action == "layout-info":
        from tools.setup_layout import context

        print(context()[args[1]])
    elif action == "layout-env":
        from tools.setup_layout import context, normalize_environment

        normalize_environment(path, Path(args[1]), context()["role"])
    elif action == "layout-rollback-compose":
        from tools.setup_layout import rollback_compose

        rollback_compose(path, Path(args[1]), args[2])
    elif action == "source-pack":
        from tools.setup_pack import apply

        apply(path, Path(args[1]), args[2], args[3])
    elif action == "layout-compose":
        from tools.setup_layout import compose, context

        sys.stdout.write(compose(read_safe(Path(args[1])).decode(), context()))
    elif action == "layout-compose-input":
        from tools.setup_layout import compose, context

        sys.stdout.write(compose(sys.stdin.read(artifacts.MAX_SOURCE + 1), context()))
    elif action == "helper-policy":
        value = formats.load(json.loads(read_safe(path / "policy.json", private=True)), "helper policy", 1)
        expected = {"artifact": str(path / "helper.pyz"), "config": str(path / "config.json")}
        if any(value.get(key) != item for key, item in expected.items()):
            raise ValueError("helper installation identity does not match this directory")
        verify_pin(value, value.get("sha256", ""))
    elif action == "select-helper":
        value = transaction.manifest(Path(args[2]))
        if value.get("helper") != str(path) or value.get("helper_inode") is None:
            raise ValueError("helper rollback selection requires its recorded installation")
        print(select_pinned_helper(path, Path(args[1]), value["helper_inode"]))
    elif action == "launcher-text":
        from tools.setup_schedule import hook_text

        sys.stdout.buffer.write(hook_text(path, Path(args[1]), read_safe(Path(args[2]), private=True)))
    elif action == "check-launcher":
        value = formats.load(json.loads(read_safe(Path(args[1]) / ".identity.json", private=True)),
                             "helper identity", 1)
        hook = path / "script"
        if value.get("hook") != str(hook) \
                or value.get("hook_sha256") != hashlib.sha256(read_safe(hook)).hexdigest():
            raise ValueError("helper hook does not match its installed entry")
    elif action == "helper-identity":
        from shazzzbot.helper.footprint import identity_payload

        sys.stdout.buffer.write(identity_payload(path, args[1], hook=Path(args[2]), active=args[3] == "1"))
    elif action == "hook-active":
        from tools.setup_schedule import active

        print(int(active(path)))
    elif action == "checksum":
        print(hashlib.sha256(read_safe(path)).hexdigest())
    elif action == "verify-policy":
        verify_pin(formats.load(json.loads(read_safe(path, private=True)), "helper policy", 1), args[1])
    elif action == "schedule":
        from tools.setup_schedule import schedule

        schedule(path, Path(args[1]))
    elif action in {"save-answers", "restore-answers"}:
        from tools.setup_answers import answers

        answers(path, Path(args[1]), Path(args[2]), args[3], args[4], save=action == "save-answers")
    elif action == "private":
        inspect_file(path, private=True, uid=int(args[1]) if len(args) > 1 else 0)
    elif action == "mkdir":
        with opened(path, directory=True, create=True):
            pass
    elif action == "data-directory":
        with opened(path, directory=True, uid=int(args[1])):
            pass
    elif action == "empty":
        with opened(path, directory=True) as fd, os.scandir(fd) as entries:
            if next(entries, None) is not None:
                raise ValueError("directory contains files not owned by this install")
    elif action == "write":
        write_safe(path, sys.stdin.buffer.read(artifacts.MAX_SOURCE + 1))
    elif action == "read":
        sys.stdout.buffer.write(read_safe(path))
    elif action == "env":
        try:
            data = read_safe(path, private=True).decode()
        except FileNotFoundError:
            data = ""
        print(setup_config.dotenv(data).get(args[1], ""))
    elif action == "env-file":
        values = setup_config.dotenv(read_safe(path, private=True).decode())
        write_safe(Path(args[1]), "".join(f"{key}={value}\n" for key, value in values.items()).encode())
    elif action == "code":
        trusted_code(path)
    elif action == "owner":
        value = formats.load(json.loads(read_safe(path / ".setup-identity", private=True)), "install identity", 2)
        if value.get("host") != host_identity() or value.get("app") != str(path) \
                or value.get("install_id") != os.environ.get("SHAZZZBOT_INSTALL_ID"):
            raise ValueError("host or installation identity mismatch")
    elif action == "managed":
        managed_files(path, args[1], args[2])
    elif action == "host":
        print(install_identity(host_identity(path), Path(args[1]), Path(args[2]) if len(args) > 2 else None))
    elif action == "remember-id":
        if re.fullmatch(r"[0-9a-f]{32}", args[2]) is None:
            raise ValueError("invalid install id")
        value = formats.dump({"host": host_identity(), "app": args[1], "install_id": args[2]}, 1)
        try:
            old = formats.load(json.loads(read_safe(path / "install-identity.json", private=True)),
                               "saved install identity", 1)
        except FileNotFoundError:
            old = value
        if old != value:
            raise ValueError("saved install identity mismatch")
        write_safe(path / "install-identity.json", json.dumps(value).encode())
    elif action == "backup":
        print(transaction.snapshot(path, Path(args[1]), Path(args[2]), int(args[3]), *args[4:]))
    elif action in {"watch", "seal"}:
        getattr(transaction, action)(path, args[1], args[2])
    elif action == "replace":
        transaction.replace(path, args[1], args[2], sys.stdin.buffer.read(artifacts.MAX_SOURCE + 1),
                            int(args[3]) if len(args) > 3 else None)
    elif action == "restore":
        transaction.restore_snapshot(path, Path(args[1]), Path(args[2]), helper_changed=args[3] == "1")
    elif action in {"identity", "identity-data", "drift"}:
        data = identity(path, *args[1:])
        if action == "identity-data":
            sys.stdout.buffer.write(data)
        elif action == "identity":
            write_safe(path / ".setup-identity", data)
        elif read_safe(path / ".setup-identity", private=True) != data:
            raise ValueError("install identity or configuration changed")
    elif action in {"helper", "signal"}:
        setup_helper.process(path, Path(args[1]), action)
    elif action == "start-helper":
        setup_helper.start(path, Path(args[1]), Path(args[2]))
    elif action == "release":
        print(artifacts.release(path, args[1])[args[2]])
    elif action == "release-tag":
        print(formats.load(json.loads(read_safe(path)), "release manifest", 1)["version"])
    elif action == "extract":
        artifacts.extract(path, Path(args[1]), args[2])
    elif action == "verify":
        artifacts.verify(path, args[1])
    else:
        raise ValueError("unknown installer operation")


def _directory(path: Path) -> None:
    with opened(path, directory=True):
        pass


def main() -> None:
    try:
        manage(sys.argv[1:])
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise SystemExit("installer file operation failed: " + str(exc)) from None


if __name__ == "__main__":
    main()
