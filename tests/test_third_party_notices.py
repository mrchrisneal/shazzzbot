import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normal(name):
    return re.sub(r"[-_.]+", "-", name).casefold()


def notice_rows():
    rows = {}
    for line in (ROOT / "THIRD_PARTY_NOTICES.md").read_text().splitlines():
        if line.startswith("|"):
            fields = [field.strip().strip("`") for field in line.strip("|").split("|")]
            if len(fields) == 5:
                rows[normal(fields[0])] = fields
    return rows


def test_notices_cover_all_locked_python_dependencies():
    rows = notice_rows()
    for lock in ROOT.glob("requirements*.txt"):
        for name, version in re.findall(r"^([\w.-]+)(?:\[[^]]+\])?==([^\s\\;]+)", lock.read_text(), re.MULTILINE):
            assert normal(name) in rows, (lock.name, name)
            assert version in rows[normal(name)][1].split(", "), (name, version)


def test_notices_cover_vendored_library_files_and_header_versions():
    rows = notice_rows()
    aliases = {
        "state": "@codemirror/state", "lint": "@codemirror/lint", "language": "@codemirror/language",
        "legacy-modes": "@codemirror/legacy-modes", "commands": "@codemirror/commands",
        "autocomplete": "@codemirror/autocomplete", "search": "@codemirror/search",
        "highlight": "@lezer/highlight", "lr": "@lezer/lr",
    }
    for path in sorted((ROOT / "docs/demo/vendor").rglob("*")):
        legal = path.name.casefold() in {"license", "license.txt", "license.md"} \
            or re.search(r"[-_]license(?:\.(?:txt|md|rst))?$", path.name, re.IGNORECASE)
        if not path.is_file() or legal:
            continue
        relative = path.relative_to(ROOT).as_posix()
        assert any(relative in row[4] for row in rows.values()), relative
        header = path.read_text().split("*/", 1)[0]
        for name, version in re.findall(r"(@?[\w/-]+)@(\d+(?:\.\d+)+)", header):
            key = normal(aliases.get(name, name))
            assert key in rows, (relative, name)
            assert version in rows[key][1].split(", "), (relative, name, version)


def test_notice_license_links_exist_and_required_runtime_texts_are_bundled():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    for link in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in link and not link.startswith("#"):
            assert (ROOT / link.split("#", 1)[0]).is_file(), link
    checksums = {
        "Pyodide": "1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5",
        "CPython": "b0e25a78cffb43f4d92de8b61ccfa1f1f98ecbc22330b54b5251e7b6ba010231",
        "Emscripten": "620a78084fc7ca97c0b5dea9abf891f3ffcadfdbf305276f099c9c4e12fc1d86",
    }
    for name, checksum in checksums.items():
        path = ROOT / "docs/demo/vendor" / (name + "-LICENSE.txt")
        assert path.is_file() and path.name in text
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum


def test_contribution_policy_is_literal_in_release_notes_and_pr_template():
    policy = ("Development happens in a private repository. This repository receives one commit per release. "
              "It may not be monitored regularly, and pull requests are not accepted until a future "
              "confirmed-stable release.")
    support = ("Shazzzbot supports Unraid only. Support for other systems, platforms, and integrations "
               "depends on feedback.")
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
    assert policy in template and support in template
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "printf '\\n%s\\n' '" + policy + " " + support + "' >> \"$notes\"" in workflow
