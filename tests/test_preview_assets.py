import fnmatch
import hashlib
import json
import runpy
import sys

import pytest

from tools import preview_assets as pa

DATA = b"runtime bytes"
SHA = hashlib.sha256(DATA).hexdigest()


class Reply:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, n):
        return self.data[:n]


def test_sources_list_the_package_files_the_page_loads(tmp_path, monkeypatch):
    files = pa.sources()
    assert "bot.py" in files and "previewdata.py" in files and files == sorted(files)
    assert files.count("VERSION") == 1 and (pa.ROOT / "VERSION").is_file()
    assert any(f.startswith("fonts/") and f.endswith(".ttf") for f in files)
    assert not any("__pycache__" in f for f in files)
    assert json.loads((pa.PAGE / "sources.json").read_text())["files"] == files
    monkeypatch.setattr(pa, "PAGE", tmp_path)
    pa.write_sources()
    assert json.loads((tmp_path / "sources.json").read_text()) == {"files": files}


def test_fetch_checks_https_and_the_pinned_hash(tmp_path, monkeypatch):
    urls = []

    def urlopen(url, timeout):
        urls.append(url)
        return Reply(DATA)
    monkeypatch.setattr(pa.urllib.request, "urlopen", urlopen)
    dest = tmp_path / "f"
    with pytest.raises(SystemExit, match="non-https"):
        pa.fetch("http://x.example/f", SHA, dest)
    with pytest.raises(SystemExit, match="pinned sha256"):
        pa.fetch("https://x.example/f", "0" * 64, dest)
    assert not dest.exists()
    assert pa.fetch("https://x.example/f", SHA, dest) == DATA and dest.read_bytes() == DATA
    assert pa.fetch("https://x.example/f", SHA, dest) == DATA and len(urls) == 2
    monkeypatch.setattr(pa, "LIMIT", 4)
    dest.unlink()
    with pytest.raises(SystemExit, match="pinned sha256"):
        pa.fetch("https://x.example/f", hashlib.sha256(DATA[:5]).hexdigest(), dest)


def test_runtime_fetches_core_files_package_dependencies_and_wheels(tmp_path, monkeypatch):
    lock = {"packages": {"a": {"depends": ["b"], "file_name": "a.whl", "sha256": "sa"},
                         "b": {"depends": ["a"], "file_name": "b.whl", "sha256": "sb"},
                         "c": {"depends": [], "file_name": "c.whl", "sha256": "sc"}}}
    pins = {"cdn": "https://cdn.example/", "core": {"pyodide-lock.json": "sl"}, "packages": ["a"],
            "wheels": {"w.whl": {"url": "https://pypi.example/w.whl", "sha256": "sw"}}}
    page = tmp_path / "page"
    page.mkdir()
    (page / "runtime.json").write_text(json.dumps(pins))
    monkeypatch.setattr(pa, "PAGE", page)
    got = []

    def fetch(url, sha, dest):
        got.append((url, sha, dest.name))
        if dest.name == "pyodide-lock.json":
            dest.write_text(json.dumps(lock))
        return b"x"
    monkeypatch.setattr(pa, "fetch", fetch)
    assert pa.needed(lock, ["a"]) == ["a", "b"]
    pa.fetch_runtime(tmp_path / "out")
    assert got == [("https://cdn.example/pyodide-lock.json", "sl", "pyodide-lock.json"),
                   ("https://cdn.example/a.whl", "sa", "a.whl"), ("https://cdn.example/b.whl", "sb", "b.whl"),
                   ("https://pypi.example/w.whl", "sw", "w.whl")]


def test_cli(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pa, "write_sources", lambda: calls.append("sources"))
    monkeypatch.setattr(pa, "fetch_runtime", lambda out: calls.append(out))
    pa.main(["--sources-only"])
    pa.main(["--out", str(tmp_path)])
    assert calls == ["sources", "sources", tmp_path]
    monkeypatch.setattr(sys, "argv", ["preview_assets.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(pa.__file__, run_name="__main__")
    assert caught.value.code == 0


def test_docker_stages_and_context_include_the_canonical_version_resource():
    dockerfile = (pa.ROOT / "Dockerfile").read_text()
    stages = dockerfile.split("FROM ")[1:]
    assert len(stages) == 2
    assert all("COPY VERSION ./VERSION\n" in stage for stage in stages)
    rules = [line.strip() for line in (pa.ROOT / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    matching = [rule for rule in rules if fnmatch.fnmatchcase("VERSION", rule.removeprefix("!"))]
    assert matching[-1] == "!VERSION"
