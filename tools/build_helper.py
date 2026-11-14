import argparse
import hashlib
import io
import json
import re
import sys
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    from tools.release_check import version_file

    return version_file(ROOT / "VERSION")


def build(output: Path, source: Path | None = None) -> str:
    release_version = version()
    source = source or Path(__file__).resolve().parent.parent / "shazzzbot" / "helper"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        members = {"__main__.py": b"from helper.cli import main\n\nmain()\n",
                   "VERSION": (release_version + "\n").encode("ascii")}
        members.update({"helper/" + path.relative_to(source).as_posix(): path.read_bytes()
                        for path in source.rglob("*.py")})
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    stream.seek(0)
    zipapp.create_archive(stream, target=output, interpreter="/usr/bin/env python3")
    output.chmod(0o700)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def bundle(output: Path, tag: str, revision: str, source: Path | None = None) -> Path:
    from shazzzbot.helper import formats
    from tools.setup_artifacts import MAX_UPDATE, PLATFORMS

    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) is None or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("bundle needs a release tag and revision")
    source = source or Path(__file__).resolve().parent.parent / "shazzzbot/helper"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "helper.pyz"
    digest = build(archive, source)
    if not 1 <= archive.stat().st_size <= MAX_UPDATE:
        raise ValueError("helper archive exceeds its size limit")
    for platform in PLATFORMS:
        name = f"shazzzbot-helper-{platform}.py"
        entry = source.parent.parent / name
        data = entry.read_bytes()
        if not 1 <= len(data) <= 65536:
            raise ValueError("helper entry exceeds its size limit")
        target = output / name
        if entry.resolve() != target.resolve():
            target.write_bytes(data)
            target.chmod(0o700)
        value = formats.dump({"platform": platform, "version": tag, "revision": revision,
                 "files": {"helper.pyz": {"sha256": digest, "size": archive.stat().st_size},
                           name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}}, 1)
        path = output / f"helper-{platform}.json"
        path.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n", encoding="ascii")
    return path


def main() -> None:
    if __package__ in {None, ""}:
        sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description="Build the standalone host helper zipapp", allow_abbrev=False)
    parser.add_argument("output", type=Path, nargs="?", default=Path("helper.pyz"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--version", default="v" + version())
    parser.add_argument("--revision")
    args = parser.parse_args()
    if args.bundle_dir is not None:
        if args.revision is None:
            parser.error("--bundle-dir needs --revision")
        print(bundle(args.bundle_dir, args.version, args.revision, args.source))
        return
    print(f"{build(args.output, args.source)}  {args.output}")


if __name__ == "__main__":
    main()
