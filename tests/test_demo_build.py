import asyncio
import io
import itertools
import json
import re
import runpy
import sys

import pytest
from PIL import Image

from shazzzbot import __version__, previewdata, schemaview
from tools import demo_build as demo


def pngs(root, item):
    return (root / item["file"]).read_bytes().hex() if "file" in item else ""


def test_demo_uses_invented_data_and_the_real_renderers(tmp_path):
    result = demo.build(tmp_path)
    assert result == json.loads((tmp_path / "frames.json").read_text())
    metadata = (tmp_path / "schema.json").read_bytes()
    assert metadata == schemaview.dumps().encode()
    schema = json.loads(metadata)
    assert schema == schemaview.document()
    assert schema["version"] == __version__
    sensitive = [schema["sections"]["preview"]["password_hash"], schema["environment"]["DISCORD_TOKEN"]]
    assert all(field["sensitive"] and not {"default", "example", "default_by", "choices"} & field.keys()
               for field in sensitive)
    assert set(result["sets"]) == set(demo.SETS)
    for name, value in result["sets"].items():
        assert len(value["frames"]) == demo.FRAMES
        for frame in value["frames"]:
            assert set(frame) == {key for key, _ in demo.MODES}
            files = [item["file"] for mode in ("image", "default") for msg in frame[mode] for item in msg
                     if "file" in item]
            assert files and all((tmp_path / f).read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for f in files)
            assert not any("file" in item for mode in ("text", "rich") for msg in frame[mode] for item in msg)
            default = {item["embed"]["title"]: "file" in item for msg in frame["default"] for item in msg
                       if "title" in item["embed"]}
            assert default["Tasks"] is False and default["Services"] is True and default["Disks"] is True
        text = json.dumps(value)
        assert ("needs the shazzzbot helper" in text) == (name == "nohelper")
    assert demo.snapshot(demo.frame_time(demo.WORST)).cpu_pct == 100
    assert result["names"][:6] == ["All good", "Mostly good", "Mixed bag", "Mostly bad", "All bad", "Max width"]
    assert all(len(name) <= 16 for name in result["names"])
    assert result["names"][6:10] == ["Parity running", "Parity paused", "Parity w/ errors", "Parity good"]
    for name, value in result["sets"].items():
        for n, spec in enumerate(demo.SPECS):
            if spec.severity is not None and name == "helper":
                assert value["severity"][n] == spec.severity, spec.name
        for mode in ("text", "rich", "image"):
            shown = [json.dumps([[{k: v for k, v in item.items() if k != "file"} | {"png": pngs(tmp_path, item)}
                                  for item in msg] for msg in frame[mode]]) for frame in value["frames"]]
            assert all(a != b for a, b in itertools.pairwise(shown)), (name, mode)
    assert demo.frozen(demo.Clock()).now().timestamp() == demo.START
    snaps = json.loads((tmp_path / "snapshots.json").read_text())
    assert snaps["interval_s"] == demo.INTERVAL_S and set(snaps["sets"]) == set(demo.SETS)
    good = snaps["sets"]["helper"][demo.NAMES.index("Parity good")]["array"]["parity"]
    assert good["running"] is False and good["paused"] is False
    assert good["last_errors"] == 0 and good["last_exit"] == 0 and good["last_end"] > 0
    toml = (demo.ROOT / "docs" / "demo" / "demo.toml").read_text()
    for name, value in result["sets"].items():
        for n, (snap, built) in enumerate(zip(snaps["sets"][name], value["frames"], strict=True)):
            out = asyncio.run(previewdata.render(json.dumps(snap), toml, images=n == demo.WORST))
            assert out["diagnostics"] == []
            assert out["frame"]["text"] == built["text"] and out["frame"]["rich"] == built["rich"]
            for mode in ("image", "default") if n == demo.WORST else ():
                got = [item for msg in out["frame"][mode] for item in msg]
                want = [item for msg in built[mode] for item in msg]
                assert [g["embed"] for g in got] == [w["embed"] for w in want]
                for g, w in zip(got, want, strict=True):
                    assert ("png" in g) == ("file" in w)
                    if "file" in w:
                        assert g["name"] == w["file"].rsplit("/", 1)[1]
                        pixels = Image.open(io.BytesIO(g["png"])).tobytes()
                        assert pixels == Image.open(tmp_path / w["file"]).tobytes()


def test_demo_cli_uses_the_requested_directory(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(demo, "build", lambda path: calls.append(path))
    demo.main(["--out", str(tmp_path)])
    assert calls == [tmp_path]
    monkeypatch.setattr(sys, "argv", ["demo_build.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(demo.__file__, run_name="__main__")
    assert caught.value.code == 0
    assert "--out" in capsys.readouterr().out


def test_demo_is_deterministic_private_and_helper_free_without_the_helper(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert demo.build(first) == demo.build(second)
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    assert all((first / p).read_bytes() == (second / p).read_bytes() for p in files)
    sets = json.loads((first / "frames.json").read_text())["sets"]
    helper, nohelper = json.dumps(sets["helper"]), json.dumps(sets["nohelper"])
    for marker in ("off at", "last took", "Plugin update check", "update available"):
        assert marker in helper
        assert marker not in nohelper
    text = helper + nohelper + (first / "snapshots.json").read_text()
    assert not re.search(r"(?<![\d.])\d{17,20}(?!\d)", text)
    assert all(ip.startswith("192.0.2.") for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    assert "unraid-" not in text.lower() and "neal" not in text.lower()
