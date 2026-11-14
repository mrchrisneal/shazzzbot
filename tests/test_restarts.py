import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from shazzzbot import bot as B
from shazzzbot import restarts as R
from shazzzbot.docker import Container
from tests.test_bot import OPER, STRANGER, VIEWER, cmd_bot, ctx, wire

WEB = Container("c1", "web", "running", "Up", project="apps")
DB = Container("c2", "db", "running", "Up", project="apps")
OTHER = Container("c3", "other", "running", "Up", project="apps-other")


@pytest.fixture
def batch(tmp_path, monkeypatch):
    bot, commands = cmd_bot(tmp_path)
    bot.cfg.restart_allow = ["web*", "db"]
    bot.cfg.restart_groups = {"apps": ["web*", "db", "web*"]}
    bot.containers = [WEB, DB, OTHER, Container("c4", "plain", "running", "Up")]
    bot.docker.list = AsyncMock(return_value=list(bot.containers))
    bot.docker.restart = AsyncMock()
    monkeypatch.setattr(R, "confirm", AsyncMock(return_value=True))
    return bot, commands


@pytest.mark.parametrize("command", ["docker restart-stack", "docker restart-group"])
def test_selected_containers_restart_once_in_sequence_with_audit(batch, command):
    bot, commands = batch
    bot.containers.append(WEB)
    caller = ctx(OPER)
    active = 0
    events = []
    async def restart(cid):
        nonlocal active
        active += 1
        assert active == 1
        events.append(cid)
        await asyncio.sleep(0)
        active -= 1
    bot.docker.restart.side_effect = restart
    asyncio.run(commands[command].callback(caller, "apps"))
    R.confirm.assert_awaited_once_with(caller, ["web", "db"])
    bot.docker.list.assert_awaited_once()
    assert events == ["c1", "c2"]
    caller.edit.assert_awaited_once()
    assert caller.edit.await_args.kwargs["content"] == "Restarted: `web`, `db`\nFailed: none"
    audits = [call.args[1] for call in bot.audit.await_args_list]
    assert audits == [f"starting {command.removeprefix('docker ')} apps (2 containers)",
                      "restarted `web`", "restarted `db`",
                      f"ended {command.removeprefix('docker ')} apps (2 containers): 2 restarted, 0 failed"]
    assert all(call.args[0] is caller.author for call in bot.audit.await_args_list)
    assert caller.edit.await_args.kwargs["allowed_mentions"].to_dict()["parse"] == []


@pytest.mark.parametrize("user,guild", [(STRANGER, 10), (VIEWER, 10), (OPER, 666)])
@pytest.mark.parametrize("command", ["docker restart-stack", "docker restart-group"])
def test_batch_restarts_require_operate_in_the_configured_guild(batch, command, user, guild):
    bot, commands = batch
    caller = ctx(user, guild=guild)
    asyncio.run(commands[command].callback(caller, "apps"))
    caller.respond.assert_awaited_once_with("Not allowed.", ephemeral=True)
    R.confirm.assert_not_awaited()
    bot.docker.list.assert_not_awaited()
    bot.docker.restart.assert_not_awaited()
    bot.audit.assert_not_awaited()


@pytest.mark.parametrize("command", ["docker restart-stack", "docker restart-group"])
def test_any_disallowed_member_refuses_the_whole_selection(batch, command):
    bot, commands = batch
    bot.cfg.restart_allow = ["web*"]
    caller = ctx(OPER)
    asyncio.run(commands[command].callback(caller, "apps"))
    assert caller.respond.await_args.args[0] == "Not in restarts.allow: `db`. Nothing changed."
    mentions = caller.respond.await_args.kwargs["allowed_mentions"]
    assert not mentions.users and not mentions.roles and not mentions.everyone
    R.confirm.assert_not_awaited()
    bot.docker.list.assert_not_awaited()
    bot.docker.restart.assert_not_awaited()
    bot.audit.assert_not_awaited()


