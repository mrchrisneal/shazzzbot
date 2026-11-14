import asyncio
import io
from array import array
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from PIL import Image

from shazzzbot import bot as B
from shazzzbot import chartgen as C
from shazzzbot import charts
from shazzzbot.panel import WIDTH
from tests.test_bot import OPER, STRANGER, VIEW_ROLE, VIEWER, channel, cmd_bot, ctx, wire

NOW = 1_700_000_000.0
DAY = 86400


def row(t, value=10.0, cores=4):
    return array("d", (t, value, value, value, value, value, value, *([value] * cores)))


def history(n=60, step=60.0, value=10.0, cores=4):
    return [row(NOW - (n - i) * step, value, cores) for i in range(n)]


def picked(keys, samples, window=3600):
    known = {s.key: s for s in C.available(samples)}
    return [(known[k], C.rows(samples, known[k], window, NOW)) for k in keys]


def test_available_lists_only_series_with_data():
    assert C.available([]) == []
    assert [s.key for s in C.available([row(NOW, cores=1)])] == [s.key for s in C.BASE]
    keys = [s.key for s in C.available(history(cores=3))]
    assert keys[-3:] == ["core-1", "core-2", "core-3"] and len(keys) == 9


def test_autocomplete_filters_and_caps():
    series = C.available(history(cores=64))
    assert [c.value for c in C.matches(series, "DISK")] == ["disk-read", "disk-write"]
    assert [c.value for c in C.matches(series, "core 6")] == ["core-6", "core-60", "core-61", "core-62",
                                                                "core-63", "core-64"]
    assert [c.value for c in C.matches(series, "li")] == ["list"]
    assert C.matches(series, "zzz") == []
    everything = C.matches(series, "")
    assert len(everything) == 25 and everything[-1].value == "list"
    assert C.matches(series, "net")[0].name == "Net down (Mbps)"


def test_options_lists_everything():
    text = C.options(C.available(history(cores=8)), 1800)
    assert "`cpu` CPU (%)" in text and "`core-1` to `core-8`" in text and "Default: 30m." in text
    assert "none yet" in C.options([], 1800) and "core-" not in C.options([], 1800)


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_image_stacks_one_row_per_series(n):
    keys = ["cpu", "ram", "net-up", "core-2"][:n]
    embeds, png = C.build(picked(keys, history()), 3600, NOW, "image", None, 180)
    assert len(embeds) == 1 and embeds[0].image.url == "attachment://chartgen.png"
    im = Image.open(io.BytesIO(png))
    assert im.height == charts.HEAD_H + n * charts.CHART_H and im.width == charts.W


def test_chart_command_uses_the_shared_two_times_line_scale():
    with patch.object(C.Image, "new", wraps=C.Image.new) as allocate:
        png = C.image(picked(["cpu"], history()), 3600, NOW, None, 180)
    height = charts.HEAD_H + charts.CHART_H
    assert charts.SS == C.renderbudget.SCALE == 2
    assert Image.open(io.BytesIO(png)).size == (charts.W, height)
    assert allocate.call_args_list[0].args[:2] == ("RGB", (charts.W, height))
    assert allocate.call_args_list[1].args[:2] == ("RGBA", (charts.W * 2, height * 2))


def test_oversized_chart_command_returns_a_bounded_placeholder_before_allocating_the_card(monkeypatch):
    chosen = picked(["cpu"], history())
    series, samples = chosen[0]
    chosen = [(series._replace(label="W" * 1000), samples)]
    width = charts.chart_width(samples, C.spec(chosen[0][0]), charts._fonts())
    height = charts.HEAD_H + charts.CHART_H
    original = C.Image.new
    allocations = []

    def allocate(mode, size, *args, **kwargs):
        assert size not in {(width, height), (width * charts.SS, height * charts.SS)}
        assert 4 * size[0] * size[1] < C.renderbudget.RENDER_BUDGET
        allocations.append(size)
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(C.Image, "new", allocate)
    with patch.object(C.view, "placeholder", wraps=C.view.placeholder) as placeholder:
        png = C.image(chosen, 3600, NOW, None, 180)
    placeholder.assert_called_once_with(C.KEY, "Charts (1h)", width, NOW, None)
    image = Image.open(io.BytesIO(png))
    assert allocations and image.width <= width and image.height < 1000


