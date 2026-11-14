"""Fail closed unless release versions and the source commit's CI result agree."""
import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

ASSETS = ("source.tar.gz", "helper.pyz", "helper-unraid.json", "shazzzbot-helper-unraid.py",
          "release.json", "SHA256SUMS")


def version_file(path: Path) -> str:
    value = path.read_bytes().decode("ascii").removesuffix("\n")
    check_versions("v" + value, value, value, value)
    return value


def notes(text: str, tag: str) -> str:
    version = tag.removeprefix("v")
    check_versions(tag, version, version, version)
    lines = text.splitlines(keepends=True)
    found = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == f"## {version}"]
    if len(found) != 1:
        raise ValueError("CHANGELOG must contain exactly one section for the release version")
    start = found[0] + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    body = "".join(lines[start:end]).strip()
    if not body:
        raise ValueError("the release CHANGELOG section must not be empty")
    return body + "\n"


def check_draft(value: object) -> dict:
    if not isinstance(value, dict) or value.get("draft") is not True:
        raise ValueError("release must remain a draft until every asset is verified")
    return value


def check_assets(value: object, root: Path) -> None:
    assets = check_draft(value).get("assets")
    if not isinstance(assets, list) or len(assets) != len(ASSETS):
        raise ValueError("release must contain exactly the six expected assets")
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") not in ASSETS or asset["name"] in seen:
            raise ValueError("release contains an unexpected or duplicate asset")
        name = asset["name"]
        seen.add(name)
        if type(asset.get("size")) is not int or asset["size"] != (root / name).stat().st_size \
                or asset["size"] <= 0 or asset.get("state") != "uploaded":
            raise ValueError("release asset size or upload state does not match the local file")


def _digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(65536):
            digest.update(block)
    return digest.digest()


def check_downloads(root: Path, downloaded: Path) -> None:
    if {path.name for path in downloaded.iterdir()} != set(ASSETS):
        raise ValueError("downloaded release assets do not match the expected names")
    for name in ASSETS:
        if _digest(root / name) != _digest(downloaded / name):
            raise ValueError("downloaded release asset SHA-256 does not match the local file")


def check_versions(tag: str, source: str, bot: str, helper: str) -> None:
    if not isinstance(tag, str) or \
            re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag) is None:
        raise ValueError("release tag must be canonical vMAJOR.MINOR.PATCH")
    if any(value != tag[1:] for value in (source, bot, helper)):
        raise ValueError("release tag, source, bot, and helper versions must agree")


def check_ci(data: object, sha: str) -> None:
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("source commit must be a full lowercase SHA-1")
    valid = isinstance(data, dict) and isinstance(data.get("workflow_runs"), list)
    if not valid:
        raise ValueError("CI response must contain workflow_runs")
    if not any(isinstance(run, dict) and run.get("head_sha") == sha and run.get("event") == "push"
               and run.get("status") == "completed" and run.get("conclusion") == "success"
               for run in data["workflow_runs"]):
        raise ValueError("source commit has no completed successful push CI run")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    versions = commands.add_parser("versions")
    for name in ("tag", "source", "bot", "helper"):
        versions.add_argument(name)
    ci = commands.add_parser("ci")
    ci.add_argument("sha")
    ci.add_argument("json_file", type=Path)
    version = commands.add_parser("version")
    version.add_argument("path", type=Path)
    changelog = commands.add_parser("notes")
    changelog.add_argument("tag")
    changelog.add_argument("path", type=Path)
    draft = commands.add_parser("draft")
    draft.add_argument("json_file", type=Path)
    assets = commands.add_parser("assets")
    assets.add_argument("json_file", type=Path)
    assets.add_argument("root", type=Path)
    downloads = commands.add_parser("downloads")
    downloads.add_argument("root", type=Path)
    downloads.add_argument("downloaded", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "versions":
            check_versions(args.tag, args.source, args.bot, args.helper)
        elif args.command == "ci":
            check_ci(json.loads(args.json_file.read_text()), args.sha)
        elif args.command == "version":
            print(version_file(args.path))
        elif args.command == "notes":
            print(notes(args.path.read_text(), args.tag), end="")
        elif args.command == "draft":
            check_draft(json.loads(args.json_file.read_text()))
        elif args.command == "assets":
            check_assets(json.loads(args.json_file.read_text()), args.root)
        else:
            check_downloads(args.root, args.downloaded)
    except (OSError, ValueError) as error:
        parser.exit(1, f"release check failed: {error}\n")


if __name__ == "__main__":
    main()
