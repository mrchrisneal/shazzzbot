"""Completed task durations and persisted completion state."""
from .helper.platforms import task_durations
from .helper.platforms.task_durations import _number

Record = task_durations.Record
appdata_backup = task_durations.appdata_backup
parity_history = task_durations.parity_history
user_script = task_durations.user_script


def restore(value: object) -> dict[str, Record]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Record] = {}
    for name, record in value.items():
        if not isinstance(name, str) or not name or not isinstance(record, dict) or not _number(record.get("at")) \
                or "seconds" not in record:
            continue
        seconds = record["seconds"]
        if seconds is None or _number(seconds):
            out[name] = {"at": float(record["at"]), "seconds": None if seconds is None else float(seconds)}
    return out


def merge(saved: dict[str, Record], value: object, now: float) -> bool:
    changed = False
    for name, record in restore(value).items():
        if record["at"] > now:
            continue
        previous = saved.get(name)
        if previous and (record["at"] < previous["at"] or record["at"] == previous["at"]
                         and (previous["seconds"] is not None or record["seconds"] is None)):
            continue
        saved[name] = record
        changed = True
    return changed


def elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds >= 48 * 3600:
        days, hours = divmod(seconds // 3600, 24)
        return f"{days}d" + (f" {hours}h" if hours else "")
    hours, minutes = divmod(seconds // 60, 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