@pytest.mark.parametrize("mode", ["rich", "text"])
@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_rich_and_text_give_one_embed_per_series_in_order(mode, n):
    keys = ["disk-write", "cpu", "ram", "net-down"][:n]
    embeds, png = C.build(picked(keys, history()), 3600, NOW, mode, None, 180)
    assert png is None
    assert [e.title.split(",")[0] for e in embeds] == [s.name for s, _ in picked(keys, history())]
    if mode == "rich":
        assert [f.name for f in embeds[0].fields] == ["Now", "Peak", "Low", "Average", "Trend"]
    else:
        assert "Average" in embeds[0].description and "**Last 1h**" in embeds[0].description


def test_empty_span_says_so():
    cpu = C.BASE[0]
    for mode in ("rich", "text"):
        embeds, _ = C.build([(cpu, C.rows(history(), cpu, 300, NOW + 3 * DAY))], 300, NOW + 3 * DAY, mode, None, 180)
        assert "No samples in this span." in embeds[0].description


def test_worst_case_fits_discord_limits():
    samples = history(n=7 * 24 * 4, step=900.0, value=1e15, cores=128)
    keys = ["disk-read", "net-down", "core-128", "core-127"]
    chosen = picked(keys, samples, 7 * DAY)
    fonts = charts._fonts()
    embeds, png = C.build(chosen, 7 * DAY, NOW, "image", None, 2700)
    width = Image.open(io.BytesIO(png)).width
    assert all(width >= charts.chart_width(r, C.spec(s), fonts) for s, r in chosen)
    assert len(png) < 8 * 1024 * 1024
    for mode in ("rich", "text"):
        embeds, _ = C.build(chosen, 7 * DAY, NOW, mode, None, 2700)
        assert len(embeds) <= 10 and sum(len(e) for e in embeds) <= 6000
        assert all(len(e.title) <= 256 and len(e.description or "") <= 4096 for e in embeds)
        assert all(len(f.value) <= 1024 for e in embeds for f in e.fields)
        if mode == "text":
            body = [line for e in embeds for line in e.description.split("\n") if not line.startswith(("```", "**"))]
            assert max(map(len, body)) <= WIDTH + 3
    huge = C.options(C.available(samples), 7 * DAY) + "Unknown series: " + "x" * 200
    assert len(huge) <= 2000


def setup(tmp_path, samples=None):
    b, cmds = cmd_bot(tmp_path)
    b.history.samples.extend(history() if samples is None else samples)
    return b, cmds["chartgen"]


def caller(user=VIEWER, roles=(VIEW_ROLE,)):
    c = ctx(user, list(roles))
    c.author.send = AsyncMock()
    return c


def run(cmd, c, *series, **kw):
    asyncio.run(cmd.callback(c, *series, **kw))


def audit(bot, c, outcome, *, count=1, series=None, span=None, mode=None):
    assert bot.audit.await_count == count
    args, kwargs = bot.audit.await_args
    assert args[0] is c.author and not kwargs
    text = args[1]
    assert text.startswith("/chartgen series=") and text.endswith("outcome=" + outcome)
    assert len(text) < 1900 and len(text.splitlines()) == 1
    for name, value in (("series", series), ("span", span), ("mode", mode)):
        if value is not None:
            assert name + "=" + value in text
    return text


def test_command_denies_strangers_and_other_guilds(tmp_path):
    b, cmd = setup(tmp_path)
    for c in (caller(STRANGER, ()), ctx(OPER, guild=5)):
        c.author.send = AsyncMock()
        run(cmd, c, "cpu")
        assert c.respond.await_args.args[0] == "Not allowed."
        c.author.send.assert_not_awaited()
    b.audit.assert_not_awaited()


def test_command_lists_options_without_spending_the_cooldown(tmp_path):
    b, cmd = setup(tmp_path)
    c = caller()
    for args, start in ((("list",), "Series with data"), (("bogus", "cpu"), "Unknown series: bogus"),
                        (("cpu",), "Use a span"), ((" ",), "Series with data")):
        run(cmd, c, *args, span="9y" if start == "Use a span" else "")
        assert c.respond.await_args.args[0].startswith(start)
        assert c.respond.await_args.kwargs["ephemeral"] is True
        audit(b, c, "options", series=",".join(value for value in args if value.strip()),
              span="9y" if start == "Use a span" else charts.span(b.window), mode="image")
        b.audit.reset_mock()
    run(cmd, c, "cpu", mode="nope")
    assert c.respond.await_args.args[0].startswith("Series with data")
    audit(b, c, "options", mode="nope")
    b.audit.reset_mock()
    run(cmd, c, "cpu")
    assert c.respond.await_args.args[0] == "Sent to your DMs."
    audit(b, c, "sent to DM", series="cpu", span=charts.span(b.window), mode="image")


