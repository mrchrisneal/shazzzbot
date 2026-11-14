import asyncio
import hashlib
import hmac
import io
import json
import os
import signal
import struct
import time
import urllib.request
import zipapp
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from shazzzbot.helper import auth, constants, updates
from shazzzbot.helper import files as storage
from shazzzbot.helper.platforms import tasks, unraid

FLASH_CHECK = unraid.ensure_flash_safe

def directory_mount_id(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        text = Path(f"/proc/{os.getpid()}/fdinfo/{fd}").read_text()
        return next(int(line.partition(":")[2]) for line in text.splitlines() if line.startswith("mnt_id:"))
    finally:
        os.close(fd)


def archive_bytes(extra=(), version="0.2.0", *, source=None, main=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("VERSION", version + "\n")
        archive.writestr("__main__.py", main if main is not None else
                         'raise RuntimeError("never execute during validation")\n')
        archive.writestr("helper/constants.py", source if source is not None else "pass\n")
        for name, value in extra:
            archive.writestr(name, value)
    output = io.BytesIO()
    stream.seek(0)
    zipapp.create_archive(stream, output, interpreter="/usr/bin/env python3")
    return output.getvalue()


def signed(helper, **changes):
    obj = {"action": "hello", "args": {}, "ts": time.time(), "nonce": "n" * 16, "protocol": constants.PROTOCOL}
    obj.update(changes)
    obj["sig"] = hmac.new(helper.key, auth.canonical(obj), hashlib.sha256).hexdigest()
    return auth.canonical(obj) + b"\n"


def writer(uid=10001):
    sock = SimpleNamespace(getsockopt=Mock(return_value=struct.pack("3i", 123, uid, 10001)))
    return SimpleNamespace(get_extra_info=lambda key: sock, write=Mock(), drain=AsyncMock(), close=Mock(),
                           wait_closed=AsyncMock())


def update_fixture(helper, monkeypatch):
    root = Path(helper.cfg["socket"]).parent
    helper.cfg.update(update_url="https://example.invalid/helper", update_version="0.2.0")
    storage.write_private(root / "helper.pid", auth.canonical({"pid": 42, "start": "100",
                                                                    "path": str(helper.path)}))
    monkeypatch.setattr(storage, "process_start", lambda pid: "100")
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(signal, "pidfd_send_signal", Mock())
    data = archive_bytes()
    response, stream = SimpleNamespace(geturl=lambda: helper.cfg["update_url"], read=lambda n: data), Mock()
    stream.__enter__ = Mock(return_value=response)
    stream.__exit__ = Mock(return_value=False)
    opener = Mock(return_value=SimpleNamespace(open=Mock(return_value=stream)))
    monkeypatch.setattr(urllib.request, "build_opener", opener)
    digest = hashlib.sha256(data).hexdigest()
    helper.cfg["update_sha256"] = digest
    monkeypatch.setattr(updates, "wait_update", Mock())
    return data, digest, response


def signal_callbacks(loop, monkeypatch):
    callbacks = {}
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb, *args: callbacks.update({sig: (cb, args)}))
    monkeypatch.setattr(loop, "remove_signal_handler", Mock(return_value=True))
    return callbacks


def mock_samba_process(monkeypatch):
    async def spawn(*argv, **kwargs):
        return SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"[global]\n", b""])),
                               wait=AsyncMock(return_value=0), pid=99999999)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", Mock())


MONITOR = """[smart]
disk1.199="3"
disk1.ack="true"
disk2.5="8"
disk2.187="1"
cache2.199="2"
[used]
disk1="98"
"""

CRON = """# Generated mover schedule:
0 3 * * * /usr/local/sbin/mover &> /dev/null
@reboot /should/be/skipped
#
0 * * * * run-parts /etc/cron.hourly
30 4 * * 0 run-parts /etc/cron.weekly
not a cron line
*/5 * * * * /usr/local/emhttp/plugins/user.scripts/startCustom.php \
/boot/config/plugins/user.scripts/scripts/gp-mirrors/script > /dev/null
15 2 * * * /usr/bin/fstrim -a
"""


def tree(root, update):
    for sub, name, text in (("cron.d", "root", CRON), ("emhttp", "monitor.ini", MONITOR),
                            ("unraidcheck", "result.json", json.dumps(update)),
                            ("user.scripts", "schedule.json", json.dumps({
                                "a": {"frequency": "weekly",
                                      "script": "/boot/x/user.scripts/scripts/xfs_defrag/script"},
                                "b": {"frequency": "hourly", "id": "schedulenightly"},
                                "c": {"frequency": "custom", "script": "ignored"}})),
                            ("user.scripts.state/running", "gp-mirrors", ""),
                            ("user.scripts.state/finished", "old", "")):
        os.makedirs(root / sub, exist_ok=True)
        (root / sub / name).write_text(text)



def source_paths(root):
    return {name: root / name for name in tasks.SOURCES}
