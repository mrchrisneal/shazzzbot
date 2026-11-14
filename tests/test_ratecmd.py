import asyncio
import math
from datetime import UTC
from unittest.mock import MagicMock

from shazzzbot import ratecmd as R
from tests.test_bot import OPER, VIEW_ROLE, VIEWER, cmd_bot, ctx

ROUTE = "PATCH /channels/{id}/messages/{id}/channels/1234"


def row(**kw):
    base = {"class": "media", "target_rate": 0.1, "header_limit": 5, "window": 5.0, "buffer": 1, "remaining": 3,
            "429_count": 2, "last_429": 0.0, "scope": "shared", "divergent": False}
    return {**base, **kw}


def test_rate_formats():
    assert [R.rate(v) for v in (None, 0, -1, math.inf, 0.1, 2.5)] == ["none"] * 4 + ["1 per 10s", "2.50/s"]


def test_where_names_the_major_resource():
    assert R.where(ROUTE) == "PATCH /channels/{id}/messages/{id} in <#1234>"
    assert R.where("POST /guilds/{id}/x/guilds/9") == "POST /guilds/{id}/x in guild"
    assert R.where("GET /gateway") == "GET /gateway"


def test_line_shows_every_field_when_present():
    text = R.line(ROUTE + "|media|shared", row(last_429=3600.0, divergent=True), UTC)
    assert text.startswith("**media** `PATCH /channels/{id}/messages/{id} in <#1234>`\n")
    assert "target 1 per 10s, limit 5 per 5s, buffer 1, 3 left, 2 x 429, last 1:00 AM" in text
    assert text.endswith("scope shared, media and text differ")


def test_line_omits_missing_fields():
    text = R.line("GET /x", {"target_rate": None, "header_limit": 0, "buffer": None, "429_count": 0}, None)
    assert text == "**?** `GET /x`\ntarget none, 0 x 429"


def test_report_empty_and_sorted_by_429s():
    assert R.report({}, None).startswith("No rate data yet.")
    text = R.report({"GET /a": row(**{"429_count": 0}), "GET /b": row(**{"429_count": 9})}, None)
    assert text.index("GET /b") < text.index("GET /a")
    assert text.startswith("Discord rate budget")


def test_report_worst_case_fits_one_message():
    long_route = "PATCH /" + "x" * 300 + "/channels/" + "9" * 20
    summary = {f"{long_route}{i}|media|shared": row(divergent=True, last_429=1.0, target_rate=1e-9,
                                                     header_limit=10**6, window=1e6, remaining=10**6)
               for i in range(40)}
    text = R.report(summary, UTC)
    assert len(text) <= 2000
    assert text.endswith("more.")


def test_command_allows_viewer_and_hides_mentions(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    b.rate_budget = MagicMock(summary=MagicMock(return_value={}))
    for who in (ctx(VIEWER, [VIEW_ROLE]), ctx(OPER)):
        asyncio.run(cmds["ratelimits"].callback(who))
        args, kwargs = who.respond.await_args
        assert args[0].startswith("No rate data yet.")
        assert kwargs["ephemeral"] is True
        assert kwargs["allowed_mentions"].to_dict()["parse"] == []
