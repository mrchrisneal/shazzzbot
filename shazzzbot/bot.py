"""Discord client: one self-updating status message plus permissioned slash commands."""
import asyncio
import collections
import io
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import tasks

from . import (
    SOURCE_URL,
    __version__,
    apphistory,
    board_delivery,
    boards,
    chartgen,
    charts,
    config,
    delivery,
    discordauth,
    discordlog,
    forges,
    health,
    helperclient,
    logsetup,
    netdata,
    panel,
    panelwork,
    perms,
    prep,
    ratecmd,
    relay,
    releases,
    renderbudget,
    restarts,
    services,
    sysimage,
    tasktimes,
    unraid,
    ups,
    view,
)
from .boards import Board, Message
from .docker import Container, Docker, Engine, Inventory
from .glyphs import GREEN
from .helper import formats
from .helper.constants import version_number
from .host import Host, Snapshot

log = logging.getLogger("shazzzbot")
ENGINE_EVERY = 300.0
SERVICES_EVERY = 60.0
RELEASE_EVERY = 6 * 3600.0
RELEASE_RETRY = 300.0
UPLOAD_WAIT = 20.0  # py-cord retries a rate-limited send for minutes; give up and try next cycle
TICK = delivery.TICK
IMAGE_WAIT = 150.0  # an upload 429 asks for 20-140s; cutting the retry short wastes the attempt
FIRST_CHECK_WAIT = 60.0  # above three 8s URL stages plus relay calls, so a slow first check still lands
EMBED_LIMIT = 6000  # Discord's cap on all embed text in one message
NOTE_ROOM = 300  # room left for the note that names the sections a reply leaves out


@dataclass
class Node:
    key: str
    hostname: str
    version: str | None
    level: int = panel.FINE
    ms: float | None = None
    bundle_version: str | None = None
    entry_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.key == "helper" and any(value is not None for value in
                (self.version, self.bundle_version, self.entry_sha256)) and any(value is None for value in
                (self.version, self.bundle_version, self.entry_sha256)):
            raise ValueError("helper identity is incomplete")

    def update_needed(self, latest: str) -> bool | None:
        values = (self.version, self.bundle_version) if self.key == "helper" else (self.version,)
        try:
            target = version_number(latest)
            versions = [version_number(value) if value is not None else None for value in values]
        except ValueError:
            return None
        if any(version is not None and version < target for version in versions):
            return True
        return None if None in versions else False

    def current_text(self) -> str:
        current = (discord.utils.escape_markdown(f"v{self.version[:64]}") if self.version
                   else "unavailable (not connected or not reported)")
        text = f"Current version: {current}\n"
        if self.key == "helper" and self.bundle_version is not None and self.bundle_version != self.version:
            bundle = discord.utils.escape_markdown(f"v{self.bundle_version[:64]}")
            text += f"Installed bundle version: {bundle}\n"
        return text


def newer(tag: str, current: str) -> bool:
    """True when release tag `tag` (v1.2.3) is a higher version than `current`."""
    try:
        return version_number(tag) > version_number(current)
    except ValueError:
        return False

