"""Bounded per-app histories with throttled atomic persistence in the bot's data directory."""
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from heapq import nlargest
from math import ceil, inf

from .helper import formats
from .host import Instance, Snapshot
from .spark import Sample

MAX_APPS = 256
MAX_INSTANCES = 64
MAX_POINTS = 1_250_000
MAX_BYTES = 64 * 1024 * 1024
SAVE_EVERY = 60
log = logging.getLogger(__name__)


def _number(value: object, cap: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= cap


def _name(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable()


def _instance(value: object) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(part, str) and 0 < len(part) <= 32 and part.isascii() and part.isdecimal() for part in value)


class AppHistory:
    def __init__(self, interval: int, path: str | None = None, window: int = 0):
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.interval, self.path, self._window = interval, path, window
        self.samples: dict[str, dict[str, deque[Sample]]] = {"cpu": {}, "ram": {}}
        self.instances: dict[str, dict[str, dict[Instance, float]]] = {"cpu": {}, "ram": {}}
        self._data_lock, self._write_lock = threading.Lock(), threading.RLock()
        self._last_save, self._closed = -inf, False
        if path is not None:
            self._load(path, window)

    def _limit(self, window: int, names: int = 1) -> int:
        if window <= 0:
            raise ValueError("window must be positive")
        return max(1, min(MAX_POINTS // max(1, names), max(2, ceil(window / self.interval) + 1)))

    def _points(self, values: object, window: int, now: float, limit: int) -> deque[Sample]:
        if not isinstance(values, list):
            raise TypeError("invalid app samples")
        points: deque[Sample] = deque(maxlen=limit)
        last = -inf
        for point in values:
            if not isinstance(point, list) or len(point) != 2 or not _number(point[0], 1e12) \
                    or not _number(point[1], 1e15) or point[0] <= last:
                raise ValueError("invalid app sample")
            at, value = map(float, point)
            last = at
            if now - window <= at <= now:
                points.append((at, value))
        return points

    def _restore(self, data: object, window: int, now: float) -> None:
        data = formats.load(data, "app history", 2)
        if set(data) != {"format", "samples", "instances"}:
            raise ValueError("invalid app history")
        raw = data["samples"]
        if not isinstance(raw, dict) or set(raw) != {"cpu", "ram"} or not all(
                isinstance(apps, dict) and len(apps) <= MAX_APPS and all(_name(name) for name in apps)
                for apps in raw.values()):
            raise ValueError("invalid app names")
        limit = self._limit(window, sum(map(len, raw.values())))
        restored: dict[str, dict[str, deque[Sample]]] = {"cpu": {}, "ram": {}}
        count = 0
        for metric, apps in raw.items():
            for name, values in apps.items():
                points = self._points(values, window, now, limit)
                count += len(values)
                if count > MAX_POINTS:
                    raise ValueError("too many app samples")
                if points:
                    restored[metric][name] = points
        identities = self._restore_ids(data["instances"], raw, window, now)
        self.samples = restored
        self.instances = {metric: {name: ids for name, ids in names.items() if name in restored[metric]}
                          for metric, names in identities.items()}

    def _restore_ids(self, raw: object, samples: dict, window: int, now: float) -> dict:
        if not isinstance(raw, dict) or set(raw) != {"cpu", "ram"}:
            raise ValueError("invalid app instances")
        out: dict[str, dict[str, dict[Instance, float]]] = {"cpu": {}, "ram": {}}
        for metric, apps in raw.items():
            if not isinstance(apps, dict) or set(apps) - set(samples[metric]):
                raise ValueError("invalid instance names")
            for name, records in apps.items():
                if not isinstance(records, list) or len(records) > MAX_INSTANCES:
                    raise ValueError("invalid instances")
                seen: dict[Instance, float] = {}
                for record in records:
                    if not isinstance(record, list) or len(record) != 3 or not _instance(record[:2]) \
                            or not _number(record[2], 1e12) or tuple(record[:2]) in seen:
                        raise ValueError("invalid instance")
                    seen[(record[0], record[1])] = float(record[2])
                out[metric][name] = {key: at for key, at in seen.items() if now - window <= at <= now}
        return out

    def _load(self, path: str, window: int) -> None:
        self._limit(window)
        try:
            # Operator-selected state path; restored app names never become file paths. reviewed 2026-11
            # bearer:disable python_lang_path_traversal
            with open(path, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("app history file too large")
            self._restore(json.loads(raw), window, time.time())
        except FileNotFoundError:
            log.info("app history file is missing; starting empty")
        except formats.FormatError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            log.warning("app history file is corrupt or unreadable; starting empty")

    def _expire(self, window: int, now: float) -> None:
        for metric, apps in self.samples.items():
            for name, points in list(apps.items()):
                while points and points[0][0] < now - window:
                    points.popleft()
                while points and points[-1][0] > now:
                    points.pop()
                if not points:
                    del apps[name]
                    self.instances[metric].pop(name, None)
            for name, identities in self.instances[metric].items():
                self.instances[metric][name] = {key: at for key, at in identities.items()
                                                if now - window <= at <= now}

    def _record_ids(self, metric: str, name: str, snapshot: Snapshot, now: float) -> None:
        if name in snapshot.grouped_apps or name.startswith(("*", "VM:", "LXC:")):
            self.instances[metric].pop(name, None)
            return
        seen = self.instances[metric].setdefault(name, {})
        seen.update({key: now for key in snapshot.app_instances.get(name, ()) if _instance(key)})
        keep = nlargest(MAX_INSTANCES, seen, key=lambda key: (seen[key], key))
        self.instances[metric][name] = {key: seen[key] for key in keep}

    def _cap(self, window: int) -> None:
        for metric, apps in self.samples.items():
            keep = nlargest(min(MAX_APPS, max(1, MAX_POINTS // 2)), apps.items(),
                            key=lambda item: (item[1][-1][0], item[1][-1][1], item[0]))
            self.samples[metric] = dict(keep)
            keep_names = set(self.samples[metric])
            self.instances[metric] = {name: ids for name, ids in self.instances[metric].items() if name in keep_names}
        limit = self._limit(window, sum(map(len, self.samples.values())))
        self.samples = {metric: {name: points if points.maxlen == limit else deque(points, maxlen=limit)
                                for name, points in apps.items()} for metric, apps in self.samples.items()}

    def add(self, snapshot: Snapshot, window: int, now: float) -> None:
        self._limit(window)
        if not _number(now, 1e12):
            raise ValueError("invalid app sample time")
        with self._data_lock:
            self._expire(window, now)
            if window > self._window:
                self.samples = {metric: {name: deque(points) for name, points in apps.items()}
                                for metric, apps in self.samples.items()}
            self._window = window
            for metric, full, top in (("cpu", snapshot.apps_cpu, snapshot.top_cpu),
                                      ("ram", snapshot.apps_ram, snapshot.top_ram)):
                totals: defaultdict[str, float] = defaultdict(float)
                for name, value in full.items() if full is not None else top:
                    if _name(name) and _number(value, 1e15):
                        totals[name] += value
                for name, value in totals.items():
                    if not _number(value, 1e15):
                        continue
                    points = self.samples[metric].setdefault(name, deque())
                    while points and points[-1][0] >= now:
                        points.pop()
                    points.append((now, value))
                    self._record_ids(metric, name, snapshot, now)
            self._cap(window)

    def multiple(self, metric: str, name: str, start: float, end: float) -> bool:
        with self._data_lock:
            return sum(start <= at <= end for at in self.instances[metric].get(name, {}).values()) > 1

    def values(self, metric: str, name: str, start: float, end: float) -> list[Sample]:
        with self._data_lock:
            return [(t, value) for t, value in self.samples[metric].get(name, ()) if start <= t <= end]

    def store(self, *, force: bool = False) -> None:
        with self._write_lock:
            if self.path is None or self._closed or not force and time.monotonic() - self._last_save < SAVE_EVERY:
                return
            self._last_save = time.monotonic()
            with self._data_lock:
                samples = {metric: {name: list(points) for name, points in apps.items()}
                           for metric, apps in self.samples.items()}
                instances = {metric: {name: [[*key, at] for key, at in ids.items()] for name, ids in names.items()}
                             for metric, names in self.instances.items()}
            try:
                raw = json.dumps(formats.dump({"samples": samples, "instances": instances}, 2), allow_nan=False,
                                 separators=(",", ":")).encode("ascii")
                if len(raw) > MAX_BYTES:
                    raise ValueError("app history file too large")
                self._write(raw, self.path)
            except (OSError, ValueError) as error:
                log.warning("could not save app history (%s); keeping it in memory", type(error).__name__)

    def _write(self, raw: bytes, path: str) -> None:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".apps-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def close(self) -> None:
        with self._write_lock:
            self.store(force=True)
            self._closed = True