def test_command_dms_the_chart_and_applies_the_cooldown(tmp_path):
    b, cmd = setup(tmp_path)
    c = caller()
    run(cmd, c, "cpu", "RAM", "cpu", span="6h")
    c.defer.assert_awaited_once_with(ephemeral=True)
    sent = c.author.send.await_args.kwargs
    assert sent["files"][0].filename == "chartgen.png" and len(sent["embeds"]) == 1
    assert c.respond.await_args.args[0] == "Sent to your DMs."
    audit(b, c, "sent to DM", series="cpu,RAM,cpu", span="6h", mode="image")
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    run(cmd, c, "cpu", mode="text")
    assert c.respond.await_args.args[0].startswith("You can request another chart in 60 seconds")
    audit(b, c, "cooldown", count=2, series="cpu", mode="text")
    other = caller(OPER, ())
    run(cmd, other, "net-up", mode="text")
    assert other.author.send.await_args.kwargs["files"] == []
    audit(b, other, "sent to DM", count=3, series="net-up", mode="text")


def test_cooldown_expires_and_old_entries_are_pruned(tmp_path, monkeypatch):
    b, cmd = setup(tmp_path)
    monkeypatch.setattr(C, "COOLDOWN", 0.0)
    a, z = caller(), caller(OPER, ())
    run(cmd, a, "cpu", mode="rich")
    run(cmd, z, "cpu", mode="rich")
    run(cmd, a, "cpu", mode="rich")
    assert a.author.send.await_count == 2
    assert b.audit.await_count == 3
    assert all(call.args[1].endswith("outcome=sent to DM") for call in b.audit.await_args_list)


def test_global_cap_turns_away_a_second_render(tmp_path, monkeypatch):
    b, cmd = setup(tmp_path)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(*_args):
        started.set()
        await gate.wait()
        return "sent to DM"

    monkeypatch.setattr(C, "deliver", slow)

    async def go():
        first, second = caller(), caller(OPER, ())
        task = asyncio.create_task(cmd.callback(first, "cpu", mode="text"))
        await started.wait()
        await cmd.callback(second, "cpu", mode="text")
        assert second.respond.await_args.args[0].startswith("Another chart is rendering")
        second.defer.assert_not_awaited()
        audit(b, second, "busy", mode="text")
        gate.set()
        await task
        audit(b, first, "sent to DM", count=2, mode="text")
    asyncio.run(go())


@pytest.mark.parametrize("error, start, fallback", [
    (discord.Forbidden(MagicMock(status=403), "closed"), "I cannot DM you", True),
    (discord.HTTPException(MagicMock(status=500), "boom"), "That did not work", False),
    (TimeoutError("budget"), "The Discord rate budget is full", False),
])
def test_dm_failures_fall_back_privately(tmp_path, error, start, fallback):
    b, cmd = setup(tmp_path)
    c = caller()
    c.author.send = AsyncMock(side_effect=error)
    run(cmd, c, "cpu", "ram")
    args, kwargs = c.respond.await_args
    assert args[0].startswith(start) and kwargs["ephemeral"] is True
    if fallback:
        assert kwargs["files"][0].filename == "chartgen.png"
        assert kwargs["files"][0] is not c.author.send.await_args.kwargs["files"][0]
        assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    else:
        assert "files" not in kwargs
    audit(b, c, "ephemeral fallback" if fallback else "error", series="cpu,ram", mode="image")


@pytest.mark.parametrize("stage", ["defer", "build", "fallback", "dm"])
def test_unexpected_failures_are_audited_once_without_exception_details(tmp_path, monkeypatch, stage):
    b, cmd = setup(tmp_path)
    c = caller()
    error = RuntimeError("private failure detail")
    if stage == "defer":
        c.defer.side_effect = error
    elif stage == "build":
        monkeypatch.setattr(C, "build", MagicMock(side_effect=error))
    elif stage == "fallback":
        c.author.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
        c.respond.side_effect = error
    else:
        c.author.send.side_effect = error
    with pytest.raises(RuntimeError, match="private failure detail"):
        run(cmd, c, "cpu", span="1h", mode="text")
    text = audit(b, c, "error", series="cpu", span="1h", mode="text")
    assert "private failure detail" not in text


