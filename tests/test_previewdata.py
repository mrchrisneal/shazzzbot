import asyncio
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from shazzzbot import delivery, previewcfg, previewdata
from shazzzbot.boards import Message

ROOT = Path(__file__).resolve().parent.parent


def test_urls_lose_credentials_queries_and_fragments():
    assert previewdata._url("") == ""
    assert previewdata._url("https://username:password@example.com:8443/a/b?token=x#f") == "https://example.com:8443/a/b"
    assert previewdata._url("http://[fd00::1]/x") == "http://[fd00::1]/x"


def test_plain_turns_containers_into_json_lists():
    assert previewdata._plain({"a": {3, 1}, "b": (1, deque([frozenset({2})]))}) == {"a": [1, 3], "b": [1, [[2]]]}


def test_render_returns_only_diagnostics_without_a_snapshot_or_valid_settings():
    out = asyncio.run(previewdata.render("", '[system]\ntitle = "a"\n'))
    assert out == {"diagnostics": [], "frame": None}
    out = asyncio.run(previewdata.render('{"format": 1}', "guild_id = 3\n"))
    assert out["frame"] is None and out["diagnostics"][0]["severity"] == "error"


def test_load_refuses_other_snapshot_versions(tmp_path):
    _, cfg = previewcfg.check("", previewcfg.BASE)
    with pytest.raises(ValueError, match="format"):
        previewdata.load({"format": 0}, cfg, str(tmp_path))
    assert previewdata._array(None) is None


def test_delivery_metadata_contains_only_safe_main_output_facts():
    record = {"last_success": 42, "last_failed": True, "reason": "HTTPException", "token": "private value"}
    assert previewdata._delivery(None) == {}
    cleaned = previewdata._delivery({"system:image": record, "private.example:image": record,
                                     7: record, "tasks:text": [],
                                     "services:embed": {"last_success": True, "last_failed": 1, "reason": 7}})
    assert cleaned == {"system:image": {"last_success": 42.0, "last_failed": True, "reason": "HTTPException"},
                       "services:embed": {"last_success": None, "last_failed": False, "reason": ""}}
    assert "private" not in repr(cleaned) and "token" not in repr(cleaned)
    for stamp in (-1, float("nan"), float("inf"), 10**1000, "42"):
        assert previewdata._delivery({"tasks:text": {"last_success": stamp, "reason": "not a class"}}) == {
            "tasks:text": {"last_success": None, "last_failed": False, "reason": ""}}



def _bot(**kw):
    return SimpleNamespace(image_keys=lambda m: tuple(k for k, v in m.modes.items() if v == "image"), **kw)

@pytest.mark.parametrize("dates,expected", [([], None), ([100], None), ([100, 100], None),
                                          ([200, 100], None), ([float("inf"), 200], None),
                                          ([100, 250], 150)])
def test_image_cycle_comes_from_the_real_two_slot_plan_not_the_countdown(monkeypatch, dates, expected):
    message = Message(2, modes={"system": "image"}, image_every=60, edit_every=20)
    monkeypatch.setattr(delivery, "message_schedule", lambda *_args: {delivery.identity(message): dates})
    timing = previewdata._timing(_bot(), message, "system", "system.png", 50, None, {}, 100)
    assert timing == {"last_success": None, "last_failed": False, "reason": "", "nominal": 60,
                      "planned_gap": expected, "sent": True, "kind": "image", "next": 100}


def test_current_planned_gap_uses_confirmed_success_and_virtual_views_are_not_sent(monkeypatch):
    message = Message(2, modes={"tasks": "text"}, edit_every=60)
    seen = {"tasks:text": {"last_success": 25, "last_failed": True, "reason": "TimeoutError"}}
    real = previewdata._timing(_bot(), message, "tasks", None, 50, None, seen, 100)
    assert real == {"last_success": 25, "last_failed": True, "reason": "TimeoutError", "nominal": 60,
                    "planned_gap": 75, "sent": True, "kind": "text", "next": 100}
    never = previewdata._timing(_bot(), message, "tasks", None, 50, None, {}, 100)
    assert never["last_success"] is None and never["planned_gap"] is None
    virtual = previewdata._timing(_bot(), message, "tasks", None, 50, "text", seen, 100)
    assert virtual["last_success"] is None and not virtual["last_failed"] and not virtual["sent"]
    blocked = previewdata._timing(_bot(), message, "tasks", None, 50, None, seen, float("inf"))
    assert blocked["planned_gap"] is None
    after_clock_change = previewdata._timing(_bot(), message, "tasks", None, 50, None, seen, 20)
    assert after_clock_change["planned_gap"] == 0


def test_main_schedule_is_sanitized_without_output_identifiers():
    assert previewdata._scheduled(None) is None
    assert previewdata._scheduled([]) == {}
    assert previewdata._scheduled({7: {}, "bad:kind": {}, "tasks:text": [],
                                   "services:embed": {"nominal": 0}}) == {}
    assert previewdata._scheduled({"tasks:text": {"next": 160, "nominal": 60, "image_gap": float("inf"),
                                                   "channel_id": 123}}) == {
        "tasks:text": {"next": 160.0, "nominal": 60.0, "image_gap": None}}


