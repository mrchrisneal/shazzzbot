from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import select
import signal
import ssl
import stat
import subprocess  # fixed isolated Python launcher, never a shell; reviewed 2026-11  # nosec B404
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import files, footprint, installation
from .artifact import source_version
from .constants import ENV, MAX_ACTION_TIMEOUT, MAX_UPDATE, VERSION, version_number


def environment(cfg: dict[str, Any]) -> dict[str, str]:
    root = footprint.ensure_workdir()
    return {**ENV, "HOME": str(root), "TMPDIR": str(root),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data")}


@contextlib.contextmanager
def deadline(seconds: float):
    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("HTTP total deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


async def release(cfg: dict[str, Any]) -> str:
    installation.guard(cfg)
    policy = installation.policy(cfg, footprint.home(cfg) / "helper.pyz")
    data = _loaded_bytes(cfg, footprint.home(cfg) / "helper.pyz")
    actual = hashlib.sha256(data).hexdigest()
    if not any(hmac.compare_digest(actual, pin)
               for pin in (policy["sha256"], policy.get("previous_sha256", ""))):
        raise ValueError("release worker archive does not match its pinned hash")
    runtime = footprint.ensure_workdir() / "runtime/helper.pyz"
    files.write_private(runtime, data, 0o700)
    with files.trusted_open(runtime) as fd:
        code = "import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_module(sys.argv[2],run_name='__main__')"
        child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", "-c", code,
                        str(footprint.fd_path(fd)), "helper.updates", stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        env=environment(cfg), close_fds=True, pass_fds=(fd,))
        try:
            output, _ = await asyncio.wait_for(child.communicate(cfg["release_url"].encode()), 6)
            if child.returncode or len(output) > 128:
                raise ValueError("release check failed")
            return output.decode().strip()
        finally:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("update redirect must use https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def latest_release(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         HTTPSRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                 "User-Agent": "shazzzbot-helper/" + VERSION})
    with deadline(5), opener.open(request, timeout=5) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("release reply must use https")
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("release reply too large")
    body = json.loads(data)
    version = body.get("tag_name") if isinstance(body, dict) else None
    version = version if isinstance(version, str) else ""
    version_number(version)
    return version.removeprefix("v")


REEXEC = r"""
import builtins, hashlib, hmac, json, os, signal, stat, sys, tempfile, runpy, zipimport
path, config, digest, result = sys.argv[1:]
parent = os.path.dirname(path)
if (os.path.basename(path) != "helper.pyz" or config != os.path.join(parent, "config.json")
        or os.path.basename(result) != "update.result" or os.path.basename(os.path.dirname(result)) != "socket"):
    raise ValueError("invalid update paths")
home = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = os.fstat(home)
if info.st_uid != 0 or info.st_mode & 0o077:
    raise ValueError("unsafe helper home")
home_identity = (info.st_dev, info.st_ino)
work = os.path.dirname(os.path.dirname(result))
working = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = os.fstat(working)
if info.st_uid != 0 or info.st_mode & 0o077:
    raise ValueError("unsafe helper work directory")
socket = os.open("socket", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=working)
try:
    os.mkdir("runtime", 0o700, dir_fd=working)
except FileExistsError:
    pass
runtime = os.open("runtime", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=working)
info = os.fstat(runtime)
if info.st_uid != 0 or info.st_mode & 0o077:
    raise ValueError("unsafe helper runtime directory")
os.close(working)
os.close(home)
info = os.fstat(socket)
if info.st_uid != 0 or info.st_mode & 0o027:
    raise ValueError("unsafe helper socket directory")
clean = dict(os.environ)
ready = False
archives = []

def read_private(name):
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != home_identity:
            raise ValueError("helper home identity changed")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise ValueError("unsafe update file")
        data = stream.read(1048577)
        if len(data) > 1048576:
            raise ValueError("update file too large")
        return data

def replace(directory, name, data, mode):
    fd, tmp = tempfile.mkstemp(prefix=".helper-", dir="/proc/self/fd/" + str(directory))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(os.path.basename(tmp), name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(os.path.basename(tmp), dir_fd=directory)
        except FileNotFoundError:
            pass

def archive(expected):
    data = read_private("helper.pyz")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise ValueError("update hash mismatch")
    replace(runtime, "helper.pyz", data, 0o700)
    fd = os.open("helper.pyz", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=runtime)
    archives.append(fd)
    return fd

def acknowledge(actual):
    global ready
    if not hmac.compare_digest(actual, digest):
        raise ValueError("running update hash mismatch")
    replace(socket, "update.result", json.dumps({"ok": True, "sha256": digest}).encode(), 0o600)
    ready = True
    signal.alarm(0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)

def timeout(signum, frame):
    raise TimeoutError("helper startup timed out")

try:
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(30)
    loaded = archive(digest)
    builtins._shazzzbot_ready = acknowledge
    execution = "/proc/self/fd/" + str(loaded)
    sys.path_importer_cache.pop(execution, None)
    zipimport._zip_directory_cache.pop(execution, None)
    sys.argv = [execution, "--config", config]
    runpy.run_path(execution, run_name="__main__")
    if not ready:
        raise RuntimeError("helper exited before startup")
except BaseException:
    if ready:
        raise
    signal.alarm(0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    previous = read_private("helper.pyz.prev")
    previous_digest = hashlib.sha256(previous).hexdigest()
    pins = json.loads(read_private("policy.json"))
    if not isinstance(pins, dict) or type(pins.get("format")) is not int or pins["format"] != 1:
        raise ValueError("unsupported helper policy format; written by a newer Shazzzbot or invalid")
    if previous_digest not in (pins.get("sha256"), pins.get("previous_sha256")):
        raise ValueError("previous helper does not match its pinned hash")
    home = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(home)
        if (info.st_dev, info.st_ino) != home_identity:
            raise ValueError("helper home identity changed")
        replace(home, "helper.pyz", previous, 0o700)
    finally:
        os.close(home)
    replace(socket, "update.result", json.dumps({"ok": False, "sha256": digest}).encode(), 0o600)
    print("update startup failed; restored previous helper", file=sys.stderr, flush=True)
    loaded = archive(previous_digest)
    os.set_inheritable(loaded, True)
    os.execve(sys.executable, [sys.executable, "-I", "-B", "/proc/self/fd/" + str(loaded), "--config", config], clean)
finally:
    for fd in archives + [socket, runtime]:
        os.close(fd)
"""


def wait_update(cfg: dict[str, Any], path: Path, digest: str, timeout: int = 35) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        installation.guard(cfg)
        try:
            result = json.loads(files.private_read(path))
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        if result == {"ok": True, "sha256": digest}:
            return
        raise ValueError("updated helper failed startup")
    raise ValueError("updated helper startup timed out")


def restore_running(cfg: dict[str, Any], path: Path, pinned: Path, original: bytes, pidfd: int,
                    result: Path, timeout: int) -> None:
    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if not poller.poll((timeout + 30) * 1000):
        raise ValueError("new helper did not stop; manual recovery required")
    _write_archive(cfg, original)
    digest = hashlib.sha256(original).hexdigest()
    result.unlink()
    canonical_result = Path(cfg["socket"]).parent / "update.result"
    child = subprocess.Popen(  # fixed hash-checking isolated launcher; reviewed 2026-11  # nosec B603
        [sys.executable, "-I", "-B", "-c", REEXEC, str(path), cfg["_config"], digest, str(canonical_result)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment(cfg), close_fds=True, start_new_session=True)
    try:
        wait_update(cfg, result, digest)
    except BaseException:
        child.terminate()
        raise


def update(cfg: dict[str, Any], path: Path, digest: str) -> None:
    installation.guard(cfg)
    policy = installation.policy(cfg, path)
    with files.trusted_open(Path(cfg["socket"]).parent, directory=True) as directory:
        lock = os.open("update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            info = os.fstat(lock)
            if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise ValueError("unsafe update lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _update(cfg, path, digest, path, footprint.fd_path(directory),
                    {policy["sha256"], policy.get("previous_sha256", "")})
        finally:
            os.close(lock)


def _write_archive(cfg: dict[str, Any], data: bytes, previous: bool = False) -> None:
    with footprint.open_home(cfg) as home:
        name = "helper.pyz.prev" if previous else "helper.pyz"
        files.write_private(footprint.fd_path(home) / name, data, 0o700)


def _loaded_bytes(cfg: dict[str, Any], path: Path) -> bytes:
    if "_loaded_archive_fd" not in cfg:
        with footprint.open_home(cfg) as home:
            return files.private_read(footprint.fd_path(home) / "helper.pyz", MAX_UPDATE)
    fd = cfg["_loaded_archive_fd"]
    info = os.fstat(fd)
    if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe loaded helper archive")
    data = os.pread(fd, MAX_UPDATE + 1, 0)
    if len(data) > MAX_UPDATE:
        raise ValueError("loaded helper archive exceeds its size limit")
    return data


def _update(cfg: dict[str, Any], path: Path, digest: str, canonical: Path, root: Path, expected: set[str]) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None\
            or urllib.parse.urlsplit(cfg["update_url"]).scheme != "https"\
            or not hmac.compare_digest(cfg["update_sha256"], digest.lower())\
            or not cfg["update_version"]:
        raise ValueError("update requires its VERSION and SHA-256 pinned in config and an HTTPS update_url")
    digest = digest.lower()
    pidfile = root / "helper.pid"
    identity = json.loads(files.private_read(pidfile))
    pid = identity["pid"]
    action_timeout = identity.get("action_timeout", cfg["action_timeout"])
    if type(action_timeout) is not int or not 1 <= action_timeout <= MAX_ACTION_TIMEOUT:
        raise ValueError("invalid running helper timeout")
    if type(pid) is not int or pid <= 1 or identity["path"] != str(canonical)\
            or files.process_start(pid) != identity["start"]:
        raise ValueError("running helper identity does not match")
    pidfd = os.pidfd_open(pid, 0)
    try:
        if files.process_start(pid) != identity["start"]:
            raise ValueError("running helper changed before download")
        signal.pidfd_send_signal(pidfd, 0)
        original = _loaded_bytes(cfg, path)
        if not any(hmac.compare_digest(hashlib.sha256(original).hexdigest(), pin) for pin in expected):
            raise ValueError("installed helper does not match its pinned SHA-256")
        old = source_version(original, canonical)
        tls = urllib.request.HTTPSHandler(context=ssl.create_default_context())
        opener = urllib.request.build_opener(tls, HTTPSRedirect())
        with deadline(30), opener.open(cfg["update_url"], timeout=30) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ValueError("update response must use https")
            data = response.read(MAX_UPDATE + 1)
        if len(data) > MAX_UPDATE or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest.lower()):
            raise ValueError("update size or SHA-256 mismatch")
        new = source_version(data, canonical)
        if new != cfg["update_version"]:
            raise ValueError("update VERSION must match update_version pinned in config")
        if version_number(new) <= version_number(old):
            raise ValueError("update VERSION must be newer")
        event = pidfile.parent / "update.json"
        message = f"manual update {old} -> {new}"
        committed = False
        acknowledged = False
        try:
            if files.process_start(pid) != identity["start"]:
                raise ValueError("running helper changed during download")
            installation.guard(cfg)
            _write_archive(cfg, original, previous=True)
            result = pidfile.parent / "update.result"
            with contextlib.suppress(FileNotFoundError):
                result.unlink()
            files.write_private(event, message.encode())
            committed = True
            _write_archive(cfg, data)
            signal.pidfd_send_signal(pidfd, signal.SIGUSR1)
            wait_update(cfg, result, digest, action_timeout + 35)
            acknowledged = True
            installation.commit(cfg, canonical, data)
        except BaseException:
            available = True
            try:
                installation.guard(cfg)
            except (OSError, ValueError):
                available = False
            if available:
                if acknowledged:
                    restore_running(cfg, canonical, path, original, pidfd, result, action_timeout)
                    print("update persistence failed; previous helper restarted", file=sys.stderr, flush=True)
                elif committed:
                    _write_archive(cfg, original)
                with contextlib.suppress(FileNotFoundError):
                    event.unlink()
            else:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            raise
        print(message, flush=True)
    finally:
        os.close(pidfd)


def main() -> None:
    print(latest_release(sys.stdin.read(4096)), flush=True)


if __name__ == "__main__":
    main()
