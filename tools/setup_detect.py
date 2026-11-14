from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from shazzzbot.helper import formats
from shazzzbot.helper.constants import RELEASE_URL, REPO
from tools.setup_files import host_identity, inspect_file, read_safe

MAX_BYTES = 1024 * 1024
MAX_CONTAINERS = 1024
WAIT = 5
SOURCE = f"https://github.com/{REPO}"
DEFAULTS = ("/mnt/user/appdata/shazzzbot", "/var/lib/shazzzbot-relay")
OS_RELEASE = Path("/etc/os-release")
INIT = Path("/proc/1/comm")
METHODS = {"compose-image", "compose-build", "docker-run", "bot-only"}
INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},'
           '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},'
           '"running":{{json .State.Running}},"state":{{json .State.Status}},'
           '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}')


def _text(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(not c.isprintable() for c in value):
        raise ValueError("invalid detection text")
    return value


def _path(value: object) -> str:
    text = _text(value)
    path = Path(text)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError("invalid detected path")
    return str(path)


def _install_path(value: object) -> str:
    path = Path(_path(value))
    if path.parts[:3] == ("/", "mnt", "user"):
        from shazzzbot.helper.footprint import resolve_home

        path = resolve_home(path)
    return str(path)


def _run(argv: list[str], cap: int = MAX_BYTES) -> bytes:
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + WAIT
    data = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(argv, WAIT)
            chunk = os.read(process.stdout.fileno(), min(65536, cap + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > cap:
                raise ValueError("detection command exceeded its limit")
        if process.wait(timeout=max(0, deadline - time.monotonic())):
            raise ValueError("detection command failed")
        return bytes(data)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        finally:
            process.stdout.close()


def _repo(image: str) -> str:
    name = image.partition("@")[0]
    tail = name.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[0] if ":" in tail else name


def _version(value: object) -> str:
    pattern = r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    if isinstance(value, str) and re.fullmatch(pattern, value):
        return "v" + value.removeprefix("v")
    return ""


def containers(image: str) -> list[dict]:
    ids = _run(["docker", "ps", "-aq"]).decode().split()
    if len(ids) > MAX_CONTAINERS or any(re.fullmatch(r"[0-9a-f]{12,64}", item) is None for item in ids):
        raise ValueError("invalid or excessive Docker container list")
    if not ids:
        return []
    raw = _run(["docker", "inspect", "--format", INSPECT, *ids]).decode()
    result = []
    for line in raw.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("labels"), (dict, type(None))) \
                or not isinstance(value.get("mounts"), list) or type(value.get("running")) is not bool:
            raise ValueError("invalid Docker inspection")
        labels = value.get("labels") or {}
        if value.get("health") not in {None, "healthy", "starting", "unhealthy"} \
                or value.get("state") not in {None, "running", "created", "restarting", "removing",
                                              "paused", "exited", "dead"}:
            raise ValueError("invalid Docker health or state")
        labels = {key: _text(labels.get(key, "")) for key in (
            "org.shazzzbot.install", "org.opencontainers.image.source", "org.opencontainers.image.version",
            "com.docker.compose.project", "com.docker.compose.project.working_dir", "com.docker.compose.service")}
        found_image = _text(value.get("image"))
        repo = _repo(found_image)
        own_image = repo == _repo(image) or repo == "shazzzbot" \
            or re.fullmatch(r"shazzzbot-[0-9a-f]{16}", repo) is not None
        ours = own_image or labels["org.opencontainers.image.source"].removesuffix(".git") == SOURCE
        proxy = labels["com.docker.compose.service"] == "docker-proxy" or repo == "haproxy" \
            and re.fullmatch(r"[0-9a-f]{32}", labels["org.shazzzbot.install"]) is not None
        if not ours and not proxy:
            continue
        mounts = []
        for item in value["mounts"]:
            if not isinstance(item, dict):
                raise TypeError("invalid Docker mount")
            destination = _text(item.get("Destination", ""))
            if destination in {"/config", "/config/config.toml", "/config/.env", "/data"}:
                mounts.append({"source": _path(item.get("Source")), "destination": destination})
        result.append({"id": _text(value.get("id")), "name": _text(value.get("name")).lstrip("/"),
                       "image": found_image, "labels": labels, "mounts": mounts,
                       "running": value["running"], "proxy": proxy,
                       "health": value.get("health"), "state": value.get("state")})
    return result


def _read_json(path: Path) -> dict:
    raw = read_safe(path, private=True)
    if len(raw) > 65536:
        raise ValueError("detection metadata exceeds its limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("invalid detection metadata")
    return value


def _identity(path: Path, machine: str) -> dict | None:
    try:
        value = formats.load(_read_json(path / ".setup-identity"), "install identity", 2)
    except FileNotFoundError:
        return None
    if value.get("host") != machine or value.get("app") != str(path) \
            or not isinstance(value.get("install_id"), str) \
            or re.fullmatch(r"[0-9a-f]{32}", value["install_id"]) is None \
            or value.get("role") not in {"bot", "relay"} or value.get("method") not in METHODS \
            or not isinstance(value.get("ref"), str) \
            or not (_version(value["ref"]) or re.fullmatch(r"[0-9a-f]{40}", value["ref"])):
        raise ValueError("untrusted install identity")
    return value


def _presence(path: Path, *, private: bool = False) -> str:
    try:
        if path.name == "config.toml":
            read_safe(path, private=private)
        else:
            inspect_file(path, private=private)
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "untrusted"
    return "present"


def _locations(rows: list[dict], requested: str) -> list[str]:
    paths = [requested] if requested else list(DEFAULTS)
    for row in rows:
        if row["proxy"]:
            continue
        working = row["labels"]["com.docker.compose.project.working_dir"]
        if working:
            paths.append(_path(working))
        for mount in row["mounts"]:
            source = Path(mount["source"])
            paths.append(str(source if mount["destination"] == "/config" else source.parent))
    result = []
    for path in paths:
        try:
            result.append(_install_path(path))
        except (OSError, ValueError):
            if requested or path not in DEFAULTS:
                raise
    return list(dict.fromkeys(result))


def _installed(path: str, rows: list[dict], machine: str) -> dict | None:
    root = Path(path)
    identity = _identity(root, machine)
    config = _presence(root / "config.toml", private=True)
    env = _presence(root / ".env", private=True)
    if identity is None and config == env == "missing":
        return None
    mine = [row for row in rows if not row["proxy"] and identity is not None
            and row["labels"]["org.shazzzbot.install"] == identity["install_id"]]
    missing = []
    if identity is None:
        missing.append("install identity")
    else:
        if env != "present":
            missing.append(f".env ({env})")
        if identity["role"] == "bot" and config != "present":
            missing.append(f"config.toml ({config})")
        if not mine:
            missing.append("owned application container")
        elif not any(row["running"] for row in mine):
            missing.append("running application container")
        elif sum(row["running"] for row in mine) > 1:
            missing.append("unambiguous running application container")
        elif any(row["running"] and (row.get("health") in {"starting", "unhealthy"}
                                     or row.get("state") in {"restarting", "dead"}) for row in mine):
            missing.append("healthy application container")
        if identity["role"] == "bot" and identity["method"] != "bot-only" and not any(
                row["proxy"] and row["labels"]["org.shazzzbot.install"] == identity["install_id"]
                and row["running"] for row in rows):
            missing.append("running Docker socket proxy")
    active = [row for row in mine if row["running"]]
    running = active[0] if len(active) == 1 else None
    version = _version(running["labels"]["org.opencontainers.image.version"]) if running else ""
    if not version and running:
        version = _version(running["image"].rsplit(":", 1)[-1])
    if not version and identity:
        version = _version(identity.get("ref"))
    return {"app": path, "identity": identity, "containers": mine, "missing": missing,
            "version": version, "owned": identity is not None and bool(mine)}


def latest_release(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None \
            or parsed.query or parsed.fragment:
        return ""
    try:
        raw = _run(["curl", "-q", "-fsSL", "--proto", "=https", "--max-time", str(WAIT),
                    "--max-filesize", "65536", "--", url], 65536)
        value = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        return ""
    return _version(value.get("tag_name")) if isinstance(value, dict) else ""


def platform(marker: str) -> str:
    try:
        read_safe(Path(_path(marker)))
    except FileNotFoundError:
        try:
            data = read_safe(OS_RELEASE.resolve(strict=True)).decode()
            found = re.search(r'^ID=["\']?([a-zA-Z0-9_.-]+)["\']?$', data, re.MULTILINE)
            distro = found[1] if found else "unknown"
        except (OSError, ValueError):
            distro = "unknown"
        try:
            init = _text(read_safe(INIT).decode().strip())
        except (OSError, ValueError):
            init = "unknown"
        return f"{distro} (init {init})"
    return "Unraid"


def _helper(helper: str, hook: str) -> list[str]:
    home, entry = Path(_path(helper)), Path(_path(hook))
    identity = _presence(home / ".identity.json", private=True)
    if identity == "missing" and _presence(home / "helper.pyz") == "missing" \
            and _presence(entry / "script") == "missing":
        return ["Helper: not installed"]
    missing = [name for name in ("helper.pyz", "config.json", "api.key", "policy.json", ".identity.json")
               if _presence(home / name, private=True) != "present"]
    if _presence(entry / "script") != "present":
        missing.append("startup hook")
    schedule = entry.parent.parent / "schedule.json"
    try:
        values = _read_json(schedule)
        active = values.get("shazzzbot-helper") == {"frequency": "boot", "script": str(entry / "script")}
    except (OSError, ValueError, TypeError, RecursionError):
        active = False
    if not active:
        missing.append("startup schedule")
    status = "partial or broken: " + ", ".join(missing) if missing else "installed; startup schedule present"
    return ["Helper: " + status, "Helper entry: " + str(entry / "script"),
            "Helper schedule: " + str(schedule)]


def detect(app: str, helper: str, hook: str, image: str, latest: str, role: str, marker: str) -> dict:
    detected = platform(marker)
    lines = ["Platform: " + detected]
    report = {"state": "not-installed", "summary": "Shazzzbot: not installed", "lines": lines,
              "app": "", "role": "", "method": "", "ref": "",
              "platform": "unraid" if detected == "Unraid" else detected.split(" ", 1)[0], "current": False}
    try:
        rows = containers(_text(image))
        machine = host_identity()
        installations = [value for path in _locations(rows, app)
                         if (value := _installed(path, rows, machine)) is not None]
    except formats.FormatError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        report.update(state="unavailable", summary="Shazzzbot: detection unavailable (untrusted or unreadable data)")
        return report
    candidates = [value for value in installations if (not app or value["app"] == _install_path(app))
                  and (not role or value["identity"] is None or value["identity"]["role"] == role)]
    owned = [value for value in candidates if value["owned"]]
    if len(owned) == 1:
        selected = owned[0]
        identity = selected["identity"]
        report.update(app=selected["app"], role=identity["role"], method=identity["method"],
                      ref=identity.get("ref", ""))
    else:
        selected = candidates[0] if len(candidates) == 1 else None
    for value in candidates:
        lines.append("Install directory: " + value["app"])
        lines.extend("Container: " + row["name"] + " (" + row["image"] + ")" for row in value["containers"])
        lines.append("Config: " + str(Path(value["app"]) / "config.toml"))
        lines.append("Environment: " + str(Path(value["app"]) / ".env"))
        for row in value["containers"]:
            project = row["labels"]["com.docker.compose.project"]
            if project:
                lines.append("Compose project: " + project)
            working = row["labels"]["com.docker.compose.project.working_dir"]
            if working:
                lines.append("Compose directory: " + working)
    foreign = []
    for row in rows:
        if not row["proxy"] and not any(row in value["containers"] for value in installations):
            foreign.append(row)
            lines.append("Unowned candidate: " + row["name"] + "; left unchanged")
    helper_lines = _helper(helper, hook)
    lines.extend(helper_lines)
    if len(owned) > 1:
        report.update(state="partial", summary="Shazzzbot: multiple installs found; select --dir and --role")
    elif not candidates and foreign:
        report.update(state="partial", summary="Shazzzbot: unowned containers found; left unchanged")
    elif selected is not None:
        if selected["missing"]:
            report.update(state="partial", summary="Shazzzbot: partial or broken install; missing "
                          + ", ".join(selected["missing"]))
        else:
            installed = selected["version"]
            available = _version(latest) or latest_release(os.environ.get("SHAZZZBOT_RELEASE_API", RELEASE_URL))
            newer = available and installed and tuple(map(int, available[1:].split("."))) \
                > tuple(map(int, installed[1:].split(".")))
            status = (available + " available" if newer else "current" if available and installed
                      else "latest unavailable")
            report.update(state="installed",
                          summary=f"Shazzzbot: installed {installed or 'version unknown'} ({status})")
            report["current"] = available == installed and bool(installed) and _unchanged(selected) \
                and not any("partial or broken" in line for line in helper_lines)
    return report


def emit(report: dict) -> str:
    fields = [(key, _text(report[key])) for key in ("app", "role", "method", "ref", "state", "platform")]
    fields.append(("current", "1" if report["current"] else "0"))
    fields += [("line", _text(report["summary"])), *(("line", _text(line)) for line in report["lines"])]
    return "\n".join(f"{key}\t{value}" for key, value in fields)


def _unchanged(selected: dict) -> bool:
    from tools.setup_manage import managed_names

    identity = selected["identity"]
    if identity["method"] == "compose-build" or not isinstance(identity.get("files"), dict):
        return False
    names = (".env", "config.toml") if identity["role"] == "bot" else (".env",)
    root = Path(selected["app"])
    try:
        from tools.setup_layout import state as setup_state

        state = setup_state(root).split()
        if state != [identity["method"], identity["role"], identity["ref"]]:
            return False
        return all(identity["files"].get(name) == hashlib.sha256(read_safe(root / name,
                   private=name in {".env", "config.toml"})).hexdigest()
                   for name in (*names, *managed_names(identity["role"], identity["method"])))
    except formats.FormatError:
        raise
    except (OSError, ValueError):
        return False
