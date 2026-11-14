"""Fetch the preview's Python runtime into docs/demo/pyodide, checking every file against a pinned sha256, and
write docs/demo/sources.json, the package files the page loads into it, and docs/demo/manifest.json, the runtime and
vendored files the page caches, under a stamp that names the cache.

Run from the repository root: python -m tools.preview_assets [--sources-only]. The pins live in
docs/demo/runtime.json; package wheels are checked against the pinned pyodide-lock.json.
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "demo"
PACKAGE = ROOT / "shazzzbot"
LIMIT = 64 * 1024 * 1024


def sources() -> list[str]:
    files = [p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
             and (p.suffix == ".py" or p.parent.name in ("fonts", "icons") and p.suffix in (".ttf", ".png"))]
    return sorted(["VERSION", *(p.relative_to(PACKAGE).as_posix() for p in files)])


def write_sources() -> None:
    (PAGE / "sources.json").write_text(json.dumps({"files": sources()}, indent=1) + "\n")


def fetch(url: str, sha256: str, dest: Path) -> bytes:
    if dest.is_file() and hashlib.sha256(data := dest.read_bytes()).hexdigest() == sha256:
        return data
    if not url.startswith("https://"):
        raise SystemExit(f"refusing a non-https download: {url}")
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(url, timeout=60) as r:  # nosec B310 -- committed HTTPS pins; reviewed 2026-11.
        data = r.read(LIMIT + 1)
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"{dest.name}: download does not match its pinned sha256")
    dest.write_bytes(data)
    return data


def needed(lock: dict, names: list[str]) -> list[str]:
    out: list[str] = []
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in out:
            out.append(name)
            todo.extend(lock["packages"][name]["depends"])
    return sorted(out)


def fetch_runtime(out: Path) -> None:
    pins = json.loads((PAGE / "runtime.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    runtime = []
    for name, sha in pins["core"].items():
        runtime.append((name, sha, pins["cdn"] + name, fetch(pins["cdn"] + name, sha, out / name)))
    lock = json.loads((out / "pyodide-lock.json").read_text())
    for name in needed(lock, pins["packages"]):
        entry = lock["packages"][name]
        url = pins["cdn"] + entry["file_name"]
        file = entry["file_name"]
        runtime.append((file, entry["sha256"], url, fetch(url, entry["sha256"], out / file)))
    for name, wheel in pins["wheels"].items():
        runtime.append((name, wheel["sha256"], wheel["url"], fetch(wheel["url"], wheel["sha256"], out / name)))
    write_manifest(runtime)


def write_manifest(runtime: list[tuple[str, str, str, bytes]]) -> None:
    vendored = sorted(p for p in (PAGE / "vendor").glob("*.js"))
    stamp = hashlib.sha256(b"".join(sha.encode() for _, sha, _, _ in runtime)
                           + b"".join(p.read_bytes() for p in vendored)).hexdigest()[:12]
    manifest = {
        "stamp": stamp,
        "runtime": [{"name": n, "bytes": len(data), "url": url} for n, _, url, data in runtime],
        "vendor": [{"path": p.relative_to(PAGE).as_posix(), "bytes": p.stat().st_size} for p in vendored]}
    (PAGE / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources-only", action="store_true", help="only rewrite docs/demo/sources.json")
    parser.add_argument("--out", type=Path, default=PAGE / "pyodide", help="runtime folder (default docs/demo/pyodide)")
    args = parser.parse_args(argv)
    write_sources()
    if not args.sources_only:
        fetch_runtime(args.out)


if __name__ == "__main__":
    main(sys.argv[1:])
