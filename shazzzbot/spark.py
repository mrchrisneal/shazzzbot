"""Time-bucketed braille bars with a dotted marker for missing samples."""
from collections.abc import Sequence
from math import ceil, isfinite

from .glyphs import BRAILLE, NO_DATA

Sample = tuple[float, float]
TREND_WIDTH = 15
NICE_BUCKETS = (1, 2, 3, 5, 10, 15, 30, 60, 120, 180, 240, 300, 600, 900,
                1200, 1800, 3600, 7200, 14400, 18000, 21600)


def bucket_layout(window: float, max_width: int) -> tuple[int, int]:
    limit = max_width * 2
    for bucket in NICE_BUCKETS:
        if ceil(window / bucket) <= limit:
            return bucket, ceil(ceil(window / bucket) / 2)
    bucket = NICE_BUCKETS[-1]
    while ceil(window / bucket) > limit:
        bucket *= 2
    return bucket, ceil(ceil(window / bucket) / 2)


def _buckets(values: Sequence[Sample], width: int, start: float, end: float,
             bucket: int | None = None) -> list[float | None]:
    if width <= 0 or not start < end:
        raise ValueError("width and time span must be positive")
    buckets: list[float | None] = [None] * width
    if bucket is not None:
        count = ceil((end - start) / bucket)
        origin = end - count * bucket
        offset = width - count
    for t, value in values:
        if not (start <= t <= end and isfinite(value)):
            continue
        if bucket is None:
            column = min(width - 1, int((t - start) / (end - start) * width))
        else:
            column = offset + min(count - 1, int((t - origin) / bucket))
        previous = buckets[column]
        if previous is None or value > previous:
            buckets[column] = value
    return buckets


def limits(values: Sequence[Sample], start: float, end: float) -> tuple[float, float]:
    samples = [value for t, value in values if start <= t <= end and isfinite(value)]
    return min(samples, default=0.0), max(samples, default=0.0)


def _scale(buckets: list[float | None], lo: float, hi: float) -> list[float | None]:
    span = hi - lo
    return [None if v is None else max(0.0, min(1.0, (v - lo) / span)) if span else 0.0 for v in buckets]


def spark(values: Sequence[Sample], width: int, *,
          start: float, end: float, bucket: int | None = None) -> str:
    """One row of braille, two buckets per cell, with a dotted no-data marker."""
    return braille(values, width, start=start, end=end, bucket=bucket)


def braille(values: Sequence[Sample], width: int, *,
            start: float, end: float, bucket: int | None = None) -> str:
    """Two buckets per cell, filled upward from the floor; missing cells use the dotted no-data marker."""
    low, high = limits(values, start, end)
    scaled = _scale(_buckets(values, width * 2, start, end, bucket), low, high)
    heights = [None if v is None else ceil(v * 3) for v in scaled]
    for i in range(0, len(heights), 2):
        if heights[i] is not None or heights[i + 1] is not None:
            heights[i] = heights[i] if heights[i] is not None else 0
            heights[i + 1] = heights[i + 1] if heights[i + 1] is not None else 0
    bits = ((1, 2, 4, 64), (8, 16, 32, 128))
    out = []
    for column in range(width):
        mask = 0
        for side in range(2):
            height = heights[column * 2 + side]
            for dot in range(4):
                if height is not None and 3 - dot <= height:
                    mask |= bits[side][dot]
        out.append(chr(ord(BRAILLE) + mask) if mask else NO_DATA)
    return "".join(out)
