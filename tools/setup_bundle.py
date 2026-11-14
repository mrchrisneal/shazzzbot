"""Read a trusted local helper bundle once and stage only its verified bytes."""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shazzzbot.helper import footprint
from tools.setup_artifacts import platform_document
from tools.setup_files import opened, trusted, write_at

MANIFEST = "helper-unraid.json"
ENTRY = "shazzzbot-helper-unraid.py"


@dataclass(frozen=True)
class LocalBundle:
    manifest: dict[str, Any]
    files: dict[str, bytes]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def revision(self) -> str:
        return self.manifest["revision"]


def _read(root: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=root)
    try:
        info = os.fstat(fd)
        trusted(info, directory=False)
        if not 1 <= info.st_size <= limit:
            raise ValueError("local helper bundle file exceeds its size limit")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if not 1 <= len(data) <= limit:
            raise ValueError("local helper bundle file exceeds its size limit")
        return data
    finally:
        os.close(fd)


def local_bundle(path: Path, version: str = "") -> LocalBundle:
    if path.is_relative_to(Path("/boot")):
        raise ValueError("local helper bundle must be outside /boot")
    with opened(path, directory=True) as root:
        if set(os.listdir(root)) != {MANIFEST, "helper.pyz", ENTRY}:
            raise ValueError("local helper bundle must contain exactly its manifest, archive, and platform entry")
        raw = _read(root, MANIFEST, 65536)
        manifest = platform_document(raw, "unraid")
        if version and version != manifest["version"]:
            raise ValueError("local helper bundle version does not match --version")
        files = {MANIFEST: raw}
        for name, record in manifest["files"].items():
            data = _read(root, name, record["size"])
            if len(data) != record["size"] \
                    or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), record["sha256"]):
                raise ValueError("local helper bundle file size or SHA-256 mismatch")
            files[name] = data
    return LocalBundle(manifest, files)


def stage_bundle(bundle: LocalBundle, destination: Path) -> None:
    if not destination.is_relative_to(footprint.WORK) or destination == footprint.WORK:
        raise ValueError("helper bundle staging must stay inside the private work directory")
    with opened(destination, directory=True) as stage:
        if os.fstat(stage).st_mode & 0o077:
            raise ValueError("helper bundle staging directory must be private")
        for name, data in bundle.files.items():
            write_at(stage, name, data, mode=0o600 if name == MANIFEST else 0o700)
