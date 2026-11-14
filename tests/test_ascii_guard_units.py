import importlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import ascii_charset as charset


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "tools"))
    return importlib.import_module("tools.ascii_guard")


@pytest.mark.parametrize("point,kind", [(65, None), (0x200B, charset.BANNED_INVISIBLE),
                                       (0x2014, charset.BANNED_DASH), (0x201C, charset.BANNED_QUOTE),
                                       (0xFF41, charset.BANNED_CONFUSABLE), (0x2192, None),
                                       (0x00A7, None), (0x0410, charset.BANNED_SCRIPT),
                                       (0x1F600, charset.BANNED_EMOJI), (0x200D, charset.BANNED_EMOJI),
                                       (0x20AC, charset.BANNED_UNLISTED)])
def test_character_classes_remain_fail_closed(point, kind):
    assert charset.classify(chr(point)) == kind


def test_comment_parsers_keep_literals_out_and_find_real_comments(guard):
    assert guard._python_comments('x = "# ignored" # real\n') == [(1, 17, "# real")]
    assert guard._python_comments('"unterminated\n') == []
    assert guard._python_comments('"""unfinished') == []
    text = 'a = "escaped \\" quote # literal" # real\nb = "unfinished\nmore" # end\n'
    comments = guard._hash_comments(text)
    assert comments[-1][2] == "# end"
    assert guard._hash_comments("a = '''\n# literal\n''' # real\n", triple_quotes=True) == [(3, 5, "# real")]
    assert guard._hash_comments("a: |\n  # literal\n\nb: x # real\n", yaml_blocks=True) == [(4, 6, "# real")]
    assert guard._hash_comments("a=x ; real", semicolon=True) == [(1, 5, "; real")]
    code = 'a="literal // none"; /* first\ncontinued */ b=1; // end\n'
    assert [row[2] for row in guard._code_comments(code, "//")] == ["/* first", "continued */", "// end"]
    assert guard._code_comments('"\\\\\\"" // comment\n"unterminated\n// next', "//")[-1][2] == "// next"
    assert guard._code_comments('`first\n// literal\nend` // real', "//", template_quotes=True)[-1][2] == "// real"
    assert guard._code_comments("$tag$\n-- literal\n$tag$ -- real", "--", dollar_quotes=True)[-1][2] == "-- real"
    assert guard._code_comments("/* complete */ x", "//") == [(1, 1, "/* complete */")]
    assert guard._code_comments("/* open\nstill open", "//")[-1][2] == "still open"
    assert guard._html_comments("<!-- real --> <p>x</p>") == [(1, 1, "<!-- real -->")]


@pytest.mark.parametrize("path,text", [("x.py", "# real"), ("x.yaml", "# real"), ("x.toml", "# real"),
                                      ("x.sh", "# real"), ("Dockerfile", "# real"), ("x.ini", "; real"),
                                      ("x.js", "// real"), ("x.css", "/* real */"), ("x.go", "// real"),
                                      ("x.cpp", "// real"), ("x.html", "<!-- real -->"), ("x.sql", "-- real")])
def test_comment_formats_are_recognized(guard, path, text):
    assert guard._comments(text, path) == [(1, 1, text)]


def test_metadata_excludes_urls_idioms_colours_and_overlaps(guard):
    text = "# task 123; 2020-01-02; owner: Person; was 10px; changed from 1 to 2; #456"
    assert len(guard._metadata_matches(text, "x.py")) == 6
    assert guard._metadata_matches("# https://example.com/issue/123#456 the #1", "x.py") == []
    assert guard._metadata_matches("/* #123 #1234 #123456 #12345678 */", "x.css") == []
    assert len(guard._metadata_matches("# issue #123", "x.py")) == 1
    assert guard._comments("# metadata", "x.txt") == []
    hit = guard.scan_text("# task 123\n", "x.py")[0]
    assert hit.char == hit.text == "task 123" and "comment-metadata" in str(hit)
    unnamed = guard.Hit("x.py", 1, 2, chr(0x0378), charset.REVIEW, "")
    assert unnamed.name == "U+0378" and "U+0378" in str(unnamed)
    named = guard.Hit("x.py", 1, 1, chr(0x2014), charset.BANNED_DASH, "")
    assert named.name == "EM DASH" and "EM DASH" in str(named)


def test_rendered_escapes_and_markdown_rules(guard):
    text = r'\u{1F600} \u2014 \U0001F600 &#x2014; &#8212; &mdash; &unknown; &#99999999; \\u2014; \u0041'
    assert len(guard.scan_text(text, "x.js")) == 6
    assert guard.scan_text(text, "x.py") == []
    assert len(guard.scan_text(chr(0x2014) + chr(0x1F600), "x.md")) == 1


