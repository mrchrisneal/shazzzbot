import pytest

from shazzzbot.glyphs import BRAILLE, NO_DATA
from shazzzbot.spark import _buckets, braille, bucket_layout, limits, spark


@pytest.mark.parametrize("window,wide,narrow", [
    (300, (10, 15), (30, 5)),
    (1800, (60, 15), (120, 8)),
    (21600, (600, 18), (1800, 6)),
    (86400, (1800, 24), (7200, 6)),
    (604800, (14400, 21), (43200, 7)),
])
def test_bucket_layout_picks_smallest_nice_interval(window, wide, narrow):
    assert bucket_layout(window, 28) == wide
    assert bucket_layout(window, 8) == narrow


def test_fixed_buckets_keep_peaks_and_pad_empty_time_on_left():
    samples = [(1, 2), (19, 8), (20, 3), (21, 7), (80, 4), (101, 99)]
    assert _buckets(samples, 6, 0, 100, 20) == [None, 8, 7, None, None, 4]
    assert spark([(80, 4)], 3, start=0, end=100, bucket=20) == NO_DATA * 2 + chr(0x28c0)
    assert spark([(20, 0), (80, 4)], 3, start=0, end=100, bucket=20) == NO_DATA + chr(0x28c0) + chr(0x28f8)


def test_empty_flat_single_sample_and_width_larger_than_the_sample_count():
    assert ord(BRAILLE) == 0x2800 and ord(NO_DATA) == 0x2840
    assert spark([], 5, start=0, end=10) == NO_DATA * 5
    assert braille([], 5, start=0, end=10) == NO_DATA * 5
    for value in (0, 0.000001, 2.5, 100):
        assert spark([(0, value), (5, value), (10, value)], 1, start=0, end=10) == chr(0x28c0)
        assert spark([(5, value)], 5, start=0, end=10) == NO_DATA * 2 + chr(0x28c0) + NO_DATA * 2
    assert spark([(5, 10)], 1, start=0, end=10) == chr(0x28c0)
    assert spark([(5, 5)], 1, start=0, end=10) == chr(0x28c0)


def test_each_series_uses_its_own_raw_range_even_when_bucket_max_hides_the_minimum():
    samples = [(-1, 99), (0, 1), (0.1, 8), (0.2, 2), (2, 3), (3, 999)]
    assert limits(samples, 0, 2) == (1, 8)
    assert spark(samples, 1, start=0, end=2) == chr(0x28e7)
    assert spark([(0, -9), (1, 999)], 1, start=0, end=1) == chr(0x28f8)
    invalid = [(0, float("nan")), (1, float("inf"))]
    assert spark(invalid, 1, start=0, end=1) == NO_DATA and limits(invalid, 0, 1) == (0, 0)


def test_rising_and_spiky_series_have_exact_filled_levels_and_both_floor_dots():
    rising = [(i, i // 2) for i in range(8)]
    assert spark(rising, 4, start=0, end=7) == "".join(chr(v) for v in (0x28c0, 0x28e4, 0x28f6, 0x28ff))
    spiky = [(i, 10 if i == 3 else 0) for i in range(8)]
    assert spark(spiky, 4, start=0, end=7) == chr(0x28c0) + chr(0x28f8) + chr(0x28c0) * 2
    assert spark([(0, 0), (1, 1e-9), (2, 1)], 2, start=0, end=3) == chr(0x28e0) + chr(0x28c7)
    for values in (rising, spiky, [(1, 1)], [(0, 1), (7, 2)]):
        trend = spark(values, 4, start=0, end=7)
        assert " " not in trend
        assert all(ch == NO_DATA or (ord(ch) - 0x2800) & 0xc0 == 0xc0 for ch in trend)


def test_missing_buckets_are_blank_at_both_ends_and_inside_without_inventing_upper_dots():
    samples = [(20, 0), (70, 100)]
    expected = NO_DATA + chr(0x28c0) + NO_DATA + chr(0x28f8) + NO_DATA
    assert spark(samples, 5, start=0, end=100) == expected
    assert braille(samples, 5, start=0, end=100) == expected
    assert spark([(0, 100), (3, 0)], 2, start=0, end=3) == chr(0x28c7) + chr(0x28c0)
    partial = spark([(90, 100), (95, 100), (100, 100)], 15, start=0, end=100)
    assert partial == NO_DATA * 13 + chr(0x28c0) * 2


def test_data_cells_keep_both_floor_dots_and_only_missing_cells_use_the_no_data_marker():
    samples = [(i, 100) for i in range(8)]
    assert braille(samples, 4, start=0, end=7) == chr(0x28c0) * 4
    samples = [(i, i // 2) for i in (0, 1, 4, 5)]
    assert braille(samples, 4, start=0, end=7) == chr(0x28c0) + NO_DATA + chr(0x28ff) + NO_DATA


@pytest.mark.parametrize("width,start,end", [(0, 0, 1), (-1, 0, 1), (1, 1, 1), (1, 2, 1)])
def test_invalid_dimensions_raise(width, start, end):
    with pytest.raises(ValueError, match="positive"):
        spark([], width, start=start, end=end)
    with pytest.raises(ValueError, match="positive"):
        braille([], width, start=start, end=end)