def test_audit_fields_are_bounded_single_line_and_keep_the_outcome(tmp_path):
    b, cmd = setup(tmp_path)
    c = caller()
    values = ["@everyone\n" + str(i) + "x" * 3000 for i in range(4)]
    run(cmd, c, *values, span="1h\n" + "s" * 3000, mode="text\n" + "m" * 3000)
    text = audit(b, c, "options")
    fields = text.removeprefix("/chartgen series=").split(" span=", 1)
    assert len(fields[0]) <= 200 and "@everyone" in fields[0]
    span, rest = fields[1].split(" mode=", 1)
    mode, outcome = rest.split(" outcome=", 1)
    assert len(span) <= 32 and len(mode) <= 16 and outcome == "options"
    assert c.respond.await_args.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


def test_real_audit_helper_posts_the_caller_and_never_pings(tmp_path):
    b, cmd = setup(tmp_path)
    c = caller()
    target = channel()
    wire(b, audit_ch=target)
    b.audit = B.Bot.audit.__get__(b, B.Bot)
    run(cmd, c, "@everyone\nnot-a-series", span="1h", mode="text")
    target.send.assert_awaited_once()
    args, kwargs = target.send.await_args
    assert args[0].startswith(f"<@{VIEWER}> /chartgen series=@everyone not-a-series")
    assert args[0].endswith("outcome=options")
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


def test_default_span_audit_stays_with_the_request_when_the_window_changes_during_delivery(tmp_path):
    b, cmd = setup(tmp_path)
    original = b.window
    c = caller()

    async def send(**_kwargs):
        b.window = 300 if original != 300 else 600
        await asyncio.sleep(0)

    c.author.send.side_effect = send
    run(cmd, c, "cpu", mode="text")
    assert b.window != original
    audit(b, c, "sent to DM", span=charts.span(original), mode="text")


def test_autocomplete_callbacks(tmp_path):
    _, cmd = setup(tmp_path)
    series = next(o for o in cmd.options if o.name == "series1").autocomplete
    spans = next(o for o in cmd.options if o.name == "span").autocomplete
    assert [c.value for c in asyncio.run(series(MagicMock(value="ra")))] == ["ram"]
    assert asyncio.run(series(MagicMock(value=None)))[0].value == "cpu"
    assert asyncio.run(spans(MagicMock(value="1"))) == ["15m", "1h", "12h"]
    assert asyncio.run(spans(MagicMock(value="45m"))) == ["45m"]
    assert asyncio.run(spans(MagicMock(value=""))) == list(charts.SPANS)


def test_chart_commands_share_the_span_autocomplete(tmp_path):
    _, cmds = cmd_bot(tmp_path)
    window = next(o for o in cmds["charts window"].options if o.name == "span").autocomplete
    generated = next(o for o in cmds["chartgen"].options if o.name == "span").autocomplete
    for typed in ("", "1", "45m", None):
        assert asyncio.run(window(MagicMock(value=typed))) == asyncio.run(generated(MagicMock(value=typed)))


@pytest.mark.parametrize("mode", C.MODES)
def test_chartgen_clamps_requested_and_default_windows_to_retention(tmp_path, monkeypatch, mode):
    b, cmd = setup(tmp_path)
    b.cfg.history_days = 1
    b.window = 7 * DAY
    now = NOW
    monkeypatch.setattr(C.time, "time", lambda: now)
    actual = C.build
    windows = []

    def build(picked, window, *args):
        windows.append(window)
        assert all(now - DAY <= row[0] <= now for _, rows in picked for row in rows)
        return actual(picked, window, *args)

    monkeypatch.setattr(C, "build", build)
    for user, requested in ((VIEWER, "7d"), (OPER, "")):
        c = caller(user)
        run(cmd, c, "cpu", span=requested, mode=mode)
        assert "24h" in c.author.send.await_args.kwargs["embeds"][0].title
        audit(b, c, "sent to DM", span="24h", mode=mode, count=len(windows))
    assert windows == [DAY, DAY]
    spans = next(o for o in cmd.options if o.name == "span").autocomplete
    assert asyncio.run(spans(MagicMock(value="7d"))) == ["24h"]
