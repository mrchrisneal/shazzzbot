from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from shazzzbot import tasktimes


@pytest.mark.parametrize("value", [None, [], "bad", {"": {"at": 1, "seconds": 2}}, {1: {"at": 1, "seconds": 2}},
                                   {"x": []}, {"x": {"at": True, "seconds": 2}},
                                   {"x": {"at": -1, "seconds": 2}}, {"x": {"at": float("nan"), "seconds": 2}},
                                   {"x": {"at": float("inf"), "seconds": 2}},
                                   {"x": {"at": 10**30, "seconds": 2}}, {"x": {"at": 1}},
                                   {"x": {"at": 1, "seconds": "2"}}, {"x": {"at": 1, "seconds": -2}}])
def test_restore_rejects_invalid_or_unbounded_records(value):
    assert tasktimes.restore(value) == {}


def test_merge_keeps_last_known_completion_and_ignores_future_and_stale_data():
    saved = tasktimes.restore({"x": {"at": 1, "seconds": 45, "private": "secret"}})
    assert saved == {"x": {"at": 1.0, "seconds": 45.0}}
    assert not tasktimes.merge(saved, None, 100)
    assert not tasktimes.merge(saved, {"x": {"at": 1, "seconds": 999}, "future": {"at": 101, "seconds": 9}}, 100)
    assert tasktimes.merge(saved, {"x": {"at": 2, "seconds": 60}, "y": {"at": 3, "seconds": 0}}, 100)
    assert saved == {"x": {"at": 2.0, "seconds": 60.0}, "y": {"at": 3.0, "seconds": 0.0}}
    assert tasktimes.merge(saved, {"x": {"at": 4, "seconds": None}}, 100)
    assert tasktimes.restore(saved)["x"] == {"at": 4.0, "seconds": None}
    assert not tasktimes.merge(saved, {"x": {"at": 2, "seconds": 60}}, 100)
    assert not tasktimes.merge(saved, {"x": {"at": 4, "seconds": None}}, 100)
    assert tasktimes.merge(saved, {"x": {"at": 4, "seconds": 12}}, 100)
    assert not tasktimes.merge(saved, {"x": {"at": 4, "seconds": 13}}, 100)


@pytest.mark.parametrize("seconds,text", [(0, "0s"), (45, "45s"), (59.9, "59s"), (60, "1m"), (720, "12m"),
                                         (3600, "1h"), (3900, "1h05m"), (90000, "25h"),
                                         (172799, "47h59m"), (172800, "2d"), (176400, "2d 1h"),
                                         (258 * 3600 + 19 * 60, "10d 18h"),
                                         (2368 * 3600 + 54 * 60, "98d 16h"), (98 * 86400, "98d")])
def test_elapsed_is_compact(seconds, text):
    assert tasktimes.elapsed(seconds) == text


def test_user_script_keeps_last_completed_run_while_a_new_run_is_active():
    text = ("Script Starting Nov 09, 2026  12:00.00\nprivate output\nScript Finished Nov 09, 2026  12:00.45\n"
            "Script Starting Nov 09, 2026  13:00.00\n")
    result = tasktimes.user_script(text, UTC)
    assert result == {"at": datetime(2026, 11, 9, 12, 0, 45, tzinfo=UTC).timestamp(), "seconds": 45.0}
    assert "private" not in str(result)
    assert tasktimes.user_script(text, None) is None
    assert tasktimes.user_script(text + "Script Finished bad\n", UTC) is None


@pytest.mark.parametrize("text", ["", "Script Finished Nov 09, 2026  12:00.45",
                                  "Script Starting bad\nScript Finished Nov 09, 2026  12:00.45",
                                  "Script Starting Nov 09, 2026  12:00.00\nScript Finished bad",
                                  "Script Starting Nov 09, 2026  13:00.00\nScript Finished Nov 09, 2026  12:00.00",
                                  "Script Starting Nov 01, 2026  01:30.00\nScript Finished Nov 01, 2026  02:30.00",
                                  "Script Starting Mar 08, 2026  02:30.00\nScript Finished Mar 08, 2026  03:30.00"])