@pytest.mark.parametrize("command", ["docker restart-stack", "docker restart-group"])
@pytest.mark.parametrize("name", ["missing", ""])
def test_empty_matches_do_not_change_anything(batch, command, name):
    bot, commands = batch
    caller = ctx(OPER)
    asyncio.run(commands[command].callback(caller, name))
    assert caller.respond.await_args.args[0] == "No containers match."
    R.confirm.assert_not_awaited()
    bot.docker.list.assert_not_awaited()
    bot.docker.restart.assert_not_awaited()
    bot.audit.assert_not_awaited()


@pytest.mark.parametrize("configured", [False, True])
def test_batch_requires_docker_and_an_audit_channel(batch, configured):
    bot, commands = batch
    docker = bot.docker
    if configured:
        wire(bot)
    else:
        bot.docker = None
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-stack"].callback(caller, "apps"))
    assert caller.respond.await_args.args[0] == (B.NO_AUDIT if configured else "Docker is not configured.")
    R.confirm.assert_not_awaited()
    docker.list.assert_not_awaited()
    docker.restart.assert_not_awaited()
    bot.audit.assert_not_awaited()


def test_cancelled_confirmation_does_not_restart_or_audit(batch):
    bot, commands = batch
    R.confirm.return_value = False
    asyncio.run(commands["docker restart-stack"].callback(ctx(OPER), "apps"))
    bot.docker.list.assert_not_awaited()
    bot.docker.restart.assert_not_awaited()
    bot.audit.assert_not_awaited()


@pytest.mark.parametrize("fresh,expected", [
    ([WEB], ["c1"]), ([WEB, Container("new", "db", "running", "Up", project="apps")], ["c1"]),
    ([WEB, Container("c2", "renamed", "running", "Up", project="apps")], ["c1"]),
    ([WEB, Container("c2", "db", "running", "Up", project="elsewhere")], ["c1"]),
    ([WEB, DB, Container("new", "new-app", "running", "Up", project="apps")], ["c1", "c2"])])
def test_confirmation_binds_ids_names_and_stack_membership(batch, fresh, expected):
    bot, commands = batch
    bot.docker.list.return_value = fresh
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-stack"].callback(caller, "apps"))
    assert [call.args[0] for call in bot.docker.restart.await_args_list] == expected
    failed = "none" if len(expected) == 2 else "`db`"
    assert caller.edit.await_args.kwargs["content"].endswith("Failed: " + failed)


def test_confirmation_keeps_a_copy_of_mutable_cached_targets(batch):
    bot, commands = batch
    bot.containers[1] = Container("c2", "db", "running", "Up", project="apps")
    bot.docker.list.return_value = bot.containers
    async def confirmed(*args):
        bot.containers[1].name = "forbidden"
        return True
    R.confirm.side_effect = confirmed
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-stack"].callback(caller, "apps"))
    bot.docker.restart.assert_awaited_once_with("c1")
    assert caller.edit.await_args.kwargs["content"] == "Restarted: `web`\nFailed: `db`"


@pytest.mark.parametrize("error", [aiohttp.ClientError("unavailable"), TimeoutError()])
def test_fresh_list_failure_starts_no_containers_and_audits_failures(batch, error):
    bot, commands = batch
    bot.docker.list.side_effect = error
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    bot.docker.restart.assert_not_awaited()
    assert caller.edit.await_args.kwargs["content"] == "Restarted: none\nFailed: `web`, `db`"
    assert len(bot.audit.await_args_list) == 4
    assert "no longer present" in bot.audit.await_args_list[1].args[1]


@pytest.mark.parametrize("error", [aiohttp.ClientError("refused"), TimeoutError()])
def test_failed_restart_is_audited_and_the_next_container_is_attempted(batch, error):
    bot, commands = batch
    bot.docker.restart.side_effect = [error, None]
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    assert [call.args[0] for call in bot.docker.restart.await_args_list] == ["c1", "c2"]
    assert caller.edit.await_args.kwargs["content"] == "Restarted: `db`\nFailed: `web`"
    assert bot.audit.await_args_list[1].args[1] == "restart of `web` FAILED"


def test_failed_start_audit_refuses_all_actions(batch):
    bot, commands = batch
    bot.audit.return_value = False
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    bot.docker.list.assert_not_awaited()
    bot.docker.restart.assert_not_awaited()
    assert caller.edit.await_args.kwargs["content"] == B.NO_AUDIT