@pytest.mark.parametrize("dates", [[], [180], [180, 300]])
def test_dump_schedule_uses_actual_main_message_plan(monkeypatch, dates):
    message = Message(2, modes={"system": "image", "tasks": "text"}, image_every=120, edit_every=60,
                      embed_keys=["system", "tasks"])
    bot = SimpleNamespace(main=SimpleNamespace(messages=[message]))
    monkeypatch.setattr(delivery, "edit_boundary", lambda *_args: 160)
    calls = []

    def actual_schedule(source, now):
        calls.append((source, now))
        return {delivery.identity(message): dates}

    monkeypatch.setattr(delivery, "message_schedule", actual_schedule)
    assert previewdata._schedule(bot, 100) == {
        "system:image": {"next": 180.0 if dates else None, "nominal": 120.0,
                         "image_gap": 120.0 if len(dates) == 2 else None},
        "tasks:text": {"next": 160.0, "nominal": 60.0, "image_gap": None}}
    assert calls == [(bot, 100)]


def test_actual_output_timing_uses_captured_schedule_instead_of_virtual_footer():
    message = Message(2, modes={"tasks": "text"}, edit_every=30)
    bot = _bot(_preview_schedule={"tasks:text": {"next": 160, "nominal": 60, "image_gap": None}})
    seen = {"tasks:text": {"last_success": 25}}
    actual = previewdata._timing(bot, message, "tasks", None, 50, None, seen, 100)
    assert actual["next"] == 160 and actual["nominal"] == 60 and actual["planned_gap"] == 135
    virtual = previewdata._timing(bot, message, "tasks", None, 50, "text", seen, 100)
    assert virtual["next"] == 100 and virtual["nominal"] == 30 and virtual["planned_gap"] is None
    bot._preview_schedule = {}
    unknown = previewdata._timing(bot, message, "tasks", None, 50, None, seen, 100)
    assert unknown["next"] is None and unknown["planned_gap"] is None


def test_observed_delivery_survives_snapshot_dump_load_and_reaches_only_the_actual_view(tmp_path, monkeypatch):
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text()
    snap["delivery"] = {
        "tasks:embed": {"last_success": snap["now"] - 60, "last_failed": True, "reason": "HTTPException"},
        "system:image": {"last_success": snap["now"] - 75, "last_failed": False, "reason": ""},
    }
    snap["schedule"] = {"tasks:embed": {"next": snap["now"] + 180, "nominal": 120, "image_gap": None},
                        "system:image": {"next": snap["now"] + 300, "nominal": 600, "image_gap": 600}}
    _, cfg = previewcfg.check(toml, previewcfg.BASE)
    monkeypatch.setattr(previewdata.unraid, "read", lambda _path: previewdata._array(snap["array"]))
    with previewdata.at(previewdata.Clock(snap["now"]), snap["cores"]):
        bot = previewdata.load(snap, cfg, str(tmp_path))
        try:
            dumped = asyncio.run(previewdata.dump(bot))
        finally:
            bot.history.close()
            bot.app_history.close()
    assert dumped["delivery"] == snap["delivery"]
    output = asyncio.run(previewdata.render(json.dumps(snap), toml, images=False))
    actual = {item["key"]: item["timing"] for message in output["frame"]["default"] for item in message}
    assert actual["tasks"]["last_success"] == snap["now"] - 60 and actual["tasks"]["last_failed"]
    assert actual["tasks"]["next"] == snap["now"] + 180 and actual["tasks"]["planned_gap"] == 240
    assert actual["tasks"]["nominal"] == 120
    assert actual["system"]["last_success"] == snap["now"] - 75 and actual["system"]["kind"] == "image"
    assert actual["system"]["next"] == snap["now"] + 300 and actual["system"]["planned_gap"] == 375
    assert all(timing["sent"] for timing in actual.values())
    edited = toml.replace('tasks = "embed"', 'tasks = "text"')
    output = asyncio.run(previewdata.render(json.dumps(snap), edited, images=False))
    redone = {item["key"]: item["timing"] for message in output["frame"]["default"] for item in message}
    assert redone["tasks"]["kind"] == "text" and redone["tasks"]["next"] != snap["now"] + 180
    assert redone["system"]["next"] != snap["now"] + 300, "a render edit drops the captured live schedule"
    output = asyncio.run(previewdata.render(json.dumps(snap), toml, images=False))
    for mode in ("text", "rich", "image"):
        assert all(item["timing"]["last_success"] is None and not item["timing"]["sent"]
                   for message in output["frame"][mode] for item in message)


