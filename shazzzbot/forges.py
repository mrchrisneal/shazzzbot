"""Forge settings, cached polling, and one shared section renderer."""
import asyncio
import contextlib
import ipaddress
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

import aiohttp

from . import forgejo, github, schema
from .forge import API, Client, Entry, Problem, clean, credential, display
from .panel import CRITICAL, FINE, WARNING, Part, Row, Section, clip, more, plural
from .tasktimes import elapsed

CLIENTS: dict[str, type[Client]] = {"forgejo": forgejo.Client, "github": github.Client}
EVERY = 60
log = logging.getLogger(__name__)
HEADS = {"ci": ("CI", ("Repo", "Latest CI", "Age")),
         "runs": ("Recent runs", ("Repo", "Run", "Result", "Duration", "Age")),
         "runners": ("Runners", ("Runner", "State", "Last job")),
         "pushes": ("Pushes", ("SHA", "Commit", "Age")),
         "issues": ("Open issues and PRs", ("Repo", "Issues", "PRs"))}


@dataclass(frozen=True)
class Settings:
    type: str
    url: str
    token_env: str
    repos: tuple[str, ...]
    show: tuple[str, ...]
    name: str = schema.default("forges", "name")
    system_row: bool = schema.default("forges", "system_row")
    latest: int = schema.default("forges", "latest")
    pinned: tuple[str, ...] = tuple(schema.default("forges", "pinned"))
    filter: dict[str, tuple[str, ...]] = field(default_factory=lambda: schema.default("forges", "filter"))


def _url(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None \
            or "?" in value or "#" in value or parsed.port == 0 or parsed.netloc.endswith(":") \
            or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", parsed.netloc) \
            or not re.fullmatch(r"/[A-Za-z0-9_./~-]*|", parsed.path) \
            or any(part in (".", "..") for part in parsed.path.split("/")):
        raise ValueError
    host = parsed.hostname
    if "[" in parsed.netloc:
        ipaddress.IPv6Address(host)
    elif "." in host and all(c in "0123456789." for c in host):
        ipaddress.IPv4Address(host)
    elif len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
                                   for label in host.removesuffix(".").split(".")):
        raise ValueError
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl().rstrip("/")


def _references(value: list[str], known: dict[str, str], setting: str, prefix: str) -> tuple[str, ...]:
    keys = [repo.casefold() for repo in value]
    if any(key not in known for key in keys):
        raise SystemExit(f"{prefix}: {setting} contains an unknown repo; use tracked repos")
    if len(set(keys)) != len(keys):
        raise SystemExit(f"{prefix}: {setting} contains duplicate repos")
    return tuple(known[key] for key in keys)