class Bot(discord.Bot):
    def __init__(self, cfg: config.Config):
        super().__init__(intents=discord.Intents(guilds=True), debug_guilds=[cfg.guild_id])
        self.rate_budget = discordlog.install(self.http, self._save_state, lambda cid: discordauth.notify(self, cid))
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        if cfg.green_ok:
            panel.use_ok_glyph(GREEN)
        self.host = Host(cfg.host_root)
        self.http_session: aiohttp.ClientSession | None = None
        self.docker: Docker | None = None
        self.containers: list[Container] = []
        self._channels_checked = False
        self._inventory = Inventory()
        self.engine: Engine | None = None
        self._engine_at = -ENGINE_EVERY
        self._release_at = -RELEASE_EVERY
        self._release_wait = RELEASE_EVERY
        self._release_checked: float | None = None
        self._release_error: str | None = None
        self.update_available = False
        self.latest_release: str | None = None
        self.svc_results: list[services.Result] = []
        self.exposed: list[tuple[str, int]] | None = None
        self.nodes: list[tuple[int, float | None, str]] | None = None if cfg.relays else []  # None: not pinged yet
        self.node_versions: dict[str, str | None] = {}
        self.helper_node: Node | None = None
        self._helper_error: str | None = None
        self._svc_at = -SERVICES_EVERY
        self._svc_task: asyncio.Task | None = None
        self.array: unraid.Array | None = None
        state = self._load_state()
        self.rate_budget.restore(state["rate_budget"])
        self._task_times: dict[str, tasktimes.Record] = state["task_times"]
        self.boards = [Board(o.channel, render={**cfg.render, **o.render}, sections=o.sections,
                             image_every=o.image_every if o.image_every is not None else cfg.image_every,
                             edit_every=o.edit_every if o.edit_every is not None else cfg.edit_every)
                       for o in cfg.outputs]
        self.main = self.boards[0]
        for n, b in enumerate(self.boards):
            b.phase = n * min(board.edit_every for board in self.boards) / len(self.boards)
            ids = state["messages"].get(b.channel_id, [])
            for i, mid in enumerate(ids):
                message = b if i == 0 else boards.child(b, i)
                if i:
                    b.messages.append(message)
                message.message_id = mid
                message.phase = b.phase + i * min(1.0, b.edit_every / len(ids))
                message.image_slot = self._slot(message.image_every)
                message.edit_slot = delivery.edit_slot(self, message)
        self.window = min(charts.parse_span(state.get("window", "")) or charts.DEFAULT_WINDOW,
                          cfg.history_days * charts.DAY)
        # Path comes from the operator's config, not from a Discord user or the network. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        self.history = charts.History(os.path.join(os.path.dirname(cfg.state_file) or ".", "history.db"),
                                      cfg.history_days * charts.DAY)
        self._snap: Snapshot | None = None
        self._ups = ups.Reading() if cfg.ups else None
        self.forges = [forges.Monitor(settings, i) for i, settings in enumerate(cfg.forges)]
        self.sample_loop.change_interval(seconds=cfg.interval)
        # Fixed filename under the operator-configured state directory, never an app name. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        self.app_history = apphistory.AppHistory(int(cast(float, self.sample_loop.seconds)),
                                                  os.path.join(os.path.dirname(cfg.state_file) or ".", "apps.json"),
                                                  self.window)
        self._primed = False
        self._upload_at = -math.inf
        self._upload_wall = state["upload_at"]  # kept across restarts, so a restart cannot spend the budget again
        if self._upload_wall is not None:
            self._upload_at = time.monotonic() - (time.time() - self._upload_wall)
        self._image_spacing = state["image_spacing"]
        self._image_retry = state["image_retry"]
        self._tick: int | None = None
        self._delivery_started = False
        self._system: list[sysimage.Row] = []
        self._status: tuple[int, str] | None = None
        self._sections: list[panel.Section] = []
        self._loads: collections.deque[tuple[float, float, float]] = collections.deque(
            maxlen=max(2, 60 // cfg.interval + 1))
        self._lock = asyncio.Lock()
        self._media_lock = asyncio.Lock()
        self._collect_lock = asyncio.Lock()
        self._snapshot: panelwork.RenderSnapshot | None = None
        self.lead = prep.Lead()
        self._drawn: dict[tuple[int, int], tuple[tuple[str, ...], list[tuple[str, bytes]]]] = {}

    @property
    def _image_spacing(self) -> float:
        return max(self._base_image_spacing, self.rate_budget.media_spacing(delivery.MAX_IMAGE_SPACING))

    @_image_spacing.setter
    def _image_spacing(self, value: float) -> None:
        self._base_image_spacing = value

    def _slot(self, every: int) -> int:
        """Local wall-clock slot number; it moves back an hour when DST ends, which still reads as a new slot."""
        now = time.time()
        offset = datetime.fromtimestamp(now, self.tz).utcoffset()
        return int((now + (offset.total_seconds() if offset else 0)) // every)

    # Path comes from the operator's config, not from a Discord user or the network. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    def _load_state(self) -> dict:
        try:
            with open(self.cfg.state_file) as f:
                state = formats.load(json.load(f), "bot state", 1)
        except OSError:
            state = {}
        except formats.FormatError:
            raise
        except (TypeError, ValueError):
            log.warning("state file is invalid; Discord rate budget starts fresh")
            state = {}
        got = state.get("messages")
        messages = {int(k): ids for k, v in got.items() if k.isdigit() and (ids := boards.restore_ids(v))} \
            if isinstance(got, dict) else {}
        def number(name: str, default: float) -> float:
            value = state.get(name)
            return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and 0 <= value <= 10**12 else default)
        upload = number("upload_at", -math.inf)
        retry = number("image_retry", 0.0)
        if upload > time.time() + config.MAX_IMAGE_SPACING:
            upload = -math.inf
        if retry > time.time() + 86400:
            retry = 0.0
        return {"messages": messages, "window": state.get("window") if isinstance(state.get("window"), str) else "",
                "rate_budget": state.get("rate_budget"),
                "upload_at": upload if math.isfinite(upload) else None,
                "image_spacing": delivery.bounded_spacing(number("image_spacing", config.IMAGE_FLOOR)),
                "image_retry": retry,
                "task_times": tasktimes.restore(state.get("task_times"))}

    # Path comes from the operator's config, not from a Discord user or the network. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
        tmp = self.cfg.state_file + ".tmp"
        spacing = self._image_spacing
        with open(tmp, "w") as f:
            json.dump(formats.dump({"messages": boards.message_state(self.boards),
                       "window": charts.span(self.window), "upload_at": self._upload_wall,
                       "task_times": self._task_times,
                       **({"rate_budget": budget} if (budget := self.rate_budget.dump()) is not None else {}),
                       **({"image_spacing": spacing, "image_retry": self._image_retry}
                          if spacing > config.IMAGE_FLOOR or self._image_retry else {})}, 1), f)
        os.replace(tmp, self.cfg.state_file)

    def served_channels(self) -> set[int]:
        return {b.channel_id for b in self.boards} | {c for c in (self.cfg.commands_channel_id,) if c}

    def channels_text(self) -> str:
        return ", ".join(f"<#{c}>" for c in sorted(self.served_channels())[:20])

    async def process_application_commands(
            self, interaction: discord.Interaction, auto_sync: bool | None = None) -> None:
        """Route commands and autocomplete only to this bot's output channels and optional commands channel."""
        if interaction.guild_id != self.cfg.guild_id:
            return
        if interaction.channel_id in self.served_channels():
            await super().process_application_commands(interaction, auto_sync)
            return
        kind = getattr(interaction, "type", None)
        try:
            if kind == discord.InteractionType.auto_complete:
                await interaction.response.send_autocomplete_result(choices=[])
            elif kind == discord.InteractionType.application_command:
                await interaction.response.send_message(
                    f"Use this bot's commands in {self.channels_text()}.", ephemeral=True)
        except (discord.HTTPException, discordauth.Blocked) as e:
            log.warning("could not answer a command outside the bot's channels: %s", e)

    async def check_command_channels(self) -> None:
        """Warn when the app-level command setting lets members run commands where the bot never answers.
        Discord administrators and per-command overrides bypass this setting."""
        app_id = self.application_id
        if not app_id:
            return
        try:
            entries = await asyncio.wait_for(self.http.get_guild_application_command_permissions(
                app_id, self.cfg.guild_id), 10)
        except discord.NotFound:
            entries = []
        except (discord.HTTPException, discordauth.Blocked, TimeoutError, aiohttp.ClientError, OSError) as e:
            log.warning("could not read command permissions: %s", e)
            return
        app = next((e for e in entries if int(e["id"]) == app_id), None)
        rules = {int(r["id"]): r["permission"] for r in (app["permissions"] if app else []) if r["type"] == 3}
        allowed = {c for c, ok in rules.items() if ok}
        if rules.get(self.cfg.guild_id - 1) is False and allowed and allowed <= self.served_channels():
            return
        text = ("This bot's commands can be used in channels where it never answers. In Server Settings > "
                "Integrations > this app > Channels, deny All Channels and allow only "
                f"{self.channels_text()}.")
        log.warning("%s", text)
        ch = self.audit_channel()
        if ch is None:
            return
        try:
            await asyncio.wait_for(ch.send(text, allowed_mentions=discord.AllowedMentions.none()), 10)
        except (discord.HTTPException, discordauth.Blocked, TimeoutError, aiohttp.ClientError, OSError) as e:
            log.warning("could not post the command channel warning: %s", e)

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        """Sampling starts before login, so the history keeps filling while Discord is down or rate limited."""
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        if self.cfg.docker_proxy:
            self.docker = Docker(self.cfg.docker_proxy, self.http_session)
        self.sample_loop.start()
        from . import preview  # the preview imports this module
        await preview.start(self)
        await super().start(token, reconnect=reconnect)

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)
        for g in self.guilds:
            if g.id != self.cfg.guild_id:
                log.warning("leaving unexpected guild %s", g.id)
                await g.leave()
        if not self.panel_loop.is_running():
            self.panel_loop.start()
        if not self._channels_checked:
            self._channels_checked = True
            await self.check_command_channels()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.cfg.guild_id:
            await guild.leave()

    async def on_application_command_error(self, ctx: discord.ApplicationContext,
                                           exception: discord.DiscordException) -> None:
        log.error("command %s failed", ctx.command, exc_info=exception)
        try:
            await ctx.respond(FAILED, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self) -> None:
        self.sample_loop.cancel()
        self.panel_loop.cancel()
        try:
            self._save_state()
        except OSError:
            log.warning("could not persist the Discord rate budget on shutdown")
        for monitor in self.forges:
            await monitor.close()
        if self._svc_task:
            self._svc_task.cancel()
        if self.http_session:
            await self.http_session.close()
        await asyncio.to_thread(self.app_history.close)
        self.history.close()
        await super().close()

    async def sample(self) -> None:
        """Poll the host and record it. The first poll only sets the baseline that rates are measured from."""
        await self._poll_docker()
        if self.cfg.ups:
            self._ups = await ups.read(self.cfg.ups)
        snap = await asyncio.to_thread(self.host.poll, {c.id: c.name for c in self.containers})
        if not self._primed:
            self._primed = True
            return
        self._snap = snap
        self._loads.append(snap.load)
        row = self.history.add(snap)
        self.app_history.add(snap, self.window, row[0])
        await asyncio.to_thread(self.history.store, row)
        await asyncio.to_thread(self.app_history.store)

    @tasks.loop(seconds=10)
    async def sample_loop(self) -> None:
        try:
            await self.sample()
            health.beat(self.cfg.interval)
        except Exception:
            log.exception("sampling failed")

    async def _poll_docker(self) -> None:
        if not self.docker:
            return
        try:
            self.containers = await self.docker.list()
            self._inventory.success()
        except (TimeoutError, aiohttp.ClientError) as e:
            self._inventory.failed = True
            log.warning("docker list failed: %s", e)
        if time.monotonic() - self._engine_at > ENGINE_EVERY:
            self._engine_at = time.monotonic()
            try:
                self.engine = await self.docker.info()
            except (TimeoutError, aiohttp.ClientError, ValueError) as e:
                log.warning("docker info failed: %s", e)

    async def _issues(self, snap: Snapshot, arr: unraid.Array | None, smart: frozenset[str]) -> list[panel.Row]:
        """One feed: the local checks, Netdata's alarms, and the service alerts, worst first."""
        issues = panel.alerts(snap, arr, self.containers, self.cfg.thresholds, smart)
        if self.http_session:
            await forges.poll(self.forges, self.http_session, FIRST_CHECK_WAIT)
        if self.cfg.netdata_url and self.http_session:
            try:
                issues += await netdata.alarms(self.http_session, self.cfg.netdata_url, self.cfg.netdata_ignore)
            except (TimeoutError, aiohttp.ClientError, ValueError) as e:
                log.warning("netdata alarms failed: %s", e)
                issues.append((panel.WARNING, "Netdata unreachable"))
        if ((self.cfg.services or self.cfg.public_host or self.cfg.relays) and self.http_session
                and time.monotonic() - self._svc_at >= SERVICES_EVERY
                and (self._svc_task is None or self._svc_task.done())):
            self._svc_at = time.monotonic()
            self._svc_task = asyncio.create_task(self._check_services(self.http_session))
            if not self.svc_results and self.exposed is None:  # first run: the first image upload must include it
                await asyncio.wait({self._svc_task}, timeout=FIRST_CHECK_WAIT)
        return sorted(issues + services.alerts(self.cfg.services, self.svc_results), key=lambda r: -r[0])

    @property
    def embed_limit(self) -> int:
        return EMBED_LIMIT

    def _fit(self, board: Board, sections: list[panel.Section], images: bool = True) -> list[discord.Embed]:
        return board_delivery.render(self, board, sections, not images or not board.channel_id, EMBED_LIMIT, NOTE_ROOM)

    async def build(self) -> list[discord.Embed]:
        """The main panel's embeds from fresh sections."""
        sections = await self.collect()
        async with self._lock:
            return self._fit(self.main, sections)

    async def _read_tasks(self) -> dict | None:
        if self.cfg.helper is None:
            self.helper_node = None
            self._helper_error = None
            return None
        hostname = self.array.name if self.array and self.array.name else "unknown"
        started, version, ms = time.monotonic(), None, None
        bundle_version, entry_sha256 = None, None
        try:
            identity = await helperclient.request(self.cfg.helper, "hello")
            version, ms = identity["version"], (time.monotonic() - started) * 1000
            bundle_version, entry_sha256 = identity["bundle_version"], identity["entry_sha256"]
            data = await helperclient.request(self.cfg.helper, "tasks")
        except helperclient.Error as error:
            message = (str(error) if isinstance(error, helperclient.ProtocolMismatch)
                       else "helper is unavailable or its tasks could not be read")
            if message != self._helper_error:
                log.warning("%s", message)
            self._helper_error = message
            self.helper_node = Node("helper", hostname, version, panel.WARNING, ms, bundle_version, entry_sha256)
            return None
        self._helper_error = None
        self.helper_node = Node("helper", hostname, version, panel.FINE, ms, bundle_version, entry_sha256)
        return data

    async def _read_ups_shutdown(self) -> dict:
        if self.cfg.helper is None or not self.cfg.ups:
            return {}
        try:
            return await helperclient.request(self.cfg.helper, "ups_shutdown")
        except helperclient.Error:
            return {}

    async def collect(self, at: float | None = None) -> list[panel.Section]:
        return await panelwork.collect(self, at)

    async def _check_services(self, http: aiohttp.ClientSession) -> None:
        try:
            started = self._svc_at if self._svc_task is asyncio.current_task() else time.monotonic()
            rs = self.cfg.relays
            await self._check_nodes(http)
            async def remote(target: str, expect: str) -> tuple[bool, str, float] | None:
                if not target.startswith("tcp://"):
                    return await relay.probe_any(http, rs, target, expect)
                answers = await asyncio.gather(*(relay.ask(http, r, [{"tcp": target[6:]}]) for r in rs))
                got = [answer[0] for answer in answers if answer]
                return next((g for g in got if g[0]), got[0] if got else None)
            cache: services.Cache = getattr(self, "_svc_cache", {})
            self._svc_cache = cache
            self.svc_results = await services.check_all(
                self.cfg.services, self.containers, http, remote if rs else None, cache=cache, every=SERVICES_EVERY,
                now=started)
            if rs and self.cfg.public_host:
                by_port = {p: c.name for c in self.containers for p in c.ports}
                got = await relay.open_ports_any(http, rs, self.cfg.public_host, list(by_port))
                self.exposed = None if got is None else [(by_port[p], p) for p in got]
        except Exception:
            log.exception("service checks failed")

    async def _check_nodes(self, http: aiohttp.ClientSession) -> None:
        rs = self.cfg.relays
        pinged = await asyncio.gather(*(relay.ping(http, r) for r in rs))
        for r, (was, _, _), (lv, _, why, _) in zip(
                rs, self.nodes or [(panel.FINE, None, "")] * len(rs), pinged):
            if lv != was:
                log.log(logging.INFO if lv == panel.FINE else logging.ERROR, "relay %s: %s",
                        urlsplit(r.url).netloc, why or "ok")
        self.nodes = [(lv, ms, why) for lv, ms, why, _ in pinged]
        self.node_versions = {r.url: version for r, (_, _, _, version) in zip(rs, pinged)}

    def node_inventory(self) -> list[Node]:
        hostname = self.array.name if self.array and self.array.name else "unknown"
        nodes = [Node("bot", hostname, __version__)]
        nodes.extend(Node(r.url, urlsplit(r.url).hostname or "unknown", self.node_versions.get(r.url))
                     for r in self.cfg.relays)
        nodes.append(self.helper_node or Node("helper", hostname, None))
        return nodes

    def update_report(self, node: Node) -> str:
        setting = self.cfg.node_updates.get(node.key, {})
        hostname = setting.get("hostname", node.hostname).replace("`", "'")[:253]
        kind = node.key if node.key in {"bot", "helper"} else "relay"
        label = f"**NODE:** {kind} on `{hostname}`"
        text = f"{label}\n{node.current_text()}"
        latest = self.latest_release
        if latest is None:
            text += "Latest public release: unavailable."
        else:
            label = "Latest known public release" if self._release_error else "Latest public release"
            text += f"{label}: {discord.utils.escape_markdown(latest[:64])}\n"
            needed = node.update_needed(latest)
            text += ("Version comparison unavailable." if needed is None else
                     "Update available." if needed else
                     "No newer release in last-known result." if self._release_error else "No update available.")
        if self._release_checked is not None:
            stamp = datetime.fromtimestamp(self._release_checked, self.tz).strftime("%Y-%m-%d %I:%M %p %Z")
            text += f"\nRelease check: {stamp} - {self._release_error or 'ok'}."
        command = setting.get("command")
        if command:
            run_on = setting.get("run_on", hostname)
            runtime_only = node.key == "helper" and any(word.split("=", 1)[0] == "--update"
                                                        for word in command.split())
            text += f"\nRun on `{run_on}`{' (runtime only)' if runtime_only else ''}:\n```\n{command}\n```"
        else:
            text += "\nUpdate command not configured (node_updates)."
        if node.key == "helper":
            if latest is not None and node.bundle_version is not None and newer(latest, node.bundle_version):
                text += "\nFull helper bundle update required."
            text += "\nFull helper bundle: rerun the installer with --upgrade --helper on this host."
        return text

    @tasks.loop(seconds=2)  # cheap when the slot has not changed; keeps edits within 2s of the boundary
    async def panel_loop(self) -> None:
        if self._snap is None:
            return
        try:
            await self._run_due()
            if not self._pending() and (prepared := await prep.ahead(self)) is not None:
                await self._run_due(prepared)
        except Exception:
            log.exception("panel update failed")

    def _pending(self) -> bool:
        return any(delivery.edit_due(self, message) and time.time() >= board.retry_at
                   for board in self.boards for message in board.messages)

    async def _run_due(self, prepared: prep.Prepared | None = None) -> None:
        full = self._pending()
        tick = self._slot(TICK)
        if not full and tick == self._tick:
            return
        self._tick = tick
        if full:
            await self.refresh(scheduled=True, prepared=prepared)
        else:
            if not self._sections:
                sections = await self.collect()
                for board in self.boards:
                    self._fit(board, sections)
            if any(self._wants_images(b) and self._due(b, False) for b in self.boards):
                await self.refresh(images_only=True, prepared=prepared)

    def _boundary(self, t: float, every: int) -> float:
        """The next local wall-clock multiple of every after t."""
        offset = datetime.fromtimestamp(t, self.tz).utcoffset()
        off = offset.total_seconds() if offset else 0
        return ((t + off) // every + 1) * every - off

    def schedule(self, now: float, uploaded: Message | None = None) -> dict[int, list[float]]:
        """Projected upload times, two per channel with images, from the real budget: the last upload, IMAGE_FLOOR,
        TICK alignment, each channel's slot, and the longest-waiting-first order refresh() uses."""
        return delivery.schedule(self, now, uploaded)

    def image_keys(self, board: Message | None = None) -> tuple[str, ...]:
        board = board or self.main
        sections = board.snapshot.sections if board.snapshot else self._sections
        return tuple(s.key for s in sections if board.modes.get(s.key) == "image")

    def _wants_images(self, board: Message) -> bool:
        selected = [key for key in (config.SECTIONS if board.sections is None else board.sections)
                    if key != "forges" or self.forges]
        return bool(self.image_keys(board)) or any(
            (board.mode or board.render.get(key, "image")) == "image" for key in selected)

    async def _check_release(self, repo: str) -> None:
        """Cache the latest public release for a few hours; network failures keep the last known result."""
        if time.monotonic() - self._release_at < self._release_wait or not self.http_session:
            return
        self._release_at = time.monotonic()
        result = await releases.fetch(self.http_session, repo)
        self._release_at = time.monotonic()
        self._release_checked, self._release_error = time.time(), result.error
        self._release_wait = RELEASE_EVERY if result.error is None else max(RELEASE_RETRY, result.retry_after)
        if result.error:
            log.debug("release check failed: %s", result.error)
        else:
            self.latest_release = result.tag
            self.update_available = result.tag is not None and newer(result.tag, __version__)

    async def image_files(self, board: Message | None = None) -> list[discord.File]:
        board = board or self.main
        ready = self._drawn.pop(delivery.identity(board), None)
        if ready is None or ready[0] != delivery.keys(self, board):
            ready = (), await self.draw_images(board, board.render_at if board.render_at is not None else time.time())
        return [discord.File(io.BytesIO(png), name) for name, png in ready[1]]

    async def draw_images(self, board: Message, now: float) -> list[tuple[str, bytes]]:
        async with self._lock:
            snapshot = board.snapshot or self._snapshot
            disks = list(snapshot.disks) if snapshot else [
                (panel.drive(d.name), d.used_pct, d.temp, *self.cfg.thresholds["disk_full"])
                for d in (self.array.disks if self.array else []) if d.used_pct is not None]
            samples = list(snapshot.samples) if snapshot else list(self.history.samples)
            sections = snapshot.sections if snapshot else tuple(self._sections)
            system = list(snapshot.system) if snapshot else list(self._system)
            status = snapshot.status if snapshot else self._status
            window, tz = (snapshot.window, snapshot.tz) if snapshot else (self.window, self.tz)
            gap = snapshot.gap if snapshot else 3 * self.cfg.interval
            update = snapshot.update_available if snapshot else self.update_available
            wanted = set(delivery.keys(self, board))
            plan = delivery.message_schedule(self, now, board).get(delivery.identity(board)) or [now, now]
            note = view.footer(plan[0] if board.channel_id else None, tz, update).replace("\n", " | ")

            @renderbudget.serialized
            def draw() -> list[tuple[str, bytes]]:
                out = []
                for s in (s for s in sections if s.key in wanted):
                    if s.key == "system":
                        bars = charts.disk_bars(disks, now, tz)
                        parts = [sysimage.render(system, now, tz, status), *([bars] if bars else [])]
                    elif s.key == "history":
                        parts = [charts.render(samples, window, now, tz, gap=gap)]
                    else:
                        parts = [view.image(s, now=now, tz=tz)]
                    out.append((view.filename(s), parts))
                return [(name, view.stack(parts, note, icon="github")) for name, parts in out]
            return await asyncio.to_thread(draw)

    async def refresh(self, force_images: bool = False, images_only: bool = False, scheduled: bool = False,
                      prepared: prep.Prepared | None = None) -> None:
        """Refresh outputs, or only due edits/images when scheduled; a failed output does not stop the others.
        prepared holds sections and images drawn ahead for this boundary, so the send does not wait on them."""
        sections = prepared.sections if prepared else await self.collect()
        snapshot = prepared.snapshot if prepared else self._snapshot
        held = {}
        waiting = []
        tasks = []
        def configure(board: Board) -> None:
            board.snapshot = snapshot
            board.modes = {s.key: board.mode or board.render.get(forges.group(s.key), "image") for s in sections
                           if board.sections is None or forges.group(s.key) in board.sections}
            channel = self.get_channel(board.channel_id)
            if isinstance(channel, discord.TextChannel) and channel.guild.id == self.cfg.guild_id:
                board_delivery.layout(self, board, sections, EMBED_LIMIT)
            for message in board.messages:
                message.snapshot = snapshot
                if force_images:
                    delivery.due(self, message, True)
            if prepared:
                self._drawn.update({key: value for key, value in prepared.files.items()
                                    if key[0] == board.channel_id})

        try:
            for board in self.boards:
                if not board.lock.locked():
                    await board.lock.acquire()
                    held[id(board)] = board
                else:
                    waiting.append(board)
            async with self._lock:
                if scheduled and not self._delivery_started:
                    self._delivery_started = True
                    for board in held.values():
                        if board.message_id is None:
                            board.retry_at = max(board.retry_at, time.time() + board.phase)
                for board in held.values():
                    configure(board)

            async def dispatch(board: Board, queued: bool) -> None:
                if queued:
                    await board.lock.acquire()
                    held[id(board)] = board
                try:
                    if queued:
                        async with self._lock:
                            configure(board)
                    await update(board)
                except Exception:
                    log.exception("panel update in channel %s failed", board.channel_id)
                finally:
                    for key in [key for key in self._drawn if key[0] == board.channel_id]:
                        self._drawn.pop(key)
                    held.pop(id(board))
                    board.lock.release()

            async def update(board: Board) -> None:
                if images_only and not (self._wants_images(board) and self._due(board, False)):
                    return
                if time.time() < board.retry_at:
                    return
                pending = any(delivery.edit_due(self, message) for message in board.messages)
                if scheduled and not pending \
                        and not (self._wants_images(board) and self._due(board, False)):
                    return
                only_images = images_only or scheduled and not pending
                board.scheduled = scheduled
                await self._refresh(board, sections, force_images, images_only=only_images)

            ready = [(board, False) for board in held.values()] + [(board, True) for board in waiting]
            tasks = [asyncio.create_task(dispatch(board, queued))
                     for board, queued in sorted(ready, key=lambda entry: entry[0].image_at)]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for board in held.values():
                board.lock.release()

    async def latest(self) -> list[discord.Embed]:
        """The main panel's last embeds, built once if the loop has not run yet. Never polls the host a second time."""
        if not self.main.embeds:
            async with self.main.lock:
                if not self.main.embeds:
                    self.main.embeds = await self.build()
        return [embed for message in self.main.messages for embed in message.embeds]

    async def status_reply(self) -> tuple[list[discord.Embed], list[discord.File]]:
        """Use the first output privately, with embeds instead of images when the upload budget is spent."""
        sections = self._sections or await self.collect()
        async with self._lock:
            board = Board(0, render=self.main.render, sections=self.main.sections,
                          image_every=self.main.image_every, edit_every=self.main.edit_every)
            board.snapshot = self._snapshot
            if self._media_lock.locked() or time.monotonic() - self._upload_at < self._image_spacing \
                    or time.time() < self._image_retry:
                board.mode = "embed"
                return self._fit(board, sections, images=False), []
            embeds = self._fit(board, sections)
        files = await self.image_files(board)
        async with self._lock:
            if files and (self._media_lock.locked() or time.monotonic() - self._upload_at < self._image_spacing
                          or time.time() < self._image_retry):
                board.mode = "embed"
                return self._fit(board, sections, images=False), []
            if files:
                self._upload_at, self._upload_wall = time.monotonic(), time.time()
                self._save_state()
            return embeds, files

    def _due(self, board: Message, force: bool) -> bool:
        """Images every image_every seconds per channel, and at most one upload per IMAGE_FLOOR for the whole bot,
        forced or not. A text-only edit keeps the images already on a message."""
        if isinstance(board, Board):
            if not self.image_keys(board) and self._wants_images(board) \
                    and board.image_slot != self._slot(board.image_every) \
                    and time.monotonic() - self._upload_at >= self._image_spacing:
                return True
            return any(delivery.due(self, message, force) for message in board.messages)
        return delivery.due(self, board, force)

    async def _refresh(self, board: Board, sections: list[panel.Section], force_images: bool,
                       images_only: bool = False) -> None:
        await board_delivery.refresh(self, board, sections, force_images, images_only)

    async def _send(self, board: Message, send, embeds: list[discord.Embed], files: list[discord.File] | None,
                    keys: tuple[str, ...], edit: bool) -> discord.Message:
        return await delivery.transmit(self, board, send, embeds, files, keys, edit,
                                       upload_wait=UPLOAD_WAIT, image_wait=IMAGE_WAIT)

    async def _sweep(self, channel: discord.TextChannel, board: Board) -> None:
        await board_delivery.cleanup(self, channel, board)

    def audit_channel(self) -> discord.TextChannel | None:
        ch = self.get_channel(self.cfg.audit_channel_id) if self.cfg.audit_channel_id else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def audit(self, user: discord.User | discord.Member, text: str) -> bool:
        """Log and post to the audit channel. False when the post did not land, so callers can fail closed."""
        log.info("audit: %s (%s) %s", user, user.id, text)
        ch = self.audit_channel()
        if ch is None or self.rate_budget.auth.blocked(ch.id):
            return False
        try:
            await ch.send(f"<@{user.id}> {text}", allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, discordauth.Blocked) as e:
            log.error("audit post failed: %s", e)
            return False
        return True


def can(bot: Bot, ctx: discord.ApplicationContext, tier: str) -> bool:
    roles = {r.id for r in getattr(ctx.author, "roles", [])}
    return ctx.guild_id == bot.cfg.guild_id and perms.allowed(bot.cfg.perms, tier, ctx.author.id, roles)


async def denied(bot: Bot, ctx: discord.ApplicationContext, tier: str) -> bool:
    """True, after telling the user, when they may not use this tier."""
    if can(bot, ctx, tier):
        return False
    await ctx.respond("Not allowed.", ephemeral=True)
    return True


class Confirm(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.ok: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This prompt is not yours.", ephemeral=True)
        return False

    async def on_error(self, error: Exception, item: discord.ui.Item, interaction: discord.Interaction) -> None:
        log.error("button %s failed", getattr(item, "label", item), exc_info=error)
        self.stop()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(FAILED, ephemeral=True)
            else:
                await interaction.response.send_message(FAILED, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, _: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Working...", view=None)
        self.ok = True  # only once the edit landed, so a failed press never counts as consent
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, _: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.ok = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


async def confirm(ctx: discord.ApplicationContext, prompt: str) -> bool:
    view = Confirm(ctx.author.id)
    await ctx.respond(prompt, view=view, ephemeral=True)
    if await view.wait():
        await ctx.edit(content="Timed out. Nothing changed.", view=None)
    return bool(view.ok)


def images_note(bot: Bot) -> str:
    plan = bot.schedule(time.time()).get(bot.main.channel_id)
    if plan and any(message.image_wanted for message in bot.main.messages):
        return (f" Images follow at {view.clock(plan[0], bot.tz)}." if math.isfinite(plan[0])
                else " Images wait for the rate budget.")
    return ""


NO_AUDIT = "The audit channel is not set or not reachable, so changes are turned off."
FAILED = "That did not work. The details are in the bot log."


def register(bot: Bot) -> None:
    blowup_until = -math.inf

    async def container_names(actx: discord.AutocompleteContext) -> list[str]:
        q = str(actx.value or "").lower()
        names = [c.name for c in bot.containers if q in c.name.lower()]
        return [n for n in names if n not in restarts.blocked(bot, [n])][:25]

    panel_cmd = bot.create_group("panel", "Status panel")

    @panel_cmd.command(description="Refresh the status panel now")
    async def refresh(ctx: discord.ApplicationContext) -> None:
        if await denied(bot, ctx, "operate"):
            return
        await ctx.defer(ephemeral=True)
        await bot.refresh(force_images=True)
        await ctx.respond("Refreshed." + images_note(bot), ephemeral=True)

    @bot.slash_command(description="Show the primary output privately")
    async def status(ctx: discord.ApplicationContext) -> None:
        if await denied(bot, ctx, "view"):
            return
        await ctx.defer(ephemeral=True)
        embeds, files = await bot.status_reply()
        await ctx.respond(embeds=embeds, files=files, ephemeral=True)
        if files:
            bot._image_spacing = delivery.recovered_spacing(bot._image_spacing)
            bot._save_state()

    @bot.slash_command(description="Show the version, license, and source code link")
    async def about(ctx: discord.ApplicationContext) -> None:  # open to all: static, and AGPL wants the source offered
        await ctx.respond(f"Shazzzbot v{__version__}, licensed AGPL-3.0-or-later. Source: <{SOURCE_URL}>",
                          ephemeral=True)

    @bot.slash_command(description="Show node versions and the terminal commands to update them")
    async def update(ctx: discord.ApplicationContext) -> None:
        if await denied(bot, ctx, "admin"):
            return
        await ctx.defer(ephemeral=True)
        if not await bot.audit(ctx.author, "requested bot update check"):
            await ctx.respond(NO_AUDIT, ephemeral=True)
            return
        await bot._check_release(SOURCE_URL.removeprefix("https://github.com/"))
        if bot.http_session:
            await bot._check_nodes(bot.http_session)
        for node in bot.node_inventory():
            await ctx.respond(bot.update_report(node), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def spans(actx: discord.AutocompleteContext) -> list[str]:
        return charts.span_choices(str(actx.value or ""), bot.cfg.history_days * charts.DAY)

    charts_cmd = bot.create_group("charts", "History charts")

    @charts_cmd.command(description="Set the time span the charts cover (5m to 7d)")
    @discord.option("span", str, description="Like 45m or 6h", autocomplete=spans)
    async def window(ctx: discord.ApplicationContext, span: str) -> None:
        if await denied(bot, ctx, "operate"):
            return
        seconds = charts.parse_span(span)
        if seconds is None:
            await ctx.respond("Use a span from 5m to 7d, like `45m`, `6h` or `3d`.", ephemeral=True)
            return
        if bot.audit_channel() is None:
            await ctx.respond(NO_AUDIT, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        seconds = min(seconds, bot.cfg.history_days * charts.DAY)
        bot.window = seconds
        bot._save_state()
        await bot.refresh(force_images=True)
        await ctx.respond(f"Charts now cover the last {charts.span(seconds)}." + images_note(bot), ephemeral=True)
        await bot.audit(ctx.author, f"set chart window to {charts.span(seconds)}")

    @bot.slash_command(description="Ping the configured owner five times for attention")
    @discord.option("reason", str, description="Why attention is needed", required=False, default="")
    async def blowup(ctx: discord.ApplicationContext, reason: str = "") -> None:
        nonlocal blowup_until
        if await denied(bot, ctx, "operate"):
            return
        target = bot.cfg.blowup_user_id
        if not target:
            await ctx.respond("Blowup is not configured.", ephemeral=True)
            return
        if bot.audit_channel() is None:
            await ctx.respond(NO_AUDIT, ephemeral=True)
            return
        send = getattr(ctx.channel, "send", None)
        if send is None:
            await ctx.respond(FAILED, ephemeral=True)
            return
        now = time.monotonic()
        if now < blowup_until:
            await ctx.respond(f"Blowup is on cooldown for {math.ceil(blowup_until - now)} seconds.", ephemeral=True)
            return
        blowup_until = now + 600
        await ctx.defer(ephemeral=True)
        reason = reason[:200]
        if not await bot.audit(ctx.author, f"requested blowup: {reason}"):
            await ctx.respond(NO_AUDIT, ephemeral=True)
            return
        await ctx.respond("Sending five attention pings.", ephemeral=True)
        mentions = discord.AllowedMentions(users=[discord.Object(target)], everyone=False, roles=False,
                                           replied_user=False)
        text = f"<@{target}> -- {ctx.author.display_name}: {reason}"
        try:
            for i in range(5):
                if i:
                    await asyncio.sleep(2)
                await send(text, allowed_mentions=mentions)
        except (discord.HTTPException, discordauth.Blocked):
            log.exception("blowup send failed")
            await ctx.respond(FAILED, ephemeral=True)

    docker_cmd = bot.create_group("docker", "Docker containers")

    @docker_cmd.command(description="Restart a container")
    @discord.option("container", str, description="Container name", autocomplete=container_names)
    async def restart(ctx: discord.ApplicationContext, container: str) -> None:
        await restarts.restart_container(bot, ctx, container)

    restarts.register(bot, docker_cmd)
    ratecmd.register(bot)
    chartgen.register(bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.environ.get("SHAZZZBOT_CONFIG", "/config/config.toml")
    try:
        cfg = config.load(path)
    except formats.FormatError as e:
        raise SystemExit(f"{path}: {e}") from None
    logsetup.configure(cfg.log_level, cfg.debug_hours)
    bot = Bot(cfg)
    register(bot)
    bot.run(cfg.token)
