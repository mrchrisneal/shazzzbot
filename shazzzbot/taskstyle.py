"""Colour-only emphasis for running maintenance tasks."""
from dataclasses import dataclass
from typing import cast

from . import schema


@dataclass(frozen=True)
class Settings:
    long_running_minutes: float = schema.default("tasks", "long_running_minutes")
    heavy_kinds: tuple[str, ...] = tuple(schema.default("tasks", "heavy_kinds"))

    def yellow(self, elapsed: float | None, last: float | None, kind: str) -> bool:
        return kind in self.heavy_kinds or self.long_running_minutes > 0 and any(
            value is not None and value > self.long_running_minutes * 60 for value in (elapsed, last))


def validate(value: object) -> Settings:
    schema.validate("tasks", value, "tasks")
    raw = cast(dict, value)
    minutes = raw.get("long_running_minutes", schema.default("tasks", "long_running_minutes"))
    if minutes is False:
        minutes = 0
    kinds = raw.get("heavy_kinds", schema.default("tasks", "heavy_kinds"))
    return Settings(float(minutes), tuple(kinds))