def test_user_script_missing_invalid_reversed_and_ambiguous_times_are_unknown(text):
    assert tasktimes.user_script(text, ZoneInfo("America/Los_Angeles")) is None


def test_user_script_measures_across_dst_using_the_host_zone():
    text = "Script Starting Nov 01, 2026  00:30.00\nScript Finished Nov 01, 2026  02:30.00"
    assert tasktimes.user_script(text, ZoneInfo("America/Los_Angeles"))["seconds"] == 10800


def test_appdata_uses_main_backup_start_and_done_not_container_messages():
    text = ("[09.11.2026 12:00:00][info][Main] WELCOME TO APPDATA.BACKUP!! :D\n"
            "[09.11.2026 12:00:02][info][db] Done!\nsecret configuration\n"
            "[09.11.2026 13:05:00][info][Main] DONE! Thanks for using this plugin and have a safe day ;)\n")
    result = tasktimes.appdata_backup(text, UTC)
    assert result == {"at": datetime(2026, 11, 9, 13, 5, tzinfo=UTC).timestamp(), "seconds": 3900.0}
    assert "secret" not in str(result)
    unknown = {"at": 123.0, "seconds": None}
    assert tasktimes.appdata_backup(text, None, 123.0) == unknown
    text += "[bad][info][Main] DONE! Thanks for using this plugin and have a safe day ;)\n"
    assert tasktimes.appdata_backup(text, UTC, 123.0) == unknown


@pytest.mark.parametrize("start,end", [(None, "09.11.2026 13:00:00"), ("bad", "09.11.2026 13:00:00"),
                                     ("09.11.2026 12:00:00", "bad"),
                                     ("09.11.2026 13:00:00", "09.11.2026 12:00:00"),
                                     ("09.11.2026 12:00:00", None)])
def test_appdata_needs_a_valid_pair(start, end):
    text = "[09.11.2026 11:00:00][info][Main] Not a completion\n"
    if start is not None:
        text += f"[{start}][info][Main] WELCOME TO APPDATA.BACKUP!! :D\n"
    if end is not None:
        text += f"[{end}][info][Main] DONE! Thanks for using this plugin and have a safe day ;)\n"
    expected = None if end is None else {"at": 123.0, "seconds": None}
    assert tasktimes.appdata_backup(text, UTC, 123.0) == expected


@pytest.mark.parametrize("fields,seconds", [("3900|100 MB/s|0|0|check P", 3900),
                                          ("3900|100 MB/s|0|0|check Q|7200|2|scheduled check", 7200),
                                          ("3900|100 MB/s|0|0|Parity-Check||1|scheduled check", 3900),
                                          ("53055|150816382|0|0|check P|7814026532", 53055)])
def test_parity_uses_logged_elapsed_seconds_including_increment_history(fields, seconds):
    assert tasktimes.parity_history("Nov 09 12:00:00|" + fields, 100) == {"at": 100, "seconds": seconds}


@pytest.mark.parametrize("fields", ["", "3900|100 MB/s|-4|0|check P", "3900|100 MB/s|0|0|recon P",
                                    "3900|100 MB/s|0|0"])
def test_parity_ignores_cancelled_other_actions_unknown_formats_and_bad_durations(fields):
    assert tasktimes.parity_history("Nov 09 12:00:00|" + fields, 100) is None


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
def test_parity_invalid_last_duration_does_not_reuse_an_older_valid_run(value):
    text = f"Nov 09 12:00:00|3900|100 MB/s|0|0|check P\nNov 10 12:00:00|{value}|100 MB/s|0|0|check P\n"
    assert tasktimes.parity_history(text, 100) == {"at": 100, "seconds": None}
