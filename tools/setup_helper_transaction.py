from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shazzzbot.helper import formats
from tools.setup_files import helper_home, host_identity, read_at, read_safe, trusted, write_at

IDENTIFIER = re.compile(r"[0-9]{8}-[0-9]{6}\.[0-9a-f]{12}")


@dataclass(frozen=True)
class Binding:
    home: Path
    inode: list[int]
    app: Path | None

    @contextmanager
    def opened(self):
        from shazzzbot.helper.footprint import identity

        with helper_home(self.home, self.inode) as fd:
            if identity(fd)["install_id"] != os.environ.get("SHAZZZBOT_INSTALL_ID"):
                raise ValueError("helper directory belongs to another installation")
            yield fd

    def relative(self, folder: Path) -> str:
        if folder.parent != self.home / "transactions" or IDENTIFIER.fullmatch(folder.name) is None:
            raise ValueError("helper transaction must stay in its private home")
        return "transactions/" + folder.name

    def validate(self, value: dict) -> None:
        if value.get("helper") != str(self.home) or value.get("helper_inode") != self.inode \
                or value.get("app") != (str(self.app) if self.app is not None else None):
            raise ValueError("helper transaction does not match its independently recorded installation")
        hook = Path(os.environ.get("SHAZZZBOT_HELPER_HOOK_DIR",
                         "/boot/config/plugins/user.scripts/scripts/shazzzbot-helper"))
        if value.get("hook") != str(hook) or "schedule" in value and value["schedule"] != str(hook.parent.parent):
            raise ValueError("helper transaction hook does not match its installation")
        entries = value.get("files")
        if not isinstance(entries, dict):
            raise TypeError("invalid helper transaction files")
        for key in entries:
            area, separator, name = key.partition("/")
            if not separator or area not in {"helper", "hook", "schedule", "app"} \
                    or area == "app" and (self.app is None or name != ".env"):
                raise ValueError("file is outside the helper-only transaction write set")


def binding() -> Binding:
    home = Path(os.environ["SHAZZZBOT_HELPER_HOME"])
    inode = json.loads(os.environ["SHAZZZBOT_HELPER_INODE"])
    if not isinstance(inode, list) or len(inode) != 2 or any(type(n) is not int or n < 0 for n in inode):
        raise ValueError("invalid independently recorded helper inode")
    selected = os.environ.get("SHAZZZBOT_HELPER_APP", "")
    app = Path(selected) if selected else None
    if app is not None:
        identity = formats.load(json.loads(read_safe(app / ".setup-identity", private=True)), "install identity", 2)
        identifier = os.environ.get("SHAZZZBOT_BOT_INSTALL_ID", "")
        if identity.get("app") != str(app) \
                or identity.get("host") != host_identity() or identity.get("role") != "bot" \
                or re.fullmatch(r"[0-9a-f]{32}", identifier) is None or identity.get("install_id") != identifier:
            raise ValueError("helper wiring requires an independently verified managed bot")
    return Binding(home, inode, app)


@contextmanager
def roots(path: Path, create: bool = False):
    scope = binding()
    if path != scope.home / "transactions":
        raise ValueError("helper transaction metadata must stay in its private home")
    with scope.opened() as home:
        if create:
            try:
                os.mkdir("transactions", 0o700, dir_fd=home)
                os.fsync(home)
            except FileExistsError:
                pass
        fd = os.open("transactions", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        try:
            trusted(os.fstat(fd), directory=True)
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError("helper transaction metadata must be private")
            yield fd
        finally:
            os.close(fd)


def read_manifest(folder: Path) -> bytes:
    scope = binding()
    relative = scope.relative(folder) + "/manifest.json"
    with scope.opened() as home:
        return read_at(home, relative, private=True)[0]


def save_manifest(folder: Path, value: dict, data: bytes) -> None:
    scope = binding()
    scope.validate(value)
    relative = scope.relative(folder) + "/manifest.json"
    with scope.opened() as home:
        write_at(home, relative, data)


def home_snapshot(home: Path, app: Path | None, keep: int, expected: list[int], managed: bool = False) -> Path:
    from tools import setup_transaction as transaction

    scope = binding()
    if home != scope.home or expected != scope.inode or app != scope.app or managed != (app is not None):
        raise ValueError("helper snapshot does not match its independently recorded scope")
    if os.environ.get("SHAZZZBOT_HELPER_INSTALLING") != "1":
        raise ValueError("helper-only snapshot requires its installation identity")
    return transaction.snapshot(app, home, home / "transactions", keep)


def _pending(home: Path, folder: Path | None, commit: bool) -> Path | None:
    from tools import setup_transaction as transaction

    scope = binding()
    if home != scope.home:
        raise ValueError("helper pending marker is outside its private home")
    if folder is not None:
        transaction.manifest(folder)
    with roots(home / "transactions", create=folder is not None) as parent:
        try:
            raw = read_at(parent, "pending", private=True)[0]
        except FileNotFoundError:
            current = None
        else:
            if len(raw) > 4096:
                raise ValueError("helper pending marker is too large")
            current = Path(raw.decode().strip())
            scope.relative(current)
        if commit:
            if current is not None:
                transaction.manifest(current)
                os.unlink("pending", dir_fd=parent)
                os.fsync(parent)
            return current
        if folder is not None:
            if current is not None and current != folder:
                raise ValueError("another helper transaction needs recovery")
            write_at(parent, "pending", (str(folder) + "\n").encode())
            return folder
        return current


def pending(home: Path, folder: Path | None = None, commit: bool = False) -> Path | None:
    try:
        return _pending(home, folder, commit)
    except FileNotFoundError:
        if folder is not None:
            raise
        with binding().opened() as parent:
            try:
                os.stat("transactions", dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        raise


def main(argv: list[str] | None = None) -> None:
    from tools import setup_transaction as transaction

    action, *args = sys.argv[1:] if argv is None else argv
    scope = binding()
    if action == "snapshot":
        app = Path(args[1]) if args[1] else None
        print(home_snapshot(Path(args[0]), app, int(args[2]), scope.inode, managed=app is not None))
    elif action in {"pending", "commit", "check-pending"}:
        folder = Path(args[1]) if action == "pending" else None
        current = pending(Path(args[0]), folder, commit=action == "commit")
        if action == "check-pending" and current is not None:
            raise ValueError(f"an interrupted helper install needs recovery from {current}")
    elif action == "restore":
        app = Path(args[1]) if args[1] else None
        if app != scope.app or Path(args[2]) != scope.home or args[3] not in {"0", "1"}:
            raise ValueError("helper recovery does not match its independently recorded scope")
        transaction.restore_snapshot(Path(args[0]), app, scope.home, helper_changed=args[3] == "1")
    else:
        raise ValueError("unknown helper transaction operation")


if __name__ == "__main__":
    main()
