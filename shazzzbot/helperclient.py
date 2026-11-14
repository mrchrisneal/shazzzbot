"""Signed read-only requests to the root helper over its Unix socket."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from typing import cast

from . import schema
from .helper.auth import canonical
from .helper.constants import PROTOCOL, version_number
from .helper.platforms.task_durations import _number
from .helper.platforms.task_kinds import HEAVY_KINDS
from .jobs import STALE, _cron

TIMEOUT = 15.0
MAX_RESPONSE = 1024 * 1024 + 4096
TASK_FIELDS = {"generated", "jobs", "smart", "os_update", "running", "finished", "durations"}
HELLO_FIELDS = ("protocol", "version", "sha256", "bundle_version", "entry_sha256")


class Error(Exception):
    pass


class ProtocolMismatch(Error):
    def __init__(self, expected: int, received: int):
        self.expected = expected
        self.received = received
        super().__init__(f"helper protocol mismatch: expected {expected}, received {received}; "
                         "rerun setup.sh --helper")


@dataclass(frozen=True)
class Settings:
    socket: str
    key_env: str


def _key(settings: Settings) -> bytes:
    key = os.environ.get(settings.key_env, "").strip()
    if len(key) > 4096 or re.fullmatch(r"[A-Za-z0-9_-]{32,}", key) is None:
        raise Error("helper key is unavailable or invalid")
    return key.encode("ascii")


def validate(value: object) -> Settings | None:
    if value is None:
        return None
    schema.validate("helper", value, "helper")
    raw = cast(dict, value)
    path, env = raw["socket"], raw["key_env"]
    if not path.startswith("/") or not path.isprintable() or "\0" in path \
            or len(os.fsencode(path)) > 107 or any(part in ("", ".", "..") for part in path.split("/")[1:]):
        raise SystemExit("helper.socket must be an absolute Unix socket path of at most 107 bytes")
    settings = Settings(path, env)
    try:
        _key(settings)
    except Error:
        raise SystemExit("helper.key_env must name a set environment variable containing a valid helper key") from None
    return settings


def _name(value: object, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= 256 and value.isprintable()


def _times(value: object) -> bool:
    return isinstance(value, dict) and all(_name(name) and _number(at) for name, at in value.items())


def tasks_data(value: object) -> dict:
    if not isinstance(value, dict) or not TASK_FIELDS <= value.keys():
        raise Error("helper returned invalid tasks")
    at = value["generated"]
    if not _number(at) or not -30 <= time.time() - at <= STALE:
        raise Error("helper task data is not fresh")
    jobs, smart, durations = value["jobs"], value["smart"], value["durations"]
    if not isinstance(jobs, list) or not all(isinstance(job, dict) and {"name", "cron"} <= job.keys()
            and _name(job["name"]) and isinstance(job["cron"], str) and len(job["cron"]) <= 256
            and len(job["cron"].split()) == 5 and _cron(job["cron"].split()) is not None for job in jobs):
        raise Error("helper returned invalid schedules")
    if not isinstance(smart, list) or not all(_name(name) for name in smart) \
            or not _name(value["os_update"], empty=True) \
            or not _times(value["running"]) or not _times(value["finished"]):
        raise Error("helper returned invalid task state")
    if not isinstance(durations, dict) or not all(_name(name) and isinstance(record, dict)
            and {"at", "seconds"} <= record.keys() and _number(record["at"])
            and (record["seconds"] is None or _number(record["seconds"])) for name, record in durations.items()):
        raise Error("helper returned invalid durations")
    kinds = value.get("kinds", {})
    if not isinstance(kinds, dict) or not all(name in value["running"] and isinstance(kind, str)
                                            and kind in HEAVY_KINDS for name, kind in kinds.items()):
        raise Error("helper returned invalid task kinds")
    result = {key: value[key] for key in TASK_FIELDS}
    result["jobs"] = [{"name": job["name"], "cron": job["cron"]} for job in jobs]
    result["durations"] = {name: {"at": record["at"], "seconds": record["seconds"]}
                           for name, record in durations.items()}
    if "kinds" in value:
        result["kinds"] = dict(kinds)
    return result


def shutdown_data(value: object) -> dict:
    if isinstance(value, dict) and not {"mode", "value"} & value.keys():
        return {}
    if not isinstance(value, dict) or not {"mode", "value"} <= value.keys() \
            or not isinstance(value["mode"], str) or value["mode"] not in {"batt_level", "batt_timer", "sec_timer"}:
        raise Error("helper returned invalid UPS shutdown data")
    number = value["value"]
    if not isinstance(number, (int, float)) or isinstance(number, bool):
        raise Error("helper returned invalid UPS shutdown value")
    try:
        valid = number >= 0 and math.isfinite(number)
    except OverflowError:
        valid = False
    if not valid:
        raise Error("helper returned invalid UPS shutdown value")
    return {"mode": value["mode"], "value": number}


def _object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise Error("helper returned duplicate fields")
        value[key] = item
    return value


def _constant(_: str) -> None:
    raise Error("helper returned a non-finite number")


def _protocol(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 65535 else None


def _version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return value == ".".join(map(str, version_number(value)))
    except ValueError:
        return False


async def _receive(reader: asyncio.StreamReader, action: str) -> dict:
    total = 0
    payload = None
    while True:
        line = await reader.readuntil(b"\n")
        total += len(line)
        if total > MAX_RESPONSE:
            raise Error("helper response is too large")
        value = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict):
            raise Error("helper returned invalid data")
        if "error" in value:
            if value["error"] == "protocol mismatch":
                received = _protocol(value.get("expected"))
                claimed = _protocol(value.get("received"))
                if received is None or received == PROTOCOL or claimed != PROTOCOL:
                    raise Error("helper returned an invalid protocol rejection")
                raise ProtocolMismatch(PROTOCOL, received)
            raise Error("helper refused the request")
        if "done" in value:
            if not {"done", "secs"} <= value.keys() or type(value["done"]) is not int or not _number(value["secs"]):
                raise Error("helper returned an invalid completion")
            if value["done"] != 0:
                raise Error("helper action failed")
            if payload is None or await reader.read(1):
                raise Error("helper returned an incomplete or trailing response")
            return payload
        if payload is not None:
            raise Error("helper returned multiple payloads")
        if action == "tasks":
            if "tasks" not in value:
                raise Error("helper returned an invalid tasks response")
            payload = tasks_data(value["tasks"])
        elif action == "ups_shutdown":
            if "ups_shutdown" not in value:
                raise Error("helper returned an invalid UPS shutdown response")
            payload = shutdown_data(value["ups_shutdown"])
        else:
            received = _protocol(value.get("protocol"))
            if received is None:
                raise Error("helper returned invalid identity")
            if received != PROTOCOL:
                raise ProtocolMismatch(PROTOCOL, received)
            if not set(HELLO_FIELDS) <= value.keys() \
                    or not all(_version(value[key]) for key in ("version", "bundle_version")) \
                    or not all(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]) is not None
                               for key in ("sha256", "entry_sha256")):
                raise Error("helper returned invalid identity")
            payload = {key: value[key] for key in HELLO_FIELDS}


async def request(settings: Settings, action: str) -> dict:
    if action not in {"hello", "tasks", "ups_shutdown"}:
        raise Error("helper client permits read-only actions only")
    body = {"protocol": PROTOCOL, "action": action, "args": {}, "ts": time.time(), "nonce": secrets.token_hex(16)}
    body["sig"] = hmac.new(_key(settings), canonical(body), hashlib.sha256).hexdigest()
    writer = None
    try:
        async with asyncio.timeout(TIMEOUT):
            reader, writer = await asyncio.open_unix_connection(settings.socket, limit=MAX_RESPONSE)
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", peer)[1] != 0:
                raise Error("helper peer must be root")
            writer.write(canonical(body) + b"\n")
            await writer.drain()
            return await _receive(reader, action)
    except TimeoutError:
        raise Error("helper timed out") from None
    except OSError:
        raise Error("helper is unreachable") from None
    except asyncio.LimitOverrunError:
        raise Error("helper response is too large") from None
    except (ValueError, TypeError, RecursionError, asyncio.IncompleteReadError):
        raise Error("helper returned invalid data") from None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