def test_each_embed_carries_its_footer_time_and_the_plan_uses_the_real_scheduler():
    from shazzzbot import view
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text()
    out = asyncio.run(previewdata.render(json.dumps(snap), toml, images=False))
    tz = ZoneInfo(previewcfg.check(toml, previewcfg.BASE)[1].timezone)
    items = [item for mode in out["frame"].values() for msg in mode for item in msg if "footer" in item["embed"]]
    assert items and all("at" in item for item in items)
    for item in items:
        assert view.footer(item["at"], tz).split(" (")[1] in item["embed"]["footer"]["text"]
    assert set(out["timing"]["messages"]) == set(out["frame"]) and out["timing"]["learned"] is False
    first = out["timing"]["messages"]["default"][0]
    assert (first["edit_at"] is None) == any(e["kind"] == "image" for e in first["embeds"]) and first["embeds"]
    assert first["edit_at"] is not None or first["images"][0] > snap["now"]
    assert all(set(e) == {"key", "kind", "title"} and e["title"] for e in first["embeds"])


def test_the_cadence_follows_the_render_mode_and_learned_limits_hide_channel_ids():
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text()

    def messages(render, edited=toml):
        data = {**snap, "render": render}
        return asyncio.run(previewdata.render(json.dumps(data), edited, images=False))["timing"]["messages"]
    plans = messages({})
    assert all(not m["images"] for m in plans["text"]) and any(m["images"] for m in plans["image"])
    kinds = {mode: {e["kind"] for m in plans[mode] for e in m["embeds"]} for mode in plans}
    assert kinds["text"] == {"text"} and kinds["rich"] == {"embed"} and kinds["image"] == {"image"}
    assert kinds["default"] == {"image", "embed"}
    edited = toml.replace('tasks = "embed"', 'tasks = "text"')
    assert "embed" not in {e["kind"] for m in messages({}, edited)["default"] for e in m["embeds"]}
    assert "embed" in {e["kind"] for m in messages({"tasks": "embed"}, edited)["default"] for e in m["embeds"]}


def test_the_budget_dump_replaces_every_channel_id():
    edit = "PATCH /channels/{id}/messages/{id}/channels/"
    raw = {"format": 1, "routes": {edit + "123456789012345678": "b1",
                                    "POST /channels/{id}/messages/channels/987654321098765432": "b2"},
           "buckets": {"b1": {}}, "evidence": {edit + "123456789012345678|media|user": {}}}
    bot = SimpleNamespace(rate_budget=SimpleNamespace(dump=lambda: raw),
                          main=SimpleNamespace(channel_id=123456789012345678))
    out = previewdata._budget(bot)
    assert "123456789012345678" not in json.dumps(out) and "987654321098765432" not in json.dumps(out)
    assert set(out["routes"]) == {edit + "2", "POST /channels/{id}/messages/channels/3"}
    assert previewdata._budget(SimpleNamespace(rate_budget=SimpleNamespace(dump=lambda: None))) is None


def test_an_edit_before_the_first_upload_shows_outgoing_placeholder_and_the_plan_names_the_upload():
    from shazzzbot import view
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text()
    out = asyncio.run(previewdata.render(json.dumps(snap), toml, images=False))
    tz = ZoneInfo(previewcfg.check(toml, previewcfg.BASE)[1].timezone)
    shown = out["frame"]["default"]
    for msg, planned in zip(shown, out["timing"]["messages"]["default"], strict=True):
        named = [item for item in msg if "name" in item]
        for item in named:
            assert item["held"]["description"].startswith("Image pending.") and "image" not in item["held"]
            assert item["held"]["footer"]["text"] == view.footer(item["at"], tz)
        if named and planned["images"]:
            assert min(item["at"] for item in named) == planned["images"][0]
    assert any("name" in item for msg in shown for item in msg)


def test_history_is_its_own_image_and_every_image_embed_gets_its_png(monkeypatch):
    monkeypatch.setattr(previewdata.B, "EMBED_LIMIT", 600)
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text().replace('history = "image"', 'history = "text"')
    toml = toml.replace('"history", "disks", "services", "docker", "apps", "tasks", "forges"]',
                        '"disks", "services", "docker", "apps", "tasks", "forges", "history"]')
    out = asyncio.run(previewdata.render(json.dumps(snap), toml))["frame"]

    def png(mode, name):
        return next(item.get("png") for msg in out[mode] for item in msg if item.get("name") == name)
    assert png("default", "system.png") == png("image", "system.png")
    assert png("image", "history.png")
    assert any(item.get("name") == "history.png" for msg in out["image"][1:] for item in msg)
    assert all("png" in item for msg in out["image"] for item in msg if item.get("name"))


def test_a_packet_footer_follows_its_own_message_clock(monkeypatch):
    from shazzzbot import view
    snap = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"][0]
    toml = (ROOT / "docs" / "demo" / "demo.toml").read_text()
    for key in ("disks", "services", "docker", "apps", "forges"):
        toml = toml.replace(f'{key} = "image"', f'{key} = "embed"')
    monkeypatch.setattr(previewdata.B, "EMBED_LIMIT", 600)
    out = asyncio.run(previewdata.render(json.dumps(snap), toml, images=False))
    tz = ZoneInfo(previewcfg.check(toml, previewcfg.BASE)[1].timezone)
    mixed = out["frame"]["default"]
    kinds = [{item["timing"]["kind"] == "image" for item in msg} for msg in mixed]
    assert {True} in kinds and {False} in kinds
    for msg in mixed:
        for item in msg:
            assert item["embed"]["footer"]["text"] == view.footer(item["at"], tz)
