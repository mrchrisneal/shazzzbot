import asyncio
import os
import signal
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot.helper import process as execution
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import FLASH_CHECK


def test_owned_subprocess_defaults_stay_in_the_helper_work_directory(helper, monkeypatch):
    child = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
                            wait=AsyncMock(return_value=0), pid=999999)
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(execution.execute(["/usr/bin/true"], 1, 4096)) == (0, [], False)
    env = spawn.await_args.kwargs["env"]
    assert env["HOME"] == env["TMPDIR"] == helper.env["TMPDIR"]
    assert env["HOME"] != "/root" and spawn.await_args.kwargs["pass_fds"] == ()


def test_action_validation_and_busy_targets(helper):
    helper.cfg["enabled"] = ["vm", "parity"]
    for action, args in (("update", {}), ("reboot", {}), ("array.stop", {}), ("vm.bad", {}),
                         ("script.run", {"name": "x"}), ("parity.check", {"name": "array"}), ("vm.start", {})):
        with pytest.raises(ValueError):
            asyncio.run(helper.dispatch(action, args, AsyncMock()))
    helper.busy.add("vm:guest")
    emit = AsyncMock()
    asyncio.run(helper.dispatch("vm.start", {"name": "guest"}, emit))
    emit.assert_awaited_once_with({"error": "busy"})


def test_commands_use_live_inventories_and_fixed_argv(helper, monkeypatch, tmp_path):
    helper.query = AsyncMock(return_value=["guest"])
    assert asyncio.run(helper.resolve("container", "restart", "guest")) == [unraid.BIN["container"], "restart", "guest"]
    helper.query.return_value = []
    for family in ("vm", "container"):
        with pytest.raises(ValueError):
            asyncio.run(helper.resolve(family, "start", "gone"))
    project = tmp_path / "Dev.Stack-name"
    project.mkdir()
    monkeypatch.setattr(unraid, "PROJECTS", tmp_path)
    with pytest.raises(ValueError, match="compose"):
        asyncio.run(helper.resolve("stack", "up", "dev_stack_name"))
    compose = project / "compose.yaml"
    compose.touch()
    argv = asyncio.run(helper.resolve("stack", "up", "dev_stack_name"))
    assert argv == [unraid.BIN["stack"], "compose", "--project-directory", str(project), "-p", "dev_stack_name",
                    "-f", str(compose), "up", "-d"]
    assert asyncio.run(helper.resolve("stack", "pull", "dev_stack_name"))[-1] == "pull"
    (tmp_path / "dev-stack-name").mkdir()
    for name in ("missing", "dev_stack_name"):
        with pytest.raises(ValueError, match="ambiguous"):
            asyncio.run(helper.resolve("stack", "up", name))
    parity = {"check": ("check", "CORRECT"), "nocorrect": ("check", "NOCORRECT"),
              "pause": ("nocheck", "PAUSE"), "resume": ("check", "RESUME"), "cancel": ("nocheck",)}
    for op, tail in parity.items():
        assert asyncio.run(helper.resolve("parity", op, "array")) == [unraid.BIN["parity"], *tail]
    disks = tmp_path / "disks.ini"
    disks.write_text('["disk1"]\ndevice="sda"\n')
    monkeypatch.setattr(unraid, "DISKS", disks)
    assert asyncio.run(helper.resolve("smart", "long", "sda")) == [unraid.BIN["smart"], "-t", "long", "/dev/sda"]
    for name in ("missing", "../sda"):
        with pytest.raises(ValueError):
            asyncio.run(helper.resolve("smart", "short", name))