def _preferences(entry: dict, repos: list[str], show: list[str], latest: int,
                 prefix: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    known = {repo.casefold(): repo for repo in repos}
    pinned = _references(entry.get("pinned", schema.default("forges", "pinned")), known, "pinned", prefix)
    if len(pinned) > latest:
        raise SystemExit(f"{prefix}: pinned must not contain more repos than latest")
    filters = entry.get("filter", schema.default("forges", "filter"))
    allowed = set(schema.names("forge_filter")).intersection(show)
    if set(filters) - allowed:
        raise SystemExit(f"{prefix}: filter must be a table with enabled repo sections: {sorted(allowed)}")
    return pinned, {kind: _references(value, known, f"filter.{kind}", prefix)
                    for kind, value in filters.items()}


def validate(raw: object) -> list[Settings]:
    schema.validate_field("config", "forges", raw, "forges", items=False)
    out: list[Settings] = []
    seen = set()
    for i, entry in enumerate(cast(list, raw), 1):
        prefix = f"forges entry {i}"
        schema.validate("forges", entry, prefix)
        kind = entry["type"]
        client = CLIENTS[kind]
        name = entry.get("name", schema.default("forges", "name", discriminator=kind))
        try:
            url = _url(entry.get("url", schema.default("forges", "url", discriminator=kind)))
        except ValueError:
            raise SystemExit(f"{prefix}: url must be an HTTP or HTTPS base URL without credentials or query") from None
        if (kind, url) in seen:
            raise SystemExit(f"{prefix}: duplicate forge")
        seen.add((kind, url))
        env = entry["token_env"]
        try:
            credential(os.environ.get(env, ""))
        except Problem:
            raise SystemExit(f"{prefix}: token_env {env} is unset or invalid") from None
        repos = entry["repos"]
        if any(any(part in (".", "..") for part in repo.split("/")) for repo in repos):
            raise SystemExit(f"{prefix}: repos must list 1 to 10 owner/name repositories")
        if len({repo.casefold() for repo in repos}) != len(repos):
            raise SystemExit(f"{prefix}: duplicate repository")
        show = entry.get("show", schema.default("forges", "show", discriminator=kind))
        if set(show) - set(client.features):
            raise SystemExit(f"{prefix}: show must list items of {list(client.features)}, without duplicates")
        system_row = entry.get("system_row", schema.default("forges", "system_row"))
        if system_row and any(c.system_row for c in out):
            raise SystemExit(f"{prefix}: system_row must be a bool and true for at most one forge")
        latest = entry.get("latest", schema.default("forges", "latest"))
        pinned, filters = _preferences(entry, repos, show, latest, prefix)
        features = tuple(item for item in client.features if item in show)
        out.append(Settings(kind, url, env, tuple(repos), features, name, system_row, latest, pinned, filters))
    return out


def key(index: int) -> str:
    return f"forge_{index}"


def group(name: str) -> str:
    return "forges" if re.fullmatch(r"forge_[0-9]+", name) else name


def _latest(entries: tuple[Entry, ...], limit: int) -> tuple[Entry, ...]:
    return tuple(sorted(entries, key=lambda entry: entry.at if entry.at is not None else -math.inf,
                        reverse=True)[:limit])


def _summary(part: Part, hidden: int, noun: str) -> Part:
    tail = [(lv, (text, *("",) * (len(part.rows[0][1]) - 1))) for lv, text in more([(hidden, noun)])]
    return part._replace(rows=[*part.rows, *tail])


def _part(kind: str, entries: tuple[Entry, ...], now: float, token: str, source: str,
          repo: str, hidden: int = 0) -> tuple[Part, list[Row]]:
    title, heads = HEADS[kind]
    if kind == "pushes":
        title = "Pushes - " + clip(clean(repo, token), 32)
    rows: list[tuple[int, tuple[str, ...]]] = [(FINE, heads)]
    issues = []
    for entry in entries:
        name = clean(entry.name, token, 210)
        values = tuple(clean(value, token, 64) for value in entry.values)
        if kind == "issues":
            values = (*values, "")[:2]
        else:
            age = f"{elapsed(now - entry.at)} ago" if entry.at is not None and entry.at <= now else "-"
            values = (values[0], age)
        label = clip(name, 24)
        if kind == "pushes" and " " in name:
            label = name.rsplit(" ", 1)[1]
        elif kind == "pushes":
            label = "-"
        rows.append((entry.level, (label, *values)))
        if entry.level > FINE:
            state = " ".join(value for value in entry.values if value).strip()
            issues.append((entry.level, f"{source} {HEADS[kind][0]}: {name} {clean(state, token)}"))
    part = Part(rows, title, "lll" if kind != "issues" else "lrr", heads=True, cut=1 if kind == "pushes" else 0)
    return _summary(part, hidden, "runner" if kind == "runners" else "repo"), issues


def _runs(entries: tuple[Entry, ...], now: float, token: str, source: str,
          hidden: int = 0) -> tuple[list[Part], list[Row]]:
    rows = [(FINE, HEADS["runs"][1])]
    issues = []
    for entry in entries:
        name = clean(entry.name, token, 24)
        if len(entry.values) == 2:
            run, state = (clean(value, token, 64) for value in entry.values)
            end = now if state == "running" else entry.finished
            duration = elapsed(max(0, end - entry.at)) if entry.at is not None and end is not None else "-"
            age = elapsed(now - entry.at) + " ago" if entry.at is not None and entry.at <= now else "-"
        else:
            run, state, duration, age = "-", clean(entry.values[0], token, 64), "-", "-"
        rows.append((entry.level, (name, run, state, duration, age)))
        if entry.level > FINE:
            detail = f"{clean(entry.values[0], token, 80)} {state}" if len(entry.values) == 2 else state
            issues.append((entry.level, f"{source} Recent runs: {name} {detail}"))
    part = Part(rows, "Recent runs", "lllrr", heads=True, cut=1, only="embed image")
    narrow = [(lv, (clip(c[0], 8), c[1], clip(c[2], 9), c[3], c[4])) for lv, c in rows]
    return [_summary(part, hidden, "run"), _summary(part._replace(rows=narrow, only="text"), hidden, "run")], issues


class Monitor:
    def __init__(self, settings: Settings, index: int):
        self.settings, self.index = settings, index
        self.client = CLIENTS[settings.type]()
        self.data: dict[str, tuple[Entry, ...]] = {kind: (Entry("Status", ("checking",)),) for kind in settings.show}
        self.at = -math.inf
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.ready = False
        self._owners: dict[int, str] = {}

    def _repos(self, kind: str) -> tuple[str, ...]:
        allowed = {repo.casefold() for repo in self.settings.filter.get(kind, self.settings.repos)}
        return tuple(repo for repo in self.settings.repos if repo.casefold() in allowed)

    def _repo(self, kind: str, entry: Entry, token: str) -> str:
        if id(entry) in self._owners:
            return self._owners[id(entry)]
        name = entry.name.rsplit(" ", 1)[0] if kind == "pushes" else entry.name
        for repo in self.settings.repos:
            if name.casefold() in {repo.casefold(), display(repo, token, self.settings.url, 210).casefold()}:
                return repo
        return name

    def _entries(self, kind: str, token: str) -> tuple[Entry, ...]:
        entries = self.data[kind]
        if kind not in self.settings.filter:
            return entries
        allowed = {repo.casefold() for repo in self._repos(kind)}
        return tuple(entry for entry in entries if entry.name == "Status"
                     or self._repo(kind, entry, token).casefold() in allowed)

    def _active_runs(self, entries: tuple[Entry, ...], token: str) -> tuple[Entry, ...]:
        latest: dict[tuple[str, str], Entry] = {}
        for entry in entries:
            if len(entry.values) != 2:
                continue
            key = (self._repo("runs", entry, token), entry.values[0].rsplit(" #", 1)[0])
            previous = latest.get(key)
            at = entry.at if entry.at is not None else -math.inf
            before = previous.at if previous is not None and previous.at is not None else -math.inf
            if previous is None or at > before:
                latest[key] = entry
        active = {id(entry) for entry in latest.values()}
        return tuple(entry for entry in entries if len(entry.values) != 2 or id(entry) in active)

    def _selected(self, kind: str, entries: tuple[Entry, ...], token: str) -> tuple[Entry, ...]:
        if kind == "runners":
            return _latest(entries, self.settings.latest)
        order = {repo.casefold(): i for i, repo in enumerate(self.settings.repos)}
        ordered = tuple(sorted(entries, key=lambda entry:
                               order.get(self._repo(kind, entry, token).casefold(), len(order))))
        ranked = _latest(ordered, len(ordered))
        if kind != "runs":
            pins = {repo.casefold(): i for i, repo in enumerate(self.settings.pinned)}
            ranked = tuple(sorted(ranked, key=lambda entry:
                                  pins.get(self._repo(kind, entry, token).casefold(), len(pins))))
        return ranked[:self.settings.latest]

    def _groups(self, entries: tuple[Entry, ...], token: str) -> dict[str, tuple[Entry, ...]]:
        grouped: dict[str, list[Entry]] = {}
        for entry in entries:
            grouped.setdefault(self._repo("pushes", entry, token), []).append(entry)
        return {repo: tuple(rows) for repo, rows in grouped.items()}

    def start(self, session: aiohttp.ClientSession) -> asyncio.Task | None:
        if time.monotonic() - self.at >= EVERY and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.refresh(session))
            return self.task
        return None

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _one(self, api: API, kind: str, repo: str | None) -> list[Entry]:
        reason = "could not read (invalid data)"
        try:
            method = getattr(self.client, kind)
            return await method(api, repo) if repo is not None else await method(api)
        except Problem as error:
            reason = "needs admin token" if kind == "runners" and error.status == 403 else str(error)
            if reason != "auth failed" and reason != "needs admin token":
                reason = f"could not read ({reason})"
        except Exception as error:
            log.exception("forge %d %s poll failed (%s)", self.index + 1, kind, type(error).__name__, exc_info=False)
        return [Entry(api.text(repo or "Runners", 210), (api.text(reason),), level=WARNING)]

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        async with self.lock:
            if time.monotonic() - self.at < EVERY:
                return
            self.at = time.monotonic()
            cfg = self.settings
            try:
                api = API(session, cfg.url + self.client.prefix, os.environ.get(cfg.token_env, ""), self.client.scheme)
                api.recent_runs = "runs" in cfg.show
                work = [(kind, repo) for kind in cfg.show
                        for repo in (self._repos(kind) if kind != "runners" else (None,))]
                result = await asyncio.gather(*(self._one(api, kind, repo) for kind, repo in work))
                groups: dict[str, list[Entry]] = {kind: [] for kind in cfg.show}
                owners: dict[int, str] = {}
                for (kind, repo), entries in zip(work, result):
                    groups[kind].extend(entries)
                    if repo is not None:
                        owners.update((id(entry), repo) for entry in entries)
                self.data = {kind: tuple(entries) for kind, entries in groups.items()}
                self._owners = owners
            except Problem:
                self.data = {kind: (Entry("Status", ("auth failed",), level=WARNING),) for kind in cfg.show}
                self._owners = {}
            finally:
                self.at = time.monotonic()
            self.ready = True

    def summary(self) -> Row:
        token = os.environ.get(self.settings.token_env, "").strip()
        entries = [entry for kind in self.data for entry in (
            self._active_runs(self._entries(kind, token), token) if kind == "runs" else self._entries(kind, token))]
        if not self.ready:
            return FINE, "checking"
        if entries and all(entry.values[0] in ("could not read (unreachable)", "could not read (timeout)")
                           for entry in entries):
            return CRITICAL, "unreachable"
        ci = self._entries("ci", token) if "ci" in self.data else ()
        counts = {state: sum(entry.values[0] == state for entry in ci)
                  for state in ("pass", "fail", "running", "cancelled", "no runs")}
        words = {"pass": "passing",  # a CI state word, not a password. reviewed 2026-11  # nosec B105
                 "fail": "failing", "running": "running", "cancelled": "cancelled", "no runs": "without runs"}
        detail = ", ".join(f"{n} {words[state]}" for state, n in counts.items() if n)
        text = plural(len(self.settings.repos), "repo") + (": " + detail if detail else "")
        return max((entry.level for entry in entries), default=FINE), text

    def section(self, now: float | None = None) -> Section:
        now = time.time() if now is None else now
        cfg = self.settings
        token = os.environ.get(cfg.token_env, "").strip()
        title = display(cfg.name or self.client.label, token, cfg.url, 120)
        parts = []
        issues = []
        for kind in cfg.show:
            entries = self._entries(kind, token)
            if kind == "runs":
                _, problems = _runs(self._active_runs(entries, token), now, token, title)
                shown = self._selected(kind, entries, token)
                run_parts, _ = _runs(shown, now, token, title, len(entries) - len(shown))
                parts.extend(run_parts)
                issues.extend(problems)
                continue
            batches = self._groups(entries, token) if kind == "pushes" else {"": entries}
            for repo, batch in batches.items():
                _, problems = _part(kind, batch, now, token, title, display(repo, token, cfg.url, 210))
                issues.extend(problems)
            if kind == "pushes":
                representatives = tuple(_latest(batch, 1)[0] for batch in batches.values())
                shown = self._selected(kind, representatives, token)
                for entry in shown:
                    repo = self._repo(kind, entry, token)
                    part, _ = _part(kind, batches[repo], now, token, title, display(repo, token, cfg.url, 210))
                    parts.append(part)
                tail = more([(len(batches) - len(shown), "repo")])
                if tail:
                    parts.append(Part([(lv, (text,)) for lv, text in tail], HEADS[kind][0], cut=None))
            else:
                shown = self._selected(kind, entries, token)
                part, _ = _part(kind, shown, now, token, title, "", len(entries) - len(shown))
                parts.append(part)
        return Section(key(self.index), title, parts, issues=issues, issues_limit=5)


async def poll(monitors: list[Monitor], session: aiohttp.ClientSession, wait: float) -> None:
    first = []
    for monitor in monitors:
        task = monitor.start(session)
        if task is not None and not monitor.ready:
            first.append(task)
    if first:
        await asyncio.wait(first, timeout=wait)
