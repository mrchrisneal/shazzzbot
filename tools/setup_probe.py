from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

EMHTTP = Path("/var/local/emhttp")
LOCALTIME = Path("/etc/localtime")
TIMEOUT = 3
MAX_RUN = 1024 * 1024
MAX_READ = 64 * 1024
MAX_CONTAINERS = 200
MAX_TEXT = 512
NETDATA_PORT = 19999
UPS_PORT = 3493
NUT = ("127.0.0.1", UPS_PORT)
DOCKER_FORMAT = ('{"name":{{json .Names}},"image":{{json .Image}},"ports":{{json .Ports}},'
                 '"networks":{{json .Networks}},"webui":{{json (.Label "net.unraid.docker.webui")}}}')
PORT = re.compile(r"\[PORT:(\d{1,5})\]")
PUBLISHED = re.compile(r":(\d{1,5})->(\d{1,5})/tcp")
UPS_LINE = re.compile(r'UPS ([A-Za-z0-9_.-]{1,64}) "')
TZ_NAME = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")
Row = dict[str, str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def parse_ini(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or key.startswith((";", "#", "[")):
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def read_ini(name: str) -> dict[str, str]:
    try:
        with open(EMHTTP / name, "rb") as handle:
            data = handle.read(MAX_RUN + 1)
        if len(data) > MAX_RUN:
            return {}
        return parse_ini(data.decode())
    except (OSError, UnicodeError):
        return {}


def run(argv: list[str]) -> str | None:
    try:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            timer = threading.Timer(TIMEOUT, proc.kill)
            timer.start()
            try:
                data = proc.stdout.read(MAX_RUN + 1) if proc.stdout else b""
                if len(data) > MAX_RUN:
                    proc.kill()
                    return None
                code = proc.wait()
            finally:
                timer.cancel()
        return data.decode() if code == 0 else None
    except (OSError, UnicodeError):
        return None


def drain(read: Callable[[int], bytes], stop: bytes | None = None) -> bytes | None:
    deadline = time.monotonic() + TIMEOUT
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = read(4096)
        if not chunk:
            return bytes(data) if stop is None else None
        data += chunk
        if len(data) > MAX_READ:
            return None
        if stop is not None and stop in data:
            return bytes(data)
    return None


def fetch_json(url: str) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url, timeout=TIMEOUT) as response:
            body = drain(response.read1)
        return None if body is None else json.loads(body)
    except (OSError, ValueError, UnicodeError):
        return None


def http_json(url: str) -> object:
    box: list[object] = []
    worker = threading.Thread(target=lambda: box.append(fetch_json(url)), daemon=True)
    worker.start()
    worker.join(TIMEOUT)
    return box[0] if box else None


def valid_ip(text: str | None) -> str | None:
    try:
        address = ipaddress.IPv4Address((text or "").strip())
    except ValueError:
        return None
    return None if address.is_loopback or address.is_unspecified else str(address)


def parse_route(text: str | None) -> str | None:
    tokens = (text or "").split()
    if "src" not in tokens[:-1]:
        return None
    return valid_ip(tokens[tokens.index("src") + 1])


def host_ip() -> str | None:
    configured = valid_ip(read_ini("network.ini").get("IPADDR:0"))
    return configured or parse_route(run(["ip", "-4", "route", "get", "1.1.1.1"]))


def valid_tz(name: str | None) -> str | None:
    if not name or len(name) > 64 or not TZ_NAME.fullmatch(name):
        return None
    try:
        zoneinfo.ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return None
    return name


def timezone() -> str | None:
    name = valid_tz(read_ini("var.ini").get("timeZone"))
    if name:
        return name
    try:
        target = os.readlink(LOCALTIME)
    except OSError:
        return None
    _, sep, tail = target.rpartition("zoneinfo/")
    return valid_tz(tail) if sep else None


def parse_ports(text: str) -> dict[int, int]:
    ports: dict[int, int] = {}
    for host, inner in PUBLISHED.findall(text):
        ports.setdefault(int(inner), int(host))
    return ports


def resolve_webui(template: str, host_ip: str | None, ports: dict[int, int], network: str) -> str:
    host_mode = "host" in network.split(",")

    def port(match: re.Match[str]) -> str:
        number = int(match.group(1))
        mapped = number if host_mode else ports.get(number)
        return str(mapped) if mapped else match.group(0)

    url = PORT.sub(port, template.replace("[IP]", host_ip) if host_ip else template)
    if not _text(url) or "[" in url or "]" in url or " " in url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.port == 0:
            return ""
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc or parts.query:
        return ""
    return url


def _text(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= MAX_TEXT and value.isprintable():
        return value
    return None


def parse_containers(text: str, host_ip: str | None) -> list[Row]:
    rows: list[Row] = []
    for line in text.splitlines():
        if len(rows) >= MAX_CONTAINERS:
            break
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        name, image = _text(item.get("name")), _text(item.get("image"))
        if not name or not image:
            continue
        ports, network, webui = (value if isinstance(value := item.get(key), str) else ""
                                 for key in ("ports", "networks", "webui"))
        rows.append({"name": name, "image": image, "webui": resolve_webui(webui, host_ip, parse_ports(ports), network)})
    return rows


def containers(host_ip: str | None) -> list[Row] | None:
    text = run(["docker", "ps", "--format", DOCKER_FORMAT])
    return None if text is None else parse_containers(text, host_ip)


def netdata(host_ip: str | None) -> str | None:
    if not host_ip:
        return None
    body = http_json(f"http://127.0.0.1:{NETDATA_PORT}/api/v1/alarms")
    return f"http://{host_ip}:{NETDATA_PORT}" if isinstance(body, dict) and "alarms" in body else None


def jellyfin(rows: list[Row], host_ip: str | None) -> str | None:
    row = next((row for row in rows if "jellyfin" in row["image"].lower()), None)
    if row is None:
        return None
    if row["webui"]:
        parts = urlsplit(row["webui"])
        base = f"{parts.scheme}://{parts.netloc}"
    elif host_ip:
        base = f"http://{host_ip}:8096"
    else:
        return None
    body = http_json(base + "/System/Info/Public")
    return base if isinstance(body, dict) and "Id" in body else None


def parse_ups(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        match = UPS_LINE.match(line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def nut_list() -> str | None:
    try:
        with socket.create_connection(NUT, timeout=TIMEOUT) as sock:
            sock.sendall(b"LIST UPS\n")
            body = drain(sock.recv, b"END LIST UPS")
        return None if body is None else body.decode()
    except (OSError, UnicodeError):
        return None


def ups(host_ip: str | None) -> list[str] | None:
    if not host_ip:
        return None
    names = parse_ups(nut_list() or "")
    return [f"{name}@{host_ip}:{UPS_PORT}" for name in names] or None


def probe() -> dict[str, object]:
    ip = host_ip()
    rows = containers(ip)
    found: dict[str, object] = {"host_ip": ip, "timezone": timezone(), "containers": rows,
                                "netdata": netdata(ip), "jellyfin": jellyfin(rows or [], ip), "ups": ups(ip)}
    return {key: value for key, value in found.items() if value is not None}


def main() -> int:
    print(json.dumps(probe()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