def test_user_and_config_scripts_are_named_and_allowlisted(helper, monkeypatch, tmp_path):
    monkeypatch.setattr(unraid, "USER_SCRIPTS", tmp_path)
    script = tmp_path / "backup" / "script"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o700)
    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(helper.resolve("userscript", "run", "backup"))
    helper.cfg["userscript_allow"] = ["backup*"]
    assert asyncio.run(helper.resolve("userscript", "run", "backup")) == ["/bin/bash", str(script)]
    with pytest.raises(FileNotFoundError):
        asyncio.run(helper.resolve("userscript", "run", "backup-missing"))
    script.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted"):
        asyncio.run(helper.resolve("userscript", "run", "backup"))
    script.unlink()
    script.symlink_to(tmp_path / "other")
    with pytest.raises(ValueError, match="untrusted"):
        asyncio.run(helper.resolve("userscript", "run", "backup"))
    with pytest.raises(ValueError, match="unknown"):
        asyncio.run(helper.resolve("script", "run", "named"))
    helper.cfg["scripts"] = {"named": ["/usr/bin/true"]}
    helper.cfg["script_binaries"] = ["/usr/bin/true"]
    assert asyncio.run(helper.resolve("script", "run", "named")) == ["/usr/bin/true"]
    script.unlink()
    script.write_text("exit 0\n")
    script.chmod(0o600)
    monkeypatch.setattr(execution, "execute", AsyncMock(return_value=(0, [], False)))
    helper.cfg.update(enabled=["userscript"])
    asyncio.run(helper.dispatch("userscript.run", {"name": "backup"}, AsyncMock()))
    assert execution.execute.await_args.args[0] == ["/bin/bash", str(script)]
    with pytest.raises(ValueError, match="forbidden"):
        asyncio.run(helper.run(["/bin/bash", str(script)], AsyncMock()))


def test_dispatch_audits_only_script_name_and_releases_busy_on_failure(helper, capsys):
    helper.cfg["enabled"] = ["script", "parity"]
    helper.resolve = AsyncMock(return_value=["/usr/bin/example", "PRIVATE_ARG"])
    helper.run = AsyncMock(return_value=7)
    emit = AsyncMock()
    asyncio.run(helper.dispatch("script.run", {"name": "named"}, emit))
    assert (emit.await_args.args[0]["done"] == 7 and not helper.busy) and ("PRIVATE_ARG" not in capsys.readouterr().out)
    helper.run.side_effect = OSError()
    asyncio.run(helper.dispatch("parity.cancel", {}, emit))
    assert emit.await_args.args[0]["done"] == 127 and not helper.busy


def test_streaming_runner_caps_output_times_out_and_kills_group(helper, monkeypatch):
    process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"one\npart", b"ial\n", b""])),
                              wait=AsyncMock(return_value=3), pid=99999999)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    kill = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(os, "killpg", kill)
    emit = AsyncMock()
    assert asyncio.run(helper.run(["/usr/bin/example"], emit)) == 3
    assert [c.args[0] for c in emit.await_args_list] == [{"out": "one"}, {"out": "partial"}]
    assert spawn.await_args.args == ("/usr/bin/example",)
    assert spawn.await_args.kwargs["env"]["HOME"] == helper.env["TMPDIR"]
    assert spawn.await_args.kwargs["env"]["TMPDIR"].endswith("shazzzbot-helper")
    assert spawn.await_args.kwargs["pass_fds"] == () and spawn.await_args.kwargs["start_new_session"]
    kill.assert_not_called()
    process.stdout.read = AsyncMock(side_effect=[b"x" * 1024, b"more", b""])
    helper.cfg["output_bytes"] = 1
    emit.reset_mock()
    assert asyncio.run(helper.run(["/usr/bin/example"], emit)) == 3
    assert [c.args[0] for c in emit.await_args_list] == [{"out": "x"}, {"truncated": True}]
    assert process.stdout.read.await_count == 3
    kill.assert_not_called()
    process.stdout.read = AsyncMock(side_effect=AsyncTimeoutError)
    assert asyncio.run(helper.run(["/usr/bin/example"], emit)) == 124
    kill.assert_called_once_with(process.pid, signal.SIGKILL)
    for failure in (OSError, AsyncTimeoutError):
        process.stdout.read = AsyncMock(side_effect=[b"done\n", b""])
        process.wait.reset_mock()
        async def disconnected(obj, failure=failure):
            assert process.wait.await_count == 2
            raise failure()
        assert asyncio.run(helper.run(["/usr/bin/example"], disconnected)) == 3
        assert b"output delivery failed" in b"".join(helper.audit.lines)
    for argv in (["relative"], ["/sbin/reboot"]):
        with pytest.raises(ValueError):
            asyncio.run(helper.run(argv, emit))


