"""The inputs Bot.collect reads, as JSON, and one frame rendered from them by the bot's own code.

The live preview dumps a running bot; the demo dumps invented data. The browser loads either into a fresh Bot under
Pyodide and renders text, rich, and image views exactly as the bot would post them.
"""
import dataclasses
import json
import math
import os
import re
import tempfile
from array import array
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Any, cast
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import discord
from PIL import Image

from . import board_delivery, config, delivery, helperclient, jobs, panel, previewcfg, sysimage, unraid, view
from . import bot as B
from .boards import Message
from .docker import Container, Engine
from .helper import formats
from .host import Snapshot
from .services import Result, Service
from .ups import Reading

FORMAT = 1
MODES: tuple[tuple[str, str | None], ...] = (("text", "text"), ("rich", "embed"), ("image", "image"),
                                             ("default", None))


class Clock:
    def __init__(self, t: float) -> None:
        self.t, self.mono = t, 1000.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.mono


def frozen(clock: Clock) -> type[datetime]:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime.fromtimestamp(clock.t, tz)
    return Frozen


@contextmanager
def at(clock: Clock, cores: int) -> Iterator[None]:
    with ExitStack() as stack:
        for name in ("time", "monotonic"):
            stack.enter_context(mock.patch.object(B.time, name, getattr(clock, name)))
        moment = frozen(clock)
        stack.enter_context(mock.patch.object(B, "datetime", moment))
        stack.enter_context(mock.patch.object(jobs, "datetime", moment))
        stack.enter_context(mock.patch.object(panel, "CORES", cores))
        stack.enter_context(mock.patch.object(sysimage, "CORES", cores))
        yield


def _url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    host = p.hostname or ""
    netloc = (f"[{host}]" if ":" in host else host) + (f":{p.port}" if p.port else "")
    return urlunsplit((p.scheme, netloc, p.path, "", ""))


