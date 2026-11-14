"""Unauthenticated, read-only NUT monitoring with bounded replies."""
import asyncio
import contextlib
import ipaddress
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

TIMEOUT = 2.0
MAX_BYTES = 8192
VARIABLES = ("ups.status", "battery.charge", "battery.runtime", "ups.load")
STATUS = {"OL": "Online", "OB": "On battery", "LB": "Low battery", "HB": "High battery",
          "RB": "Replace battery", "CHRG": "Charging", "DISCHRG": "Discharging", "BYPASS": "Bypass",
          "CAL": "Calibrating", "OFF": "Off", "OVER": "Overloaded", "TRIM": "Trimming voltage",
          "BOOST": "Boosting voltage", "FSD": "Forced shutdown", "ALARM": "Alarm", "WAIT": "Waiting for data"}
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    status: tuple[str, ...] = ()
    charge: float | None = None
    runtime: float | None = None
    load: float | None = None

    @property
    def words(self) -> str:
        return ", ".join(STATUS.get(token, token) for token in self.status)


def target(value: object) -> tuple[str, str, int]:
    if not isinstance(value, str) or len(value) > 384 or not re.fullmatch(r"[A-Za-z0-9_.:@\[\]-]+", value):
        raise ValueError("invalid UPS target")
    parsed = urlsplit("nut://" + value)
    name, host, port = parsed.username, parsed.hostname, parsed.port
    if not name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name) or not host \
            or parsed.password is not None or parsed.netloc.endswith(":") or len(host) > 253:
        raise ValueError("invalid UPS target")
    if "[" in parsed.netloc:
        ipaddress.IPv6Address(host)
    elif "." in host and all(c in "0123456789." for c in host):
        ipaddress.IPv4Address(host)
    elif not all(re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
                 for label in host.removesuffix(".").split(".")):
        raise ValueError("invalid UPS host")
    if port == 0:
        raise ValueError("invalid UPS port")
    return name, host, port if port is not None else 3493


def _number(value: str | None, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def parse(values: Mapping[str, str | None]) -> Reading:
    raw = values.get("ups.status") or ""
    tokens = tuple(dict.fromkeys(raw.split()))
    if not tokens or len(raw) > 256 or any(not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", token) for token in tokens):
        return Reading()
    return Reading(tokens, _number(values.get("battery.charge"), 100),
                   _number(values.get("battery.runtime"), 365 * 86400), _number(values.get("ups.load"), 1000))


def _reply(line: bytes, name: str, variable: str) -> str | None:
    if line in (b"ERR VAR-NOT-SUPPORTED\n", b"ERR VAR-NOT-SUPPORTED\r\n"):
        return None
    match = re.fullmatch(r'VAR ' + re.escape(name) + " " + re.escape(variable) + r' "([^"\\\r\n]*)"\r?\n',
                         line.decode("ascii"))
    if not match:
        raise ValueError("invalid NUT reply")
    return match[1]


async def read(endpoint: str) -> Reading:
    writer = None
    try:
        name, host, port = target(endpoint)
        async with asyncio.timeout(TIMEOUT):
            reader, writer = await asyncio.open_connection(host, port, limit=MAX_BYTES)
            values = {}
            count = 0
            for variable in VARIABLES:
                writer.write(f"GET VAR {name} {variable}\n".encode("ascii"))
                await writer.drain()
                line = await reader.readline()
                count += len(line)
                if count > MAX_BYTES:
                    raise ValueError("NUT reply exceeded the byte limit")
                values[variable] = _reply(line, name, variable)
            return parse(values)
    except (OSError, TimeoutError, ValueError) as e:
        log.warning("UPS monitoring failed: %s", e)
        return Reading()
    finally:
        if writer is not None:
            with contextlib.suppress(OSError, TimeoutError):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), TIMEOUT)