def test_live_queries_fail_closed(helper):
    async def good(argv, emit):
        await emit({"out": "a"})
        await emit({"out": "b"})
        return 0
    helper.run = good
    assert asyncio.run(helper.query(["/usr/bin/example"])) == ["a", "b"]
    helper.run = AsyncMock(return_value=1)
    with pytest.raises(ValueError, match="inventory"):
        asyncio.run(helper.query(["/usr/bin/example"]))


@pytest.mark.parametrize("name", ["guest", "123", "+5", " 5"])
def test_vm_operation_mapping_always_uses_guest_uuid(helper, name):
    uuid = "01234567-0123-4567-89ab-0123456789ab"
    mapping = {"start": "start", "shutdown": "shutdown", "restart": "reboot", "pause": "suspend",
               "resume": "resume", "force-stop": "destroy"}
    for operation, verb in mapping.items():
        helper.query = AsyncMock(side_effect=[[name], ["", "11234567-0123-4567-89ab-0123456789ab", uuid],
                                                  ["other"], [name]])
        assert asyncio.run(helper.resolve("vm", operation, name)) == ["/usr/bin/virsh", verb, uuid]
        assert helper.query.await_args_list[-1].args[0] == ["/usr/bin/virsh", "domname", uuid]


def test_cancelling_an_action_kills_and_reaps_its_process_group(helper, monkeypatch):
    async def go():
        ready, never = asyncio.Event(), asyncio.Event()
        async def read(size):
            ready.set()
            await never.wait()
        process = SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock(return_value=0), pid=99999999)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        kill = Mock()
        monkeypatch.setattr(os, "killpg", kill)
        task = asyncio.create_task(helper.run(["/usr/bin/example"], AsyncMock()))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        process.wait.assert_awaited_once_with()
    asyncio.run(go())


def test_a_second_action_cannot_race_the_same_target(helper):
    helper.cfg["enabled"] = ["vm"]
    helper.resolve = AsyncMock(return_value=["/usr/bin/virsh", "start", "guest"])
    async def go():
        ready, release = asyncio.Event(), asyncio.Event()
        async def run(argv, emit):
            ready.set()
            await release.wait()
            return 0
        helper.run = run
        first = asyncio.create_task(helper.dispatch("vm.start", {"name": "guest"}, AsyncMock()))
        await ready.wait()
        emit = AsyncMock()
        await helper.dispatch("vm.shutdown", {"name": "guest"}, emit)
        emit.assert_awaited_once_with({"error": "busy"})
        helper.resolve.assert_awaited_once()
        release.set()
        await first
        assert not helper.busy
    asyncio.run(go())


def test_changed_flash_permissions_refuse_actions_and_release_target(helper, monkeypatch):
    helper.cfg["enabled"] = ["parity"]
    monkeypatch.setattr(unraid, "ensure_flash_safe", AsyncMock(side_effect=ValueError("flash SMB")))
    helper.resolve = AsyncMock()
    with pytest.raises(ValueError, match="flash SMB"):
        asyncio.run(helper.dispatch("parity.check", {}, AsyncMock()))
    helper.resolve.assert_not_awaited()
    assert not helper.busy


@pytest.mark.parametrize("names", [["123", "123"], ["other", "other"], ["bad-uuid"]])
def test_ambiguous_missing_or_invalid_vm_uuids_are_refused(helper, names):
    uuids = ["01234567-0123-4567-89ab-0123456789ab", "11234567-0123-4567-89ab-0123456789ab"]
    helper.query = AsyncMock(side_effect=[["123"], names] if names == ["bad-uuid"]
                             else [["123"], uuids, [names[0]], [names[1]]])
    with pytest.raises(ValueError, match="UUID|changed"):
        asyncio.run(helper.resolve("vm", "restart", "123"))


def test_truncated_inventories_and_samba_config_fail_closed(helper, monkeypatch):
    monkeypatch.setattr(execution, "execute", AsyncMock(return_value=(0, ["guest"], True)))
    with pytest.raises(ValueError, match="truncated"):
        asyncio.run(helper.query(["/usr/bin/example"]))
    with pytest.raises(ValueError, match="flash SMB"):
        asyncio.run(FLASH_CHECK())