def _plain(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [_plain(v) for v in value]
    return value


def _number(value: object) -> float | None:
    number = cast(float, value)
    return float(number) if type(value) in (int, float) and 0 <= number <= 1e12 else None


def _delivery(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, record in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+:(text|embed|image)", key) \
                or not isinstance(record, dict):
            continue
        success = _number(record.get("last_success"))
        reason = record.get("reason", "")
        clean[key] = {"last_success": success, "last_failed": record.get("last_failed") is True,
                      "reason": reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z_]{0,64}", reason) else ""}
    return clean


def _scheduled(value: object) -> dict[str, dict] | None:
    if value is None:
        return None
    clean: dict[str, dict] = {}
    if not isinstance(value, dict):
        return clean
    for key, record in value.items():
        if isinstance(key, str) and re.fullmatch(r"[a-z0-9_]+:(text|embed|image)", key) \
                and isinstance(record, dict) and (nominal := _number(record.get("nominal"))):
            clean[key] = {"next": _number(record.get("next")), "nominal": nominal,
                          "image_gap": _number(record.get("image_gap"))}
    return clean


def _schedule(bot: B.Bot, now: float) -> dict:
    records = {}
    images = delivery.message_schedule(bot, now)
    for message in bot.main.messages:
        edits = delivery.edit_boundary(bot, message, now)
        dates = images.get(delivery.identity(message), [])
        image_at = dates[0] if dates else None
        gap = dates[1] - dates[0] if len(dates) > 1 else None
        for key in message.embed_keys:
            kind = message.modes.get(key, "embed")
            image = kind == "image"
            records[f"{key}:{kind}"] = {"next": image_at if image else edits,
                                       "nominal": message.image_every if image else message.edit_every,
                                       "image_gap": gap if image else None}
    return _scheduled(records) or {}


async def dump(bot: B.Bot) -> dict:
    """What the bot would render now; nothing secret: no token, ids, URL credentials, or container commands."""
    now = B.time.time()
    tasks = await bot._read_tasks()
    shutdown = await bot._read_ups_shutdown()
    arr = bot.array or await B.asyncio.to_thread(unraid.read, bot.cfg.emhttp)
    apps = bot.app_history
    return formats.dump({
        "now": now, "cores": panel.CORES, "interval": bot.cfg.interval, "window": bot.window,
        "snap": _plain(dataclasses.asdict(bot._snap or Snapshot())),
        "loads": _plain(bot._loads),
        "history": [list(r) for r in bot.history.samples if r[0] >= now - bot.window],
        "apps": formats.dump({"samples": _plain(apps.samples),
                 "instances": {m: {n: [[*k, t] for k, t in ids.items()] for n, ids in names.items()}
                               for m, names in apps.instances.items()}}, 2),
        "array": dataclasses.asdict(arr) if arr else None,
        "helper": bot.cfg.helper is not None, "tasks": tasks, "ups_shutdown": shutdown,
        "helper_node": dataclasses.asdict(bot.helper_node) if bot.helper_node else None,
        "nodes": _plain(bot.nodes),
        "ups": _plain(dataclasses.asdict(bot._ups)) if bot._ups else None,
        "docker": bot.docker is not None, "engine": list(bot.engine) if bot.engine else None,
        "containers": [_plain(dataclasses.replace(c, command=c.command if "watchtower" in c.image.lower() else ""
                                                  ).__dict__) for c in bot.containers],
        "services": [dataclasses.replace(s, url=_url(s.url), public_url=_url(s.public_url), vpn_url=_url(s.vpn_url),
                                         expect="").__dict__ for s in bot.cfg.services],
        "results": [list(r) for r in bot.svc_results], "exposed": _plain(bot.exposed),
        "render": dict(bot.cfg.outputs[0].render), "render_base": dict(bot.cfg.render),
        "sections": bot.main.sections, "timing": timing(bot),
        "budget": _budget(bot), "delivery": _delivery(getattr(bot, "_preview_delivery", {})),
        "schedule": _schedule(bot, now),
    }, FORMAT)


def _budget(bot: B.Bot) -> dict | None:
    """The learned Discord limits with each channel id replaced: the main output becomes the preview's channel 2."""
    raw = bot.rate_budget.dump()
    if raw is None:
        return None
    ids = {bot.main.channel_id: 2}

    def anon(key: str) -> str:
        return re.sub(r"/channels/(\d+)", lambda m: f"/channels/{ids.setdefault(int(m[1]), len(ids) + 2)}", key)
    return {**raw, "routes": {anon(k): v for k, v in raw["routes"].items()},
            "evidence": {anon(k): v for k, v in raw["evidence"].items()}}


def _snapshot(raw: dict) -> Snapshot:
    s = Snapshot(**raw)
    s.load = tuple(s.load)  # type: ignore[assignment]
    s.app_instances = {k: frozenset(tuple(x) for x in v) for k, v in s.app_instances.items()}  # type: ignore[misc]
    s.grouped_apps, s.procs = frozenset(s.grouped_apps), frozenset(s.procs)
    s.top_cpu = [tuple(x) for x in s.top_cpu]  # type: ignore[misc]
    s.top_ram = [tuple(x) for x in s.top_ram]  # type: ignore[misc]
    s.disk_io = {k: tuple(v) for k, v in s.disk_io.items()}  # type: ignore[misc]
    return s


def _array(raw: dict | None) -> unraid.Array | None:
    if raw is None:
        return None
    return unraid.Array(**{**raw, "parity": unraid.Parity(**raw["parity"]),
                           "disks": [unraid.Disk(**d) for d in raw["disks"]]})


def load(data: dict, cfg: config.Config, tmp: str) -> B.Bot:
    """A Bot holding the dumped inputs and the edited display settings, ready for collect()."""
    data = formats.load(data, "preview snapshot", FORMAT)
    # The caller creates this private TemporaryDirectory; snapshot data cannot choose it. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    cfg.state_file, cfg.emhttp, cfg.host_root = (os.path.join(tmp, "state.json"), os.path.join(tmp, "emhttp"),
                                                 os.path.join(tmp, "host"))
    cfg.interval = data["interval"]
    cfg.services = [Service(**s) for s in data["services"]]
    # This simulated socket is inside the same caller-owned temporary directory. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    cfg.helper = helperclient.Settings(os.path.join(tmp, "helper.sock"), "SHAZZZBOT_PREVIEW") \
        if data["helper"] else None
    cfg.ups = "ups@preview" if data["ups"] is not None else ""
    cfg.outputs[0].render = dict(data.get("render", {}))
    bot = B.Bot(cfg)
    bot.main.sections = data.get("sections")
    when = data.get("timing", {})
    bot.main.edit_every = when.get("edit_every", bot.main.edit_every)
    bot.main.image_every = when.get("image_every", bot.main.image_every)
    bot.rate_budget.restore(data.get("budget"))
    cast(Any, bot)._preview_delivery = _delivery(data.get("delivery"))
    live = {**data.get("render_base", {}), **cfg.outputs[0].render}
    same = all(live.get(s, "image") == bot.main.render.get(s, "image") for s in config.SECTIONS)
    cast(Any, bot)._preview_schedule = _scheduled(data.get("schedule")) if same else None
    previewcfg.apply(bot, cfg)
    bot.window = data["window"]
    bot._snap = _snapshot(data["snap"])
    bot._loads.extend(tuple(x) for x in data["loads"])
    bot.history.samples.extend(array("d", r) for r in data["history"])
    bot.app_history._restore(data["apps"], bot.window, data["now"])
    node = B.Node(**data["helper_node"]) if data["helper_node"] else None

    async def tasks() -> dict | None:
        bot.helper_node = node
        return data["tasks"]

    async def shutdown() -> dict:
        return data["ups_shutdown"]

    bot._read_tasks = tasks  # type: ignore[method-assign]
    bot._read_ups_shutdown = shutdown  # type: ignore[method-assign]
    bot.nodes = None if data["nodes"] is None else [tuple(n) for n in data["nodes"]]
    bot._ups = Reading(**{**data["ups"], "status": tuple(data["ups"]["status"])}) if data["ups"] else None
    bot.docker = object() if data["docker"] else None  # type: ignore[assignment]
    bot.engine = Engine(*data["engine"]) if data["engine"] else None
    bot.containers = [Container(**{**c, "networks": tuple(c["networks"]), "ports": tuple(c["ports"])})
                      for c in data["containers"]]
    bot.svc_results = [Result(*r) for r in data["results"]]
    bot.exposed = None if data["exposed"] is None else [tuple(x) for x in data["exposed"]]
    return bot


def timing(bot: B.Bot) -> dict:
    """The main output's update cadence; image uploads share one spacing across every output that has images."""
    return {"edit_every": bot.main.edit_every, "image_every": bot.main.image_every,
            "image_spacing": bot._image_spacing, "image_outputs": sum(map(bot._wants_images, bot.boards))}


def steady(bot: B.Bot, sections: list[panel.Section]) -> None:
    """Every message as already posted, so footers and schedules show the bot's running cadence, not a first post."""
    for _, mode in MODES:
        bot.main.mode = mode
        board_delivery.layout(bot, bot.main, sections, B.EMBED_LIMIT)
        for m in [bot.main, *bot.main.messages]:
            if m.message_id is None:
                m.message_id = m.position + 1
                m.image_slot = bot._slot(m.image_every)


def due_at(bot: B.Bot, message: Message, name: str | None, now: float) -> float:
    """The time this embed's footer shows: delivery.stamp's own choice of schedule."""
    if delivery.keys(bot, message):
        plan = delivery.message_schedule(bot, now, message)
        return plan.get(delivery.identity(message), [delivery.edit_boundary(bot, message, now)])[0]
    return delivery.edit_boundary(bot, message, now)


def plan(bot: B.Bot, now: float) -> list[dict]:
    """Per message of the current layout: each embed's key, kind, and title, the next edit, and the next two
    uploads its footer names."""
    return [{"embeds": [{"key": k, "kind": m.modes.get(k, "embed"),
                         "title": (e.title if isinstance(e, discord.Embed) and e.title else k)}
                        for e, k in zip(m.planned, m.embed_keys)],
             "edit_every": m.edit_every, "image_every": m.image_every,
             "edit_at": None if delivery.keys(bot, m) else delivery.edit_boundary(bot, m, now),
             "images": [t for t in delivery.message_schedule(bot, now, m).get(delivery.identity(m), [])
                        if math.isfinite(t)]}
            for m in bot.main.messages]


def _held(bot: B.Bot, message: Message, embed: discord.Embed, at: float) -> dict:
    """What a text edit shows for this image before its first upload: delivery.outgoing's own placeholder."""
    pending = view.footer(at, bot.tz, bot.update_available)
    return dict(delivery.outgoing(message, [embed], False, pending)[0].to_dict())


def _timing(bot: B.Bot, message: Message, key: str, name: str | None, now: float,
            mode: str | None, observed: dict[str, dict], at: float) -> dict:
    kind = message.modes.get(key, "embed")
    images = bool(delivery.keys(bot, message))
    nominal = message.image_every if images else message.edit_every
    gap: float | None = None
    if images:
        dates = delivery.message_schedule(bot, now, message).get(delivery.identity(message), [])
        if len(dates) > 1 and all(math.isfinite(t) for t in dates[:2]) and dates[1] > dates[0]:
            gap = dates[1] - dates[0]
    schedule = getattr(bot, "_preview_schedule", None)
    if mode is None and schedule is not None:
        actual = schedule.get(f"{key}:{kind}", {})
        at, nominal, gap = actual.get("next"), actual.get("nominal", nominal), actual.get("image_gap")
    old = observed.get(f"{key}:{kind}", {}) if mode is None else {}
    if old.get("last_success") is not None and at is not None and math.isfinite(at):
        gap = max(0.0, at - old["last_success"])
    return {"last_success": old.get("last_success"), "last_failed": old.get("last_failed", False),
            "reason": old.get("reason", ""), "nominal": nominal, "planned_gap": gap, "sent": mode is None,
            "kind": kind, "next": at if at is not None and math.isfinite(at) else None}


async def frame(bot: B.Bot, sections: list[panel.Section], images: bool = True,
                plans: dict[str, list] | None = None) -> dict:
    """Each mode as the Discord messages the bot would post: lists of {embed, name, png, at}. "default" uses the
    output's own render map. Without images, image items carry only their name, as an edit keeps the last upload;
    held is the embed an edit shows when nothing was uploaded yet.
    at is the time the embed's footer names: the next edit, or for an image the next upload."""
    out: dict[str, list] = {}
    files: dict[str, bytes] = {}
    observed = _delivery(getattr(bot, "_preview_delivery", {}))
    for key, mode in MODES:
        bot.main.mode = mode
        board_delivery.layout(bot, bot.main, sections, B.EMBED_LIMIT)
        now = bot.main.render_at
        assert now is not None  # layout sets it
        if plans is not None:
            plans[key] = plan(bot, now)
        messages = [[(m, e, section, delivery.image_name(e), due_at(bot, m, delivery.image_name(e), now))
                     for e, section in zip(m.planned, m.embed_keys)]
                    for m in bot.main.messages]
        for m, msg in zip(bot.main.messages, messages):
            if images and any(name not in files for _, _, _, name, _ in msg if name):
                files.update({str(f.filename): f.fp.read() for f in await bot.image_files(m)})
        out[key] = [[{"embed": delivery.stamp(bot, m, e.copy(), now).to_dict(), "key": section,
                      "timing": _timing(bot, m, section, name, now, mode, observed, at),
                      **({"at": at} if math.isfinite(at) else {}),
                      **({"name": name, "held": _held(bot, m, e, at),
                          **({"png": files[name]} if name in files else {})} if name else {})}
                     for m, e, section, name, at in msg] for msg in messages]
    return out


def _fast_png(save: Callable) -> Callable:
    def fast(im: Image.Image, fp: object, format: str | None = None, **params: object) -> None:
        params.pop("optimize", None)
        save(im, fp, format, compress_level=0, **params)
    return fast


async def render(snapshot: str, toml: str, images: bool = True) -> dict:
    """Browser entry: diagnostics for the edited TOML, and the frame when it has no errors. The PNGs are
    pixel-identical to the bot's, only less compressed, since zlib is slow under WebAssembly."""
    diagnostics, cfg = previewcfg.check(toml, previewcfg.BASE)
    if cfg is None or not snapshot:
        return {"diagnostics": diagnostics, "frame": None}
    data = formats.load(json.loads(snapshot), "preview snapshot", FORMAT)
    with tempfile.TemporaryDirectory() as tmp, at(Clock(data["now"]), data["cores"]):
        bot = load(data, cfg, tmp)
        try:
            with mock.patch.object(unraid, "read", lambda _path: _array(data["array"])), \
                    mock.patch.object(Image.Image, "save", _fast_png(Image.Image.save)):
                offset = datetime.fromtimestamp(data["now"], bot.tz).utcoffset()
                sections = await bot.collect()
                steady(bot, sections)
                plans: dict[str, list] = {}
                shown = await frame(bot, sections, images, plans)
                when = {**data.get("timing", {}), "utc_offset": offset.total_seconds() if offset else 0,
                        "learned": data.get("budget") is not None, "messages": plans}
                return {"diagnostics": diagnostics, "frame": shown, "timing": when}
        finally:
            bot.history.close()
            bot.app_history.close()