def entry(**updates):
    value = {"path": "x.py", "kind": charset.BANNED_DASH, "text": chr(0x2014), "line_text": chr(0x2014),
             "column": 1, "reason": "deliberate test input"}
    value.update(updates)
    return value


@pytest.mark.parametrize("bad", [None, [1], [{}], [entry(extra=True)], [entry(reason="")], [entry(reason=1)],
                                 [entry(kind="unknown")], [entry(path="")], [entry(text=2)], [entry(line_text="")],
                                 [entry(path="../outside")], [entry(path="/outside")], [entry(line_text="x\ny")],
                                 [entry(line_text="x\ry")], [entry(column=0)], [entry(column=True)]])
def test_allowlist_rejects_the_whole_file_on_one_bad_entry(guard, tmp_path, bad, capsys):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps(bad))
    assert guard.load_allowlist(path) == []
    assert "ignoring the entire allowlist" in capsys.readouterr().err


def test_allowlist_missing_corrupt_and_exact_matching(guard, tmp_path, monkeypatch):
    path = tmp_path / "allow.json"
    assert guard.load_allowlist(path) == []
    path.write_bytes(b"not JSON")
    assert guard.load_allowlist(path) == []
    path.write_text(json.dumps([entry()]))
    assert guard.load_allowlist(path) == [entry()]
    hits = guard.scan_text(chr(0x2014), "x.py")
    assert guard.suppress_allowlisted(hits, [entry()]) == ([], 1)
    assert guard.suppress_allowlisted(hits, []) == (hits, 0)
    meta = guard.MetadataHit("x.py", 1, 1, "task 123", "# task 123")
    assert guard.suppress_allowlisted([meta], [entry(kind=charset.BANNED_METADATA, text=meta.text,
                                                   line_text=meta.line_text)]) == ([], 1)
    monkeypatch.chdir(tmp_path)
    assert guard._allowlist_path(str(tmp_path / "x.py")) == "x.py"
    assert os.path.isabs(guard._allowlist_path(str(tmp_path.parent / "outside.py")))


def test_walk_scan_and_scope_use_only_expected_files(guard, tmp_path, monkeypatch, capsys):
    clean = tmp_path / "clean.py"
    clean.write_text("x=1")
    (tmp_path / "LICENSE").write_text("legal")
    (tmp_path / "image.png").write_bytes(b"data")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/hidden.py").write_text("hidden")
    assert guard.walk([str(clean), str(tmp_path), str(tmp_path / "missing")]) == [str(clean)]
    assert guard.walk([str(tmp_path / "image.png")]) == []
    assert "no such path" in capsys.readouterr().err
    assert guard.scan_file(str(clean)) == []
    assert guard.scan_file(str(tmp_path / "missing")) == []
    assert guard.wanted("Dockerfile") and not guard.wanted("LICENSE")
    monkeypatch.chdir(tmp_path)
    result = SimpleNamespace(stdout=b"clean.py\0missing.py\0image.png\0\0")
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: result)
    assert guard.scan_scope(["."]) == ["clean.py"]
    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(guard.subprocess, "run", failed)
    assert guard.scan_scope(["."]) == ["./clean.py"]


def result(stdout="", code=0):
    return SimpleNamespace(stdout=stdout, stderr="failure", returncode=code)


def test_staged_and_pushed_modes_handle_deletions_duplicates_and_failed_blobs(guard, monkeypatch):
    answers = iter([result("x.py\n\nimage.png\ny.py"), result(chr(0x2014)), result(code=1)])
    monkeypatch.setattr(guard, "_git", lambda *a: next(answers))
    assert len(guard.staged_hits()) == 1
    monkeypatch.setattr(guard, "_git", lambda *a: result(code=1))
    assert guard.staged_hits() is None
    assert guard.pushed_hits("local 123 remote 456") is None
    answers = iter([result("x.py\ny.py"), result(chr(0x2014)), result(code=1), result("x.py"), result(chr(0x2014))])
    seen = []
    def fake(*args):
        seen.append(args)
        return next(answers)
    monkeypatch.setattr(guard, "_git", fake)
    assert len(guard.pushed_hits("invalid\nlocal 0 remote 111\nlocal 123 remote 0\nlocal 456 remote 111\n")) == 2
    assert seen[0][0] == "ls-tree" and any(call[0] == "diff" for call in seen)
    answers = iter([result("x.py"), result("clean"), result("x.py")])
    monkeypatch.setattr(guard, "_git", lambda *a: next(answers))
    assert guard.pushed_hits("local 123 remote 111\nlocal 123 remote 111") == []


