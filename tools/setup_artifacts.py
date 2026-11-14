from __future__ import annotations

import hashlib
import hmac
import json
import re
import tarfile
from pathlib import Path

from shazzzbot.helper import formats
from shazzzbot.helper.constants import version_number
from tools.setup_files import opened, read_safe, write_safe

MAX_SOURCE = 64 * 1024 * 1024
MAX_UPDATE = 1024 * 1024
PLATFORMS = ("unraid",)


def release(path: Path, tag: str) -> dict:
    value = formats.load(json.loads(read_safe(path)), "release manifest", 1)
    if set(value) != {"format", "version", "revision", "source_sha256", "helper_sha256", "image",
                                                   "platforms"}:
        raise ValueError("invalid release digest manifest")
    if value["version"] != tag or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("release digest version mismatch")
    for key, length in (("revision", 40), ("source_sha256", 64), ("helper_sha256", 64)):
        if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value[key]):
            raise ValueError("invalid release digest: " + key)
    if not isinstance(value["image"], str) or not re.fullmatch(
            r"ghcr\.io/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}", value["image"]):
        raise ValueError("invalid release image digest")
    platforms = value["platforms"]
    if not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS) or not all(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) for digest in platforms.values()):
        raise ValueError("invalid release platform digests")
    return value


def platform_document(data: bytes, platform: str) -> dict:
    if platform not in PLATFORMS:
        raise ValueError("unsupported helper platform")
    if len(data) > 65536:
        raise ValueError("platform manifest exceeds its size limit")
    value = formats.load(json.loads(data), "helper platform manifest", 1)
    if set(value) != {"format", "platform", "version", "revision", "files"} or value["platform"] != platform \
            or not isinstance(value["version"], str) \
            or value["version"] != "v" + ".".join(map(str, version_number(value["version"]))) \
            or not isinstance(value["revision"], str) or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None:
        raise ValueError("invalid helper platform manifest")
    files = value["files"]
    entry = f"shazzzbot-helper-{platform}.py"
    if not isinstance(files, dict) or set(files) != {"helper.pyz", entry}:
        raise ValueError("invalid helper platform files")
    for name, record in files.items():
        limit = 65536 if name == entry else MAX_UPDATE
        if not isinstance(record, dict) or set(record) != {"sha256", "size"} \
                or not isinstance(record["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None \
                or type(record["size"]) is not int or not 1 <= record["size"] <= limit:
            raise ValueError("invalid helper platform file digest or size")
    return value


def platform_manifest(path: Path, release: dict, platform: str) -> dict:
    if platform not in PLATFORMS:
        raise ValueError("unsupported helper platform")
    expected = release.get("platforms", {}).get(platform)
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("invalid release platform digest")
    data = read_safe(path)
    if len(data) > 65536 or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise ValueError("platform manifest size or SHA-256 mismatch")
    value = platform_document(data, platform)
    if value["version"] != release["version"] or value["revision"] != release["revision"]:
        raise ValueError("invalid helper platform manifest")
    if value["files"]["helper.pyz"]["sha256"] != release["helper_sha256"]:
        raise ValueError("helper archive digest disagrees with release")
    return value


def verify(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid expected digest")
    with opened(path) as fd:
        import os
        with os.fdopen(os.dup(fd), "rb") as stream:
            hasher = hashlib.sha256()
            while chunk := stream.read(65536):
                hasher.update(chunk)
            actual = hasher.hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("release SHA-256 mismatch")


def extract(path: Path, destination: Path, expected: str) -> None:
    verify(path, expected)
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        entries = [item for item in archive.getmembers() if not (len(Path(item.name).parts) == 1 and item.isdir())]
        if len(entries) > 10000:
            raise ValueError("release contains too many files")
        seen = set()
        for item in entries:
            parts = Path(item.name).parts
            if len(parts) < 2 or item.name.startswith("/") or ".." in parts or "\\" in item.name:
                raise ValueError("invalid release member path")
            name = Path(*parts[1:])
            if name in seen or not (item.isdir() or item.isfile()):
                raise ValueError("duplicate or non-regular release member")
            seen.add(name)
            total += item.size
            if total > MAX_SOURCE:
                raise ValueError("release exceeds unpacked size limit")
        for item in entries:
            target = destination / Path(*Path(item.name).parts[1:])
            with opened(target if item.isdir() else target.parent, directory=True, create=True):
                pass
            if item.isfile():
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError("release member is missing")
                with stream:
                    write_safe(target, stream.read(item.size + 1))
    for name in ("compose.yaml", "compose.relay.yaml", "config.example.toml", ".env.example",
                 "docker-proxy/haproxy.cfg", "helper.example.json", "shazzzbot/helper/cli.py"):
        if not read_safe(destination / name):
            raise ValueError("release is missing required content: " + name)
