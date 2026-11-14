from __future__ import annotations

import configparser
import os
import re

_JOB = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.+)$")

_SCRIPT = re.compile(r"/user\.scripts/scripts/([^/]+)/script")

_RUNPARTS = re.compile(r"run-parts\s+/etc/cron\.(hourly|daily|weekly|monthly)")


def _name(comment: str, cmd: str) -> str:
    if m := _SCRIPT.search(cmd):
        return m.group(1)
    c = re.sub(r"^(Generated|Job for)\s+", "", comment.lstrip("#").strip())
    c = re.sub(r"\s*(schedule|cron settings)?\s*:?\s*$", "", c)
    if c:
        return c[:1].upper() + c[1:]
    words = cmd.split()
    return os.path.basename(next((w for w in words if "/" in w), words[0]))


def parse_cron(text: str) -> tuple[list[dict], dict[str, str]]:
    """(jobs, run-parts schedules). A job takes its name from the comment line right above it."""
    jobs, runparts, comment = [], {}, ""
    for line in map(str.strip, text.splitlines()):
        if line.startswith("#"):
            comment = line
            continue
        m = _JOB.match(line)
        if m and not line.startswith("@"):
            cron, cmd = m.groups()
            if r := _RUNPARTS.search(cmd):
                runparts[r.group(1)] = cron
            else:
                jobs.append({"name": _name(comment, cmd), "cron": cron})
        comment = ""
    return jobs, runparts


def smart_warnings(text: str) -> list[str]:
    """One entry per SMART attribute Unraid's monitor raised on a disk not acknowledged, as its dashboard shows them."""
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    keys = [k.rpartition(".") for k in (ini["smart"] if ini.has_section("smart") else {})]
    acked = {n for n, _, attr in keys if attr == "ack"}
    return sorted(n for n, _, attr in keys if attr != "ack" and n not in acked)