def test_fix_reports_preserved_emoji_and_rewrites_only_unambiguous_characters(guard, tmp_path):
    path = tmp_path / "x.py"
    path.write_text("a " + chr(0x2014) + " b " + chr(0x1F600))
    assert guard.fix_files([path]) == ([path], 1)
    assert path.read_text() == "a--b " + chr(0x1F600)
    assert guard.fix_files([path]) == ([], 1)


def test_report_distinguishes_review_warning_and_fatal_kinds(guard, capsys):
    bad = guard.scan_text(chr(0x2014) * 21, "x.py")
    review = [guard.Hit(str(i), 1, 1, chr(0x0378), charset.REVIEW) for i in range(7)]
    assert guard.report(bad + review) == 1
    assert "and 1 more" in capsys.readouterr().out
    assert guard.report(bad, warn_only=True) == 0
    assert guard.report(bad, fail_on={charset.BANNED_EMOJI}) == 0
    assert guard.report(bad, fail_on={charset.BANNED_DASH}) == 1
    assert guard.report(review[:1]) == 0
    assert guard.report([]) == 0


def test_inventory_and_cli_modes(guard, tmp_path, monkeypatch, capsys):
    path = tmp_path / "x.py"
    path.write_text("clean")
    assert guard.inventory([str(path)]) == 0
    path.write_text("a" + chr(0x2192) * 4 + chr(0x2014))
    assert guard.inventory([str(path)]) == 0
    assert "1 refused, 1 allowlisted" in capsys.readouterr().out
    monkeypatch.setattr(guard, "scan_scope", lambda paths: [str(tmp_path / "missing"), str(path)])
    assert guard.inventory([str(path)]) == 0
    assert guard.main(["--inventory", str(path)]) == 0
    assert guard.main(["--fix", str(path)]) == 0
    path.write_text(chr(0x1F600))
    assert guard.main(["--fix", str(path)]) == 0
    assert guard.main(["--fix", "--staged"]) == 2
    assert guard.main(["--fix", "--pushed"]) == 2
    assert guard.main(["--staged", "--pushed"]) == 2
    assert guard.main(["--fail-on", "unknown", str(path)]) == 2
    assert guard.main(["--fail-on", charset.BANNED_EMOJI, str(path)]) == 1
    monkeypatch.setattr(guard, "staged_hits", lambda: None)
    assert guard.main(["--staged"]) == 2
    monkeypatch.setattr(guard, "pushed_hits", lambda text: [])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("refs"))
    assert guard.main(["--pushed"]) == 0
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: result())
    assert guard._git("status").returncode == 0
    path.write_text("clean")
    monkeypatch.setattr(sys, "argv", ["ascii_guard.py", "--check", str(path)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(guard.__file__, run_name="__main__")
    assert exit_info.value.code == 0


def test_vendor_requires_ascii_bytes_and_authored_files_keep_all_checks(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "__file__", str(tmp_path / "tools/ascii_guard.py"))
    monkeypatch.chdir(tmp_path)
    vendor = tmp_path / "docs/demo/vendor/library.js"
    vendor.parent.mkdir(parents=True)
    body = br'const dash = "\u2014", issue = "#999";' + b"\x1f"
    vendor.write_bytes(body)
    assert guard.scan_file("docs/demo/vendor/library.js") == []
    assert guard.main(["--check", "docs/demo/vendor/library.js"]) == 0
    for relative in ("docs/demo/outside.js", "docs/demo/vendorish/library.js"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        hits = guard.scan_file(relative)
        assert len(hits) == 2
        assert any(isinstance(hit, guard.MetadataHit) and hit.kind == charset.BANNED_DASH
                   and hit.text == r"\u2014" for hit in hits)
        assert any(isinstance(hit, guard.Hit) and hit.kind == charset.BANNED_INVISIBLE
                   and hit.char == chr(0x1F) for hit in hits)
        assert guard.main(["--check", relative]) == 1
    for payload in (b"\xff", chr(0x2014).encode(), chr(0x2192).encode(), chr(0x2028).encode()):
        vendor.write_bytes(body + payload)
        hits = guard.scan_file(str(vendor))
        assert len(hits) == 1 and isinstance(hits[0], guard.Hit)
        assert hits[0].kind == charset.BANNED_UNLISTED and ord(hits[0].char) > 127
        assert guard.main(["--check", str(vendor)]) == 1
    vendor.write_bytes(body)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert guard.scan_file(str(vendor)) == []
    assert guard.main(["--check", str(vendor)]) == 0
