import sqlite3
from contextlib import closing

import pytest

from shazzzbot.helper import formats


@pytest.mark.parametrize("value", [None, [], {}, {"format": None}, {"format": True}, {"format": "1"},
                                   {"format": 0}, {"format": -1}, {"format": 2**31}])
def test_json_header_requires_an_object_and_positive_integer(value):
    with pytest.raises(formats.FormatError):
        formats.load(value, "test document", 1)


@pytest.mark.parametrize("current", [True, "1", 0, -1, 2**31])
def test_current_format_is_validated(current):
    with pytest.raises(formats.FormatError):
        formats.load({"format": 1}, "test document", current)
    with pytest.raises(formats.FormatError):
        formats.dump({}, current)
    with closing(sqlite3.connect(":memory:")) as db, pytest.raises(formats.FormatError):
        formats.sqlite(db, "test database", current)


@pytest.mark.parametrize("steps", [[], {True: lambda value: value}, {0: lambda value: value}, {1: None}])
def test_invalid_step_tables_refuse(steps):
    with pytest.raises(formats.FormatError, match="invalid format steps"):
        formats.load({"format": 1}, "test document", 1, steps)


def test_json_load_and_dump_copy_the_envelope_and_preserve_release_version():
    source = {"format": 1, "version": "v1.0.0", "content": "unchanged"}
    restored = formats.load(source, "test document", 1, {})
    assert restored == source and restored is not source
    assert formats.dump(source, 2) == {**source, "format": 2}
    assert source["format"] == 1


def test_newer_or_unsupported_json_refuses_without_modifying_input():
    source = {"format": 3, "content": "unchanged"}
    with pytest.raises(formats.FormatError, match="written by a newer Shazzzbot"):
        formats.load(source, "test document", 2)
    with pytest.raises(formats.FormatError, match="no upgrade step"):
        formats.load({"format": 1}, "test document", 2)
    assert source == {"format": 3, "content": "unchanged"}


def test_json_steps_advance_one_format_at_a_time():
    seen = []

    def step(value):
        seen.append(value["format"])
        return formats.dump({**value, "converted": True}, value["format"] + 1)

    assert formats.load({"format": 1}, "test document", 3, {1: step, 2: step}) == {
        "format": 3, "converted": True}
    assert seen == [1, 2]


@pytest.mark.parametrize("result", [None, [], {}, {"format": True}, {"format": "2"}, {"format": 1},
                                    {"format": 3}])
def test_json_step_must_return_exact_next_header(result):
    with pytest.raises(formats.FormatError, match="advance exactly once"):
        formats.load({"format": 1}, "test document", 2, {1: lambda _value: result})


def test_sqlite_stamps_only_a_new_empty_database_and_keeps_current():
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        formats.sqlite(db, "test database", 1)
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        db.execute("CREATE TABLE saved (value)")
        formats.sqlite(db, "test database", 1)
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("saved",)]
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("CREATE TABLE saved (value)")
        with pytest.raises(formats.FormatError, match="existing database"):
            formats.sqlite(db, "test database", 1)
        assert db.execute("PRAGMA user_version").fetchone() == (0,)


def test_sqlite_refuses_newer_or_missing_steps():
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("PRAGMA user_version=3")
        with pytest.raises(formats.FormatError, match="written by a newer Shazzzbot"):
            formats.sqlite(db, "test database", 1)
        db.execute("PRAGMA user_version=1")
        with pytest.raises(formats.FormatError, match="no upgrade step"):
            formats.sqlite(db, "test database", 2)
        assert db.execute("PRAGMA user_version").fetchone() == (1,)


def test_sqlite_steps_commit_together():
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("PRAGMA user_version=1")

        def step(connection):
            connection.execute("CREATE TABLE saved (value)")
            connection.execute("PRAGMA user_version=2")

        formats.sqlite(db, "test database", 2, {1: step})
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("saved",)]


@pytest.mark.parametrize("failure", ["wrong-header", "exception"])
def test_sqlite_failed_step_rolls_back_every_change(failure):
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("PRAGMA user_version=1")

        def step(connection):
            connection.execute("CREATE TABLE saved (value)")
            if failure == "exception":
                raise ValueError("step failed")
            connection.execute("PRAGMA user_version=3")

        with pytest.raises(ValueError):
            formats.sqlite(db, "test database", 2, {1: step})
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