@pytest.mark.parametrize("audits,restarted", [([True, False, True], ["c1"]),
                                           ([True, True, True, False], ["c1", "c2"])])
def test_failed_result_audit_stops_the_sequence_and_failed_end_audit_is_reported(batch, audits, restarted):
    bot, commands = batch
    bot.audit.side_effect = audits
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    assert [call.args[0] for call in bot.docker.restart.await_args_list] == restarted
    assert "Audit logging failed" in caller.edit.await_args.kwargs["content"]


def test_allowlist_is_checked_again_after_confirmation(batch):
    bot, commands = batch
    async def confirmed(*args):
        bot.cfg.restart_allow = ["web"]
        return True
    R.confirm.side_effect = confirmed
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    bot.docker.restart.assert_not_awaited()
    assert "Not in restarts.allow: `db`. Nothing changed." in caller.edit.await_args.kwargs["content"]


def test_batch_timeout_reports_unconfirmed_names(batch, monkeypatch):
    bot, commands = batch
    monkeypatch.setattr(R, "BATCH_TIMEOUT", 0)
    caller = ctx(OPER)
    asyncio.run(commands["docker restart-group"].callback(caller, "apps"))
    bot.docker.restart.assert_not_awaited()
    assert "Failed: `web`, `db`" in caller.edit.await_args.kwargs["content"]
    assert "timed out" in caller.edit.await_args.kwargs["content"]
    assert bot.audit.await_args_list[-1].args[1].startswith("ended restart-group")


def test_autocomplete_uses_projects_and_configured_group_names(batch):
    bot, commands = batch
    bot.containers += [Container(str(i), str(i), "running", "Up", project=f"project{i}") for i in range(30)]
    bot.containers.append(Container("long", "long", "running", "Up", project="x" * 101))
    bot.cfg.restart_groups.update({f"group{i}": ["web"] for i in range(30)})
    bot.cfg.restart_groups["x" * 101] = ["web"]
    stacks = commands["docker restart-stack"].options[0].autocomplete
    groups = commands["docker restart-group"].options[0].autocomplete
    assert asyncio.run(stacks(SimpleNamespace(value="APPS"))) == ["apps", "apps-other"]
    assert asyncio.run(groups(SimpleNamespace(value="APPS"))) == ["apps"]
    for autocomplete in (stacks, groups):
        names = asyncio.run(autocomplete(SimpleNamespace(value=None)))
        assert len(names) == 25 and len(set(names)) == 25 and all(0 < len(name) <= 100 for name in names)


@pytest.mark.parametrize("timed_out,approved", [(False, True), (False, False), (True, None)])
def test_confirmation_is_one_view_with_names_and_no_mentions(monkeypatch, timed_out, approved):
    view = SimpleNamespace(ok=approved, wait=AsyncMock(return_value=timed_out))
    make_view = Mock(return_value=view)
    monkeypatch.setattr(B, "Confirm", make_view)
    caller = ctx(OPER)
    assert asyncio.run(R.confirm(caller, ["web", "db"])) is bool(approved)
    make_view.assert_called_once_with(OPER)
    caller.respond.assert_awaited_once()
    assert caller.respond.await_args.args[0] == "Restart these containers?\n`web`, `db`"
    kwargs = caller.respond.await_args.kwargs
    assert kwargs["view"] is view and kwargs["ephemeral"]
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []
    assert caller.edit.await_count == int(timed_out)


@pytest.mark.parametrize("edit", [False, True])
def test_long_messages_attach_all_details_without_clipping(edit):
    caller = ctx(OPER)
    text = "container\n" * 500
    asyncio.run(R.reply(caller, text, edit=edit))
    caller.respond.assert_awaited_once()
    options = caller.respond.await_args.kwargs
    assert options["ephemeral"] and options["allowed_mentions"].to_dict()["parse"] == []
    attached = options["file"]
    assert attached.fp.read() == text.encode() and attached.filename == "restart-details.txt"
    attached.close()
    assert caller.edit.await_count == int(edit)


def test_container_labels_do_not_allow_markdown_injection():
    assert R.labels(["a`b"]) == "`a'b`"
