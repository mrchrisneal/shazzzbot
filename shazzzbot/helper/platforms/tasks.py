from __future__ import annotations

import io
import json
import logging
import os
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

from ..files import SourceError, trusted_open
from . import task_durations as tasktimes
from .task_kinds import script_kind
from .task_parsing import _SCRIPT, parse_cron, smart_warnings

LOG_LIMIT = 1024 * 1024
TOTAL_LIMIT = 8 * LOG_LIMIT
FILE_LIMIT = 1024
SOURCES = ("cron.d", "cron.hourly", "cron.daily", "cron.weekly", "cron.monthly", "crontabs", "user.scripts",
           "user.scripts.state", "unraidcheck", "appdata.backup", "parity", "localtime", "emhttp")
CRON_SOURCES = frozenset(SOURCES[:6])
log = logging.getLogger(__name__)


class CollectionLimit(ValueError):
    pass


class Collector:
    def __init__(self, paths: Mapping[str, Path], strict: bool = False):
        self.paths, self.strict = paths, strict
        self.used = 0
        self.failed: set[str] = set()

    def _skip(self, parts: tuple[str, ...], error: Exception) -> None:
        if isinstance(error, SourceError):
            if parts[0] in self.failed:
                return
            self.failed.add(parts[0])
            log.warning("tasks source skipped %r (%s)", parts[0], type(error).__name__)
        else:
            log.warning("tasks entry skipped %r (%s)", parts, type(error).__name__)

    def _path(self, parts: tuple[str, ...]) -> Path:
        return self.paths[parts[0]].joinpath(*parts[1:])

    @contextmanager
    def _open(self, parts: tuple[str, ...], *, directory: bool = False, follow: bool = False) -> Iterator[int]:
        if self.strict:
            if parts[0] in self.failed:
                raise SourceError("source already rejected")
            links = parts[0] in CRON_SOURCES or parts[0] == "localtime" and follow
            with trusted_open(self.paths[parts[0]], parts[1:], directory=directory, follow=links) as fd:
                yield fd
        else:
            flags = os.O_RDONLY | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0) \
                | (0 if follow else os.O_NOFOLLOW)
            fd = os.open(str(self._path(parts)), flags)
            try:
                yield fd
            finally:
                os.close(fd)

    def _names(self, *parts: str) -> list[str]:
        try:
            with self._open(parts, directory=True, follow=True) as fd, os.scandir(fd) as entries:
                names = [entry.name for entry in islice(entries, FILE_LIMIT + 1)]
            if len(names) > FILE_LIMIT:
                raise CollectionLimit("too many task source files")
            return sorted(names)
        except FileNotFoundError:
            return []
        except CollectionLimit:
            raise
        except (OSError, ValueError) as error:
            self._skip(parts, error)
            return []

    def _data(self, *parts: str, follow: bool = False) -> tuple[bytes, float]:
        try:
            with self._open(parts, follow=follow) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > LOG_LIMIT:
                    raise ValueError("invalid task source file")
                data = stream.read(LOG_LIMIT + 1)
                self.used += len(data)
                if self.used > TOTAL_LIMIT:
                    raise CollectionLimit("task source size limit exceeded")
                if len(data) > LOG_LIMIT:
                    raise ValueError("task source file size limit exceeded")
                return data, info.st_mtime
        except FileNotFoundError:
            return b"", 0.0
        except CollectionLimit:
            raise
        except (OSError, ValueError) as error:
            self._skip(parts, error)
            return b"", 0.0

    def _read(self, *parts: str) -> str:
        try:
            return self._data(*parts)[0].decode("utf-8")
        except UnicodeError as error:
            self._skip(parts, error)
            return ""

    def _log(self, *parts: str) -> tuple[str, float]:
        data, at = self._data(*parts)
        return data.decode("utf-8", errors="replace"), at

    def _mtimes(self, *parts: str) -> dict[str, float]:
        out = {}
        for name in self._names(*parts):
            try:
                with self._open((*parts, name), follow=True) as fd:
                    out[name] = os.fstat(fd).st_mtime
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                self._skip((*parts, name), error)
        return out

    def scheduled_files(self, runparts: dict[str, str]) -> list[dict[str, str]]:
        jobs = []
        for frequency in ("hourly", "daily", "weekly", "monthly"):
            cron = runparts.get(frequency)
            if not cron:
                continue
            sub = "cron." + frequency
            for name in self._names(sub):
                if name.startswith("user.script.start."):
                    continue
                if self.strict:
                    try:
                        with self._open((sub, name)) as fd:
                            executable = bool(os.fstat(fd).st_mode & 0o111)
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError) as error:
                        self._skip((sub, name), error)
                        continue
                else:
                    path = self._path((sub, name))
                    executable = path.is_file() and os.access(path, os.X_OK)
                if executable:
                    jobs.append({"name": name, "cron": cron})
        return jobs

    def completed(self) -> dict[str, tasktimes.Record]:
        out = {}
        text, observed = self._log("parity", "parity-checks.log")
        if run := tasktimes.parity_history(text, observed):
            out["Parity check"] = run
        try:
            zone_data, _ = self._data("localtime", follow=True)
            zone = ZoneInfo.from_file(io.BytesIO(zone_data)) if zone_data else None
        except ValueError:
            if self.strict:
                raise
            zone = None
        finished = self._mtimes("user.scripts.state", "finished")
        for name in finished | self._mtimes("user.scripts.state", "running"):
            text, _ = self._log("user.scripts.state", "tmpScripts", name, "log.txt")
            run = tasktimes.user_script(text, zone)
            if name in finished:
                at = finished[name]
                run = {"at": at, "seconds": run["seconds"] if run and int(run["at"]) == int(at) else None}
            if run:
                out[name] = run
        if "running" not in self._mtimes("appdata.backup"):
            text, observed = self._log("appdata.backup", "ab.log")
            if run := tasktimes.appdata_backup(text, zone, observed):
                out["Appdata.Backup"] = run
        return out

    def collect(self) -> dict:
        jobs, runparts = [], {}
        for sub in ("cron.d", "crontabs"):
            for name in self._names(sub):
                j, r = parse_cron(self._read(sub, name))
                jobs += j
                runparts.update(r)
        jobs += self.scheduled_files(runparts)
        for entry in json.loads(self._read("user.scripts", "schedule.json") or "{}").values():
            cron = runparts.get(entry.get("frequency", ""))
            if cron:
                match = _SCRIPT.search(entry.get("script", ""))
                jobs.append({"name": match.group(1) if match else entry.get("id", "")[8:], "cron": cron})
        check = json.loads(self._read("unraidcheck", "result.json") or "{}")
        running = self._mtimes("user.scripts.state", "running")
        kinds = {name: kind for name in running
                 if (kind := script_kind(self._read("user.scripts", "scripts", name, "script")))}
        if (started := self._mtimes("appdata.backup").get("running")) is not None:
            running["Appdata.Backup"] = started
            kinds["Appdata.Backup"] = "appdata_backup_plugin"
        return {"generated": time.time(), "jobs": jobs,
                "smart": smart_warnings(self._read("emhttp", "monitor.ini")),
                "os_update": check.get("version", "") if check.get("isNewer") is True else "",
                "running": running, "kinds": kinds,
                "finished": self._mtimes("user.scripts.state", "finished"), "durations": self.completed()}
