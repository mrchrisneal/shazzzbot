import io
import json
import runpy
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools import setup_probe as P

KEYS = {"host_ip", "timezone", "containers", "netdata", "jellyfin", "ups"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        bodies = {"/api/v1/alarms": b'{"alarms": {}}', "/System/Info/Public": b'{"Id": "x"}',
                  "/bad": b"not json", "/big": b"1" * 70000}
        if self.path == "/slow":
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(20):
                time.sleep(0.1)
                self.wfile.write(b"X-Drip: 1\r\n")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/api/v1/alarms")
            self.end_headers()
            return
        body = bodies.get(self.path, b"{}")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def web():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def nut_server(reply):
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(64)
            conn.sendall(reply)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()


def recorder(log, result):
    def fake(arg, *_rest):
        log.append(arg)
        return result

    return fake


def write_ini(tmp_path, monkeypatch, name, text):
    monkeypatch.setattr(P, "EMHTTP", tmp_path)
    (tmp_path / name).write_bytes(text if isinstance(text, bytes) else text.encode())


def test_parse_ini():
    text = '[eth0]\nIPADDR:0="1.2.3.4"\nIPADDR:0="5.6.7.8"\n; c=1\n# d=2\nnoequals\n=x\nplain = v \nq="\n'
    assert P.parse_ini(text) == {"IPADDR:0": "1.2.3.4", "plain": "v", "q": '"'}


def test_read_ini(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "EMHTTP", tmp_path)
    assert P.read_ini("missing.ini") == {}
    write_ini(tmp_path, monkeypatch, "bad.ini", b"a=\xff\n")
    assert P.read_ini("bad.ini") == {}
    write_ini(tmp_path, monkeypatch, "ok.ini", 'timeZone="UTC"\n')
    assert P.read_ini("ok.ini") == {"timeZone": "UTC"}
    monkeypatch.setattr(P, "MAX_RUN", 3)
    assert P.read_ini("ok.ini") == {}


def test_run(monkeypatch):
    py = sys.executable
    assert P.run([py, "-c", "print('hi')"]) == "hi\n"
    assert P.run([py, "-c", "raise SystemExit(1)"]) is None
    assert P.run([py, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"]) is None
    assert P.run(["/nonexistent/binary"]) is None
    monkeypatch.setattr(P, "MAX_RUN", 2)
    assert P.run([py, "-c", "print('long')"]) is None
    monkeypatch.setattr(P, "TIMEOUT", 0.2)
    assert P.run([py, "-c", "import time; time.sleep(5)"]) is None


def test_drain(monkeypatch):
    def reader(*chunks):
        items = iter(chunks)
        return lambda _n: next(items, b"")

    assert P.drain(reader(b"ab", b"c")) == b"abc"
    assert P.drain(reader(b"ab"), b"END") is None
    assert P.drain(reader(b"a END", b"more"), b"END") == b"a END"
    assert P.drain(reader(b"x" * (P.MAX_READ + 1))) is None
    monkeypatch.setattr(P, "TIMEOUT", 0)
    assert P.drain(reader(b"ab")) is None


def test_http_json(web, monkeypatch):
    base = f"http://127.0.0.1:{web}"
    assert P.http_json(base + "/api/v1/alarms") == {"alarms": {}}
    assert P.http_json(base + "/bad") is None
    assert P.http_json(base + "/big") is None
    assert P.http_json(base + "/redirect") is None
    monkeypatch.setattr(P, "TIMEOUT", 0.3)
    start = time.monotonic()
    assert P.http_json(base + "/slow") is None
    assert time.monotonic() - start < 1
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert P.http_json(f"http://127.0.0.1:{port}/") is None


def test_http_ignores_proxy_env(web, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    assert P.http_json(f"http://127.0.0.1:{web}/api/v1/alarms") == {"alarms": {}}


def test_valid_ip_and_route():
    assert P.valid_ip(None) is None
    assert P.valid_ip("garbage") is None
    assert P.valid_ip("127.0.0.1") is None
    assert P.valid_ip("0.0.0.0") is None
    assert P.valid_ip(" 192.0.2.5 ") == "192.0.2.5"
    assert P.parse_route(None) is None
    assert P.parse_route("1.1.1.1 via 192.0.2.1 dev eth0") is None
    assert P.parse_route("1.1.1.1 dev eth0 src") is None
    assert P.parse_route("1.1.1.1 via 192.0.2.1 dev br0 src 192.0.2.5 uid 0") == "192.0.2.5"


def test_host_ip(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, "network.ini", '[eth0]\nIPADDR:0="192.0.2.5"\n')
    monkeypatch.setattr(P, "run", lambda _argv: pytest.fail("fallback used"))
    assert P.host_ip() == "192.0.2.5"
    write_ini(tmp_path, monkeypatch, "network.ini", 'IPADDR:0="127.0.0.1"\n')
    calls: list[object] = []
    monkeypatch.setattr(P, "run", recorder(calls, "1.1.1.1 dev eth0 src 198.51.100.2\n"))
    assert P.host_ip() == "198.51.100.2"
    assert calls == [["ip", "-4", "route", "get", "1.1.1.1"]]


def test_valid_tz():
    assert P.valid_tz(None) is None
    assert P.valid_tz("../etc/passwd") is None
    assert P.valid_tz("A" * 65) is None
    assert P.valid_tz("Not/AZone") is None
    assert P.valid_tz("UTC") == "UTC"


def test_timezone(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, "var.ini", 'timeZone="America/Los_Angeles"\n')
    assert P.timezone() == "America/Los_Angeles"
    write_ini(tmp_path, monkeypatch, "var.ini", 'timeZone="bogus zone"\n')
    link = tmp_path / "localtime"
    monkeypatch.setattr(P, "LOCALTIME", link)
    assert P.timezone() is None
    link.symlink_to("../usr/share/zoneinfo/Europe/Berlin")
    assert P.timezone() == "Europe/Berlin"
    link.unlink()
    link.symlink_to("/somewhere/else")
    assert P.timezone() is None


def test_parse_ports():
    text = "0.0.0.0:18096->8096/tcp, :::18096->8096/tcp, 0.0.0.0:1900->1900/udp, 8920/tcp, 1.2.3.4:81->80/tcp"
    assert P.parse_ports(text) == {8096: 18096, 80: 81}


@pytest.mark.parametrize("template,ip,ports,network,want", [
    ("http://[IP]:[PORT:8096]/", "192.0.2.5", {8096: 18096}, "bridge", "http://192.0.2.5:18096/"),
    ("https://[IP]:[PORT:8443]/web", "198.51.100.2", {}, "host", "https://198.51.100.2:8443/web"),
    ("http://[IP]:[PORT:8096]/", "192.0.2.5", {}, "bridge", ""),
    ("http://[IP]:8080/", None, {}, "bridge", ""),
    ("http://user:pw@[IP]:8080/", "198.51.100.2", {}, "bridge", ""),
    ("http://[IP]:8080/?token=x", "198.51.100.2", {}, "bridge", ""),
    ("ftp://[IP]:21/", "198.51.100.2", {}, "bridge", ""),
    ("", "198.51.100.2", {}, "bridge", ""),
    ("http://[IP]:99999/", "198.51.100.2", {}, "bridge", ""),
    ("http://[IP]:0/", "198.51.100.2", {}, "bridge", ""),
    ("http:///path", "198.51.100.2", {}, "bridge", ""),
    ("http://[IP]/a b", "198.51.100.2", {}, "bridge", ""),
    ("http://[IP]/\n", "198.51.100.2", {}, "bridge", ""),
    ("http://example.lan/", "198.51.100.2", {}, "br0,host", "http://example.lan/"),
])
def test_resolve_webui(template, ip, ports, network, want):
    assert P.resolve_webui(template, ip, ports, network) == want


def test_parse_containers(monkeypatch):
    good = {"name": "jellyfin", "image": "lscr.io/linuxserver/jellyfin:latest", "ports": "0.0.0.0:18096->8096/tcp",
            "networks": "bridge", "webui": "http://[IP]:[PORT:8096]/", "env": "SECRET"}
    lines = ["not json", "[1]", json.dumps({"name": "", "image": "x"}), json.dumps({"name": "a"}),
             json.dumps({"name": "b", "image": "img", "ports": 5, "networks": None, "webui": ["x"]}), json.dumps(good)]
    rows = P.parse_containers("\n".join(lines), "192.0.2.5")
    assert rows == [{"name": "b", "image": "img", "webui": ""},
                    {"name": "jellyfin", "image": good["image"], "webui": "http://192.0.2.5:18096/"}]
    monkeypatch.setattr(P, "MAX_CONTAINERS", 1)
    assert len(P.parse_containers("\n".join([json.dumps(good)] * 3), None)) == 1


def test_containers(monkeypatch):
    monkeypatch.setattr(P, "run", lambda _argv: None)
    assert P.containers("198.51.100.2") is None
    seen: list[object] = []
    monkeypatch.setattr(P, "run", recorder(seen, json.dumps({"name": "n", "image": "i"})))
    assert P.containers(None) == [{"name": "n", "image": "i", "webui": ""}]
    assert seen == [["docker", "ps", "--format", P.DOCKER_FORMAT]]


def test_docker_ps_failure_and_timeout(monkeypatch):
    monkeypatch.setattr(P, "run", lambda _argv: None)
    assert P.containers("198.51.100.2") is None


def test_netdata(web, monkeypatch):
    assert P.netdata(None) is None
    monkeypatch.setattr(P, "NETDATA_PORT", web)
    assert P.netdata("192.0.2.5") == f"http://192.0.2.5:{web}"
    monkeypatch.setattr(P, "http_json", lambda _url: {"other": 1})
    assert P.netdata("192.0.2.5") is None
    monkeypatch.setattr(P, "http_json", lambda _url: [])
    assert P.netdata("192.0.2.5") is None


def test_jellyfin(web, monkeypatch):
    other = {"name": "x", "image": "nextcloud", "webui": ""}
    assert P.jellyfin([other], "198.51.100.2") is None
    row = {"name": "jf", "image": "JellyFin/JellyFin", "webui": f"http://127.0.0.1:{web}/web/index.html"}
    assert P.jellyfin([other, row], "198.51.100.2") == f"http://127.0.0.1:{web}"
    assert P.jellyfin([{**row, "webui": ""}], None) is None
    urls: list[object] = []
    monkeypatch.setattr(P, "http_json", recorder(urls, {"Id": "1"}))
    assert P.jellyfin([{**row, "webui": ""}], "198.51.100.2") == "http://198.51.100.2:8096"
    assert urls == ["http://198.51.100.2:8096/System/Info/Public"]
    monkeypatch.setattr(P, "http_json", lambda _url: {"Name": "x"})
    assert P.jellyfin([row], "198.51.100.2") is None


def test_parse_ups():
    text = 'BEGIN LIST UPS\nUPS ups "APC"\nUPS ups "dup"\nUPS bad!name "x"\nUPS back-1.a "y"\nEND LIST UPS\n'
    assert P.parse_ups(text) == ["ups", "back-1.a"]
    assert P.parse_ups("UPS " + "a" * 65 + ' "x"') == []


def test_ups(monkeypatch):
    assert P.ups(None) is None
    monkeypatch.setattr(P, "NUT", nut_server(b'BEGIN LIST UPS\nUPS ups "APC"\nEND LIST UPS\n'))
    assert P.ups("192.0.2.5") == ["ups@192.0.2.5:3493"]
    monkeypatch.setattr(P, "NUT", nut_server(b"BEGIN LIST UPS\nEND LIST UPS\n"))
    assert P.ups("192.0.2.5") is None


def test_nut_list(monkeypatch):
    monkeypatch.setattr(P, "NUT", nut_server(b"BEGIN LIST UPS\n"))
    assert P.nut_list() is None
    monkeypatch.setattr(P, "NUT", nut_server(b"\xff END LIST UPS\n"))
    assert P.nut_list() is None
    closed = socket.create_server(("127.0.0.1", 0))
    monkeypatch.setattr(P, "NUT", closed.getsockname())
    closed.close()
    assert P.nut_list() is None


def test_main_contract(monkeypatch):
    monkeypatch.setattr(P, "host_ip", lambda: "192.0.2.5")
    monkeypatch.setattr(P, "timezone", lambda: "UTC")
    monkeypatch.setattr(P, "containers", lambda ip: [{"name": "jf", "image": "jellyfin", "webui": ""}])
    monkeypatch.setattr(P, "netdata", lambda ip: f"http://{ip}:19999")
    monkeypatch.setattr(P, "jellyfin", lambda rows, ip: f"http://{ip}:8096")
    monkeypatch.setattr(P, "ups", lambda ip: [f"ups@{ip}:3493"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert P.main() == 0
    data = json.loads(out.getvalue())
    assert set(data) == KEYS
    assert data["containers"] == [{"name": "jf", "image": "jellyfin", "webui": ""}]


def test_main_omits_unknown(monkeypatch):
    for name in ("host_ip", "timezone"):
        monkeypatch.setattr(P, name, lambda: None)
    for name in ("containers", "netdata", "ups"):
        monkeypatch.setattr(P, name, lambda _ip: None)
    rows: list[object] = []
    monkeypatch.setattr(P, "jellyfin", recorder(rows, None))
    assert P.probe() == {}
    assert rows == [[]]


def test_script_entry(monkeypatch, capsys):
    def refuse(*_args, **_kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(P.__file__, run_name="__main__")
    assert exit_info.value.code == 0
    assert set(json.loads(capsys.readouterr().out)) <= KEYS - {"containers", "netdata", "jellyfin", "ups"}
