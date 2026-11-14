#!/usr/bin/env python3
"""Flag content that must never reach a committed file: emoji, non-ASCII dashes, smart quotes, and
comment metadata. Other non-ASCII content (Greek letters in the classifier's maths, the section sign,
and the degree sign) is listed separately for review and never fails the run.

    tools/ascii_guard.py --check PATH...      scan a path, a directory, or the whole repo
    tools/ascii_guard.py --check --staged     scan what is staged, read from the INDEX
    tools/ascii_guard.py --check --pushed     scan a push range, refs read from stdin (pre-push)
    tools/ascii_guard.py --fix PATH...        rewrite the unambiguous substitutions; never emoji

Exit 0 clean, 1 something banned was found, 2 the run itself failed.

This file scans FILES, never a diff. An earlier ad-hoc version scanned `git diff` output and reported a
clean tree while a warning sign sat in a file it had just been shown, which is worse than no scanner at
all: it turns "I did not check" into "I checked and it was fine".
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import unicodedata

from ascii_charset import (
    _BANNED,
    _DASHES,
    _QUOTES,
    BANNED_EMOJI,
    BANNED_METADATA,
    BANNED_UNLISTED,
    REVIEW,
    classify,
)

_SCAN_EXT = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm", ".svg",
    ".sh", ".bash", ".c", ".h", ".cpp", ".rs", ".go",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".service", ".env",
    ".sql", ".txt", ".md",
}
_EMOJI_ONLY_EXT = {".md"}
_SCAN_NAMES = {"Dockerfile", "Makefile", ".gitignore", ".gitattributes"}
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
              "htmlcov", "dist", "build"}

# How many individual hits a single run prints before it summarises the rest.
MAX_LISTED = 20

ALLOWLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ascii_guard_allowlist.json")
# Exact line text and a column identify a finding without tying it to a line number. Moving the line
# is harmless; changing its content or indentation deliberately makes the exemption stale.
_ALLOWLIST_FIELDS = {"path", "kind", "text", "line_text", "column", "reason"}




class Hit:
    __slots__ = ("char", "col", "kind", "line", "line_text", "path")

    def __init__(self, path, line, col, char, kind, line_text=""):
        self.path, self.line, self.col, self.char, self.kind = path, line, col, char, kind
        self.line_text = line_text

    @property
    def name(self):
        try:
            return unicodedata.name(self.char)
        except ValueError:
            return f"U+{ord(self.char):04X}"

    def __str__(self):
        return f"{self.path}:{self.line}:{self.col}: {self.kind}: U+{ord(self.char):04X} {self.name}"


class MetadataHit:
    __slots__ = ("col", "kind", "line", "line_text", "path", "text")

    def __init__(self, path, line, col, text, line_text=""):
        self.path, self.line, self.col, self.text = path, line, col, text
        self.kind = BANNED_METADATA
        self.line_text = line_text

    @property
    def char(self):
        """The allowlist keys a character finding on `char` and a metadata one on `text`. An escaped
        finding is a character finding whose source form is several characters, so it answers to both
        and the allowlist needs no special case for it."""
        return self.text

    def __str__(self):
        return f"{self.path}:{self.line}:{self.col}: {self.kind}: {self.text}"


_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"
)
_ATTRIBUTION_RE = re.compile(
    r"(?:\(\s*(?:owner|reviewer)\b[^)\r\n]*\)"
    r"|\b(?:owner|reviewer)\s*[:=]\s*@?[A-Za-z][\w.-]*"
    # The target must look like a PERSON: a handle, or a capitalised name. Without that this branch
    # matched the ordinary English verb, and "owned by the 'pi' user", "owned by the engine" and "the
    # side owned by the class it speaks for" are descriptions of ownership in code, not credit given
    # to somebody. The inline (?-i:) is load-bearing: this pattern is compiled IGNORECASE, under which
    # a bare [A-Z] would match a lowercase letter too and let every determiner back in.
    r"|\b(?:owned|reviewed)\s+by\s+"
    r"(?:@[\w.-]+|(?-i:(?!The\b|A\b|An\b|Its\b|This\b|That\b|Each\b|Any\b|Our\b|Some\b)[A-Z][\w.-]*)))",
    re.IGNORECASE,
)
_TASK_RE = re.compile(r"(?<![\w/])#\d+\b(?!\.\d)")

# A hash on a small number is far more often the English "number one" than a task reference: "the #1
# update risk", "the owner's #1 requirement", "Reading #1", "FIX #4", "trap-#2". These are exclusion
# zones, handled the same way a URL is, because Python requires a lookbehind to be fixed width and
# this one is not. Every example above is excluded by the pattern it documents, which is why this
# comment does not trip the guard: an example written bare, with no word in front of it, would.
_TASK_IDIOM_RE = re.compile(
    r"(?:\b(?:the|our|its|a|an|reading|fix|finding|item|step|section|trap|phase|part|case|round|pass|"
    r"tier|option|attempt|priority|rule|figure|table|note)|\b\w+'s)[\s-]*#\d+\b",
    re.IGNORECASE,
)
_NAMED_TASK_RE = re.compile(
    r"\b(?:task|issue|ticket|bug|pr)\s*(?:#|[-:])?\s*\d+\b(?!\.\d)", re.IGNORECASE
)
_URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>()]+", re.IGNORECASE)
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_YAML_BLOCK_RE = re.compile(r"(?:^|:\s+|-\s+)[|>](?:[+-]?\d?|\d[+-])?\s*$")

_NUMBER = r"~?\d+(?:\.\d+)?(?!\.\d)"
_UNIT = r"(?:px|rem|em|ms|s|sec(?:onds?)?|min(?:utes?)?|h(?:ours?)?|%|[kmgt]?b|hz|v|a|w)"
_VALUE = rf"(?:{_NUMBER}(?:{_UNIT})?|true|false|null|none)"
_UNIT_VALUE = rf"{_NUMBER}{_UNIT}"
_HISTORY_RES = (
    re.compile(rf"\bwas\s+{_UNIT_VALUE}\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:bumped|changed|increased|decreased|raised|lowered|reduced)\s+from\s+{_VALUE}"
        rf"(?:\s+to\s+{_VALUE})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:stepped|changed|bumped)\s+{_VALUE}\s*(?:->|=>)\s*{_VALUE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_NUMBER}%\s+(?:faster|slower)\s+than\s+(?:the\s+)?(?:old|previous|former)\s+{_VALUE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:before|old)\s*[:=]\s*{_VALUE}\b.*?\b(?:after|new)\s*[:=]\s*{_VALUE}\b",
        re.IGNORECASE,
    ),
)


def _python_comments(text):
    comments = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                comments.append((token.start[0], token.start[1] + 1, token.string))
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass
    return comments


def _hash_comments(text, semicolon=False, triple_quotes=False, yaml_blocks=False):
    comments = []
    markers = "#;" if semicolon else "#"
    quote = None
    block_indent = None
    for lineno, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        pos = 0
        escaped = False
        comment_pos = None
        while pos < len(line):
            if quote:
                if len(quote) > 1:
                    if line.startswith(quote, pos):
                        pos += len(quote)
                        quote = None
                    else:
                        pos += 1
                elif escaped:
                    escaped = False
                    pos += 1
                elif line[pos] == "\\":
                    escaped = True
                    pos += 1
                elif line[pos] == quote:
                    quote = None
                    pos += 1
                else:
                    pos += 1
                continue
            if triple_quotes and line.startswith(("'''", '\"\"\"'), pos):
                quote = line[pos:pos + 3]
                pos += 3
            elif line[pos] in "'\"":
                quote = line[pos]
                pos += 1
            elif line[pos] in markers and (pos == 0 or line[pos - 1].isspace()):
                comments.append((lineno, pos + 1, line[pos:]))
                comment_pos = pos
                break
            else:
                pos += 1
        if yaml_blocks and _YAML_BLOCK_RE.search(line[:comment_pos]):
            block_indent = indent
    return comments


def _code_comments(text, line_marker, block_markers=("/*", "*/"), template_quotes=False,
                   dollar_quotes=False):
    comments = []
    block_end = None
    quote = None
    for lineno, line in enumerate(text.splitlines(), 1):
        pos = 0
        while pos < len(line):
            if block_end:
                end = line.find(block_end, pos)
                stop = len(line) if end < 0 else end + len(block_end)
                comments.append((lineno, pos + 1, line[pos:stop]))
                pos = stop
                if end < 0:
                    break
                block_end = None
                continue
            if quote:
                if len(quote) > 1:
                    end = line.find(quote, pos)
                    if end < 0:
                        break
                    pos = end + len(quote)
                    quote = None
                    continue
                escaped = False
                while pos < len(line):
                    ch = line[pos]
                    pos += 1
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == quote:
                        quote = None
                        break
                if quote and quote != "`":
                    quote = None
                continue
            if dollar_quotes:
                dollar_quote = _DOLLAR_QUOTE_RE.match(line, pos)
                if dollar_quote:
                    quote = dollar_quote.group()
                    pos = dollar_quote.end()
                    continue
            if line.startswith(line_marker, pos):
                comments.append((lineno, pos + 1, line[pos:]))
                break
            if block_markers and line.startswith(block_markers[0], pos):
                end = line.find(block_markers[1], pos + len(block_markers[0]))
                stop = len(line) if end < 0 else end + len(block_markers[1])
                comments.append((lineno, pos + 1, line[pos:stop]))
                pos = stop
                if end < 0:
                    block_end = block_markers[1]
                    break
                continue
            if line[pos] in "'\"" or template_quotes and line[pos] == "`":
                quote = line[pos]
            pos += 1
    return comments


def _html_comments(text):
    return _code_comments(text, "\0", ("<!--", "-->"))


def _comments(text, path):
    base = os.path.basename(path)
    ext = os.path.splitext(base)[1].lower()
    if ext == ".py" or path == "<text>":
        return _python_comments(text)
    if ext in {".yaml", ".yml"}:
        return _hash_comments(text, yaml_blocks=True)
    if ext == ".toml":
        return _hash_comments(text, triple_quotes=True)
    if ext in {".sh", ".bash", ".env"} or base in {
        "Dockerfile", "Makefile", ".gitignore", ".gitattributes"
    }:
        return _hash_comments(text)
    if ext in {".ini", ".cfg", ".conf", ".service"}:
        return _hash_comments(text, semicolon=True)
    if ext in {".js", ".mjs", ".ts", ".tsx", ".jsx"}:
        return _code_comments(text, "//", template_quotes=True)
    if ext == ".css":
        return _code_comments(text, "\0")
    if ext == ".go":
        return _code_comments(text, "//", template_quotes=True)
    if ext in {".c", ".h", ".cpp", ".rs"}:
        return _code_comments(text, "//")
    if ext in {".html", ".htm", ".svg"}:
        return _html_comments(text)
    if ext == ".sql":
        return _code_comments(text, "--", dollar_quotes=True)
    return []


def _overlaps_url(match, urls):
    return any(match.start() < end and match.end() > start for start, end in urls)


def _metadata_matches(comment, path):
    matches = []
    urls = [match.span() for match in _URL_RE.finditer(comment)]
    for pattern in (_DATE_RE, _ATTRIBUTION_RE, _NAMED_TASK_RE, *_HISTORY_RES):
        matches.extend(match for match in pattern.finditer(comment) if not _overlaps_url(match, urls))
    idioms = [match.span() for match in _TASK_IDIOM_RE.finditer(comment)]
    for match in _TASK_RE.finditer(comment):
        css_hex = os.path.splitext(path)[1].lower() == ".css" and len(match.group()) - 1 in {3, 4, 6, 8}
        if not css_hex and not _overlaps_url(match, urls) and not _overlaps_url(match, idioms):
            matches.append(match)
    selected = []
    for match in sorted(matches, key=lambda item: (item.start(), -item.end())):
        if not any(match.start() < old.end() and match.end() > old.start() for old in selected):
            selected.append(match)
    return selected


# A banned character can hide as an ESCAPE and stay pure ASCII in the source, so a scan that only
# looks at characters reports the file clean while the browser still renders an em dash. Both forms
# below were found in this repository after a cleanup that the character scan had declared finished:
# 24 `\u2014`-style escapes in the frontend script and 91 `&mdash;`-style entities in the template.
# What the READER sees is the rule, so the escape is the same violation as the character.
_ENTITY_CODEPOINTS = {
    "mdash": 0x2014, "ndash": 0x2013, "horbar": 0x2015, "minus": 0x2212,
    "hellip": 0x2026, "nbsp": 0x00A0, "shy": 0x00AD,
    "lsquo": 0x2018, "rsquo": 0x2019, "ldquo": 0x201C, "rdquo": 0x201D,
    "sbquo": 0x201A, "bdquo": 0x201E, "prime": 0x2032, "Prime": 0x2033,
    "zwj": 0x200D, "zwnj": 0x200C, "lrm": 0x200E, "rlm": 0x200F,
}
_ESCAPE_RE = re.compile(
    r"\\u\{([0-9A-Fa-f]{1,6})\}"        # JS \u{1F600}
    r"|\\u([0-9A-Fa-f]{4})"             # JS and Python \u2014
    r"|\\U([0-9A-Fa-f]{8})"             # Python \U0001F600
    r"|&#[xX]([0-9A-Fa-f]+);"           # HTML &#x2014;
    r"|&#([0-9]+);"                     # HTML &#8212;
    r"|&([A-Za-z][A-Za-z0-9]{1,9});"    # HTML &mdash;
)


# Only the file types whose escapes are RENDERED TO SOMEBODY. Python and JSON escape sequences are
# left alone deliberately: the files that would match are the ones describing this very problem -- this
# scanner's own patterns, the docs checker's table, the allowlist that records an exemption, and tests
# that use an escaped character as deliberate input. Flagging those is how a scanner earns a reputation
# for crying wolf and gets switched off, which is the same outcome as not having one.
_ESCAPE_EXTS = {".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".svg", ".md"}


def _escaped_hits(text, path, lines):
    """Findings for banned characters written as an escape rather than as the character itself."""
    if os.path.splitext(path)[1].lower() not in _ESCAPE_EXTS:
        return []
    hits = []
    for lineno, line in enumerate(lines, 1):
        for m in _ESCAPE_RE.finditer(line):
            # A doubled backslash is a literal backslash followed by a "u", not an escape. It is how a
            # test writes the six characters it expects a page to contain.
            if m.group().startswith("\\") and m.start() and line[m.start() - 1] == "\\":
                continue
            hexdigits, u4, u8, xent, dent, name = m.groups()
            hexform = hexdigits or u4 or u8 or xent
            try:
                if name is not None:
                    cp = _ENTITY_CODEPOINTS.get(name)
                elif dent is not None:
                    cp = int(dent, 10)
                elif hexform is not None:
                    cp = int(hexform, 16)
                else:                                           # pragma: no cover -- regex bounds it
                    cp = None
            except ValueError:                                  # pragma: no cover -- regex bounds it
                continue
            if cp is None or cp > 0x10FFFF:
                continue
            kind = classify(chr(cp))
            if kind in _BANNED:
                hits.append(MetadataHit(path, lineno, m.start() + 1, m.group(), line))
                hits[-1].kind = kind
    return hits


def _metadata_hits(text, path, lines):
    hits = []
    for lineno, col, comment in _comments(text, path):
        for match in _metadata_matches(comment, path):
            hits.append(MetadataHit(path, lineno, col + match.start(), match.group(),
                                    lines[lineno - 1]))
    return hits


def _vendor_path(path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    relative = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    return relative.startswith("docs/demo/vendor/")


def scan_text(text, path="<text>"):
    """Return banned-character, comment-metadata, and review findings in `text`."""
    # ascii-vendor-bytes-only: verbatim third-party bundles (license file alongside);
    # escapes are library data such as Unicode regex ranges; reviewed 2026-11
    if _vendor_path(path):
        return [Hit(path, lineno, col, ch, BANNED_UNLISTED, line)
                for lineno, line in enumerate(text.split("\n"), 1)
                for col, ch in enumerate(line, 1) if ord(ch) > 0x7F]
    hits = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for col, ch in enumerate(line, 1):
            kind = classify(ch)
            if kind:
                hits.append(Hit(path, lineno, col, ch, kind, line))
    hits.extend(_metadata_hits(text, path, lines))
    hits.extend(_escaped_hits(text, path, lines))
    if os.path.splitext(path)[1].lower() in _EMOJI_ONLY_EXT:
        hits = [hit for hit in hits if hit.kind == BANNED_EMOJI]
    return hits


def _warn_bad_allowlist(path, detail):
    safe_path = str(path).encode("ascii", "backslashreplace").decode("ascii")
    safe_detail = str(detail).encode("ascii", "backslashreplace").decode("ascii")
    print(f"ascii_guard: WARNING: malformed allowlist {safe_path}: {safe_detail}", file=sys.stderr)
    print("ascii_guard: WARNING: ignoring the entire allowlist; all findings remain active",
          file=sys.stderr)


def _allowlist_path(path):
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized):
        relative = os.path.relpath(normalized, os.getcwd())
        if relative != ".." and not relative.startswith(".." + os.sep):
            normalized = relative
    return normalized.replace(os.sep, "/")


def load_allowlist(path=ALLOWLIST_PATH):
    """Return validated entries. A missing file is normal; any other defect rejects every entry."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as e:
        _warn_bad_allowlist(path, str(e))
        return []

    if not isinstance(raw, list):
        _warn_bad_allowlist(path, "the top level must be a JSON array")
        return []

    entries = []
    for number, entry in enumerate(raw, 1):
        prefix = f"entry {number}"
        if not isinstance(entry, dict):
            _warn_bad_allowlist(path, f"{prefix} must be an object")
            return []
        missing = sorted(_ALLOWLIST_FIELDS - set(entry))
        extra = sorted((key for key in set(entry) - _ALLOWLIST_FIELDS), key=repr)
        if missing:
            _warn_bad_allowlist(path, f"{prefix} is missing: {', '.join(missing)}")
            return []
        if extra:
            _warn_bad_allowlist(path,
                                f"{prefix} has unknown fields: {', '.join(map(repr, extra))}")
            return []
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            _warn_bad_allowlist(path, f"{prefix} needs a non-empty reason")
            return []
        if entry["kind"] not in _BANNED:
            _warn_bad_allowlist(path, f"{prefix} has unknown kind {entry['kind']!r}")
            return []
        for field in ("path", "text", "line_text"):
            if not isinstance(entry[field], str) or not entry[field]:
                _warn_bad_allowlist(path, f"{prefix} needs a non-empty {field}")
                return []
        normalized_path = _allowlist_path(entry["path"])
        outside = normalized_path == ".." or normalized_path.startswith("../")
        if os.path.isabs(entry["path"]) or outside:
            _warn_bad_allowlist(
                path, f"{prefix} path must be relative and stay inside the repository"
            )
            return []
        if "\n" in entry["line_text"] or "\r" in entry["line_text"]:
            _warn_bad_allowlist(path, f"{prefix} line_text must contain exactly one line")
            return []
        if type(entry["column"]) is not int or entry["column"] < 1:
            _warn_bad_allowlist(path, f"{prefix} column must be a positive integer")
            return []
        clean = dict(entry)
        clean["path"] = normalized_path
        entries.append(clean)
    return entries


def suppress_allowlisted(hits, entries):
    keys = {
        (entry["path"], entry["kind"], entry["text"], entry["line_text"], entry["column"])
        for entry in entries
    }
    remaining = []
    suppressed = 0
    for hit in hits:
        text = hit.text if hit.kind == BANNED_METADATA else hit.char
        key = (_allowlist_path(hit.path), hit.kind, text, hit.line_text, hit.col)
        if key in keys:
            suppressed += 1
        else:
            remaining.append(hit)
    return remaining, suppressed


def _read(path):
    """File text, with the encoding STATED. Letting the platform choose is one of the ways the earlier
    diff-based version lied about a clean tree."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def scan_file(path):
    try:
        return scan_text(_read(path), path)
    except OSError as e:
        print(f"ascii_guard: cannot read {path}: {e}", file=sys.stderr)
        return []


# VERBATIM LEGAL TEXT, NEVER SCANNED AND NEVER REWRITTEN. A licence is quoted exactly as its author
# published it, and the characters this guard would "fix" are load-bearing there: the one non-English
# letter in this repository is the l-with-stroke in "Pawel Kuna", a real person's name in an MIT
# copyright line. Rewriting it would misspell an author and alter a legal notice to satisfy a style
# rule. The dashes in the same files are the same kind of thing.
_LEGAL_FILES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "NOTICE",
                "THIRD-PARTY-NOTICES.md", "THIRD-PARTY-NOTICES.txt"}


def wanted(path):
    base = os.path.basename(path)
    if base in _LEGAL_FILES:
        return False
    if base in _SCAN_NAMES:
        return True
    return os.path.splitext(base)[1].lower() in _SCAN_EXT


def scan_scope(paths):
    """The files a wholesale scan covers: TRACKED files only.

    CI checks out the tracked tree and nothing else, so a local run that also walked ignored scratch
    directories would disagree with the run that gates a push -- and it is those directories, full of
    scanned manuals in other languages, that would bury every finding worth seeing. Falls back to a
    plain walk outside a git repository, so the tool still works on a bare copy.
    """
    import subprocess
    if list(paths) == ["."]:
        try:
            out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
            return sorted(f for f in out.stdout.decode("utf-8", "replace").split("\0")
                          if f and os.path.isfile(f) and wanted(f))
        except (OSError, subprocess.CalledProcessError):
            pass
    return walk(paths)


def walk(paths):
    """Expand files and directories into the set of scannable files, skipping the noise directories."""
    out = []
    for p in paths:
        if os.path.isfile(p):
            if wanted(p):
                out.append(p)
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                out.extend(os.path.join(root, f) for f in files if wanted(os.path.join(root, f)))
        else:
            print(f"ascii_guard: no such path: {p}", file=sys.stderr)
    return sorted(set(out))


# ============================================================================
# GIT MODES
# ============================================================================
def _git(*args, stdin=None):
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True, input=stdin,
                          encoding="utf-8", errors="replace")


def staged_hits():
    """Staged files, read from the INDEX rather than the working tree -- what is about to be committed
    is not necessarily what is on disk. Returns None if git itself failed."""
    r = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    if r.returncode:
        print(f"ascii_guard: git diff --cached failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    hits = []
    for path in [ln for ln in r.stdout.splitlines() if ln and wanted(ln)]:
        blob = _git("show", f":{path}")
        if blob.returncode == 0:
            hits.extend(scan_text(blob.stdout, path))
    return hits


_ZERO = re.compile(r"^0+$")


def pushed_hits(stdin_text):
    """The files a push would actually send, checked at their PUSHED content. git feeds a pre-push hook
    lines of `<local ref> <local sha> <remote ref> <remote sha>`; an all-zero LOCAL sha is a deletion and
    an all-zero REMOTE sha is a ref the remote has never seen, so there is no base to diff against and the
    whole tree at that commit is the honest scope."""
    hits, seen = [], set()
    for raw in stdin_text.splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        _, local_sha, _, remote_sha = parts[:4]
        if _ZERO.match(local_sha):
            continue
        if _ZERO.match(remote_sha):
            listing = _git("ls-tree", "-r", "--name-only", local_sha)
        else:
            listing = _git("diff", "--name-only", f"{remote_sha}..{local_sha}")
        if listing.returncode:
            print(f"ascii_guard: cannot enumerate {remote_sha}..{local_sha}: {listing.stderr.strip()}",
                  file=sys.stderr)
            return None
        for path in [ln for ln in listing.stdout.splitlines() if ln and wanted(ln)]:
            key = (local_sha, path)
            if key in seen:
                continue
            seen.add(key)
            blob = _git("show", f"{local_sha}:{path}")
            if blob.returncode == 0:
                hits.extend(scan_text(blob.stdout, path))
    return hits


# ============================================================================
# FIX
# ============================================================================
# A long dash carries spaces that must go with it: CONTRIBUTING.md requires the two hyphens JOINED to the words.
_LONG = "".join(chr(cp) for cp, rep in _DASHES.items() if rep == "--")
_SPACED_LONG = re.compile("[ \t]*[" + re.escape(_LONG) + "][ \t]*")


def fix_text(text):
    """Apply the unambiguous substitutions. Emoji are NEVER touched: one in a user-facing string may be a
    deliberate design decision, so removing it is a product call, not a formatting one. Returns the new
    text and the number of emoji deliberately left behind."""
    text = _SPACED_LONG.sub("--", text)
    for cp, rep in _DASHES.items():
        text = text.replace(chr(cp), rep)
    for cp, rep in _QUOTES.items():
        text = text.replace(chr(cp), rep)
    left = sum(1 for c in text if classify(c) == BANNED_EMOJI)
    return text, left


def fix_files(paths):
    changed, emoji_left = [], 0
    for path in paths:
        before = _read(path)
        after, left = fix_text(before)
        emoji_left += left
        if after != before:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(after)
            changed.append(path)
    return changed, emoji_left


# ============================================================================
# REPORT
# ============================================================================
def report(hits, warn_only=False, suppressed=0, fail_on=None):
    """Print the findings and return the exit code. REVIEW hits are always informational.

    `fail_on` narrows what makes the run FAIL without narrowing what it REPORTS. That distinction is
    the point: the tree carries a long backlog of emoji and non-ASCII dashes, so a gate that fails on
    everything would block every release from the day it is switched on and would simply be removed.
    Failing on the security classes alone holds the line where it is currently clean, and leaves the
    backlog visible on every run instead of hidden behind a passing check."""
    banned = [h for h in hits if h.kind in _BANNED]
    review = [h for h in hits if h.kind == REVIEW]
    fatal = [h for h in banned if h.kind in fail_on] if fail_on else banned

    print(f"ascii_guard: {suppressed} finding(s) suppressed by allowlist")

    # A gate that prints hundreds of lines is a gate that gets scrolled past. Show enough to act on and
    # say how much was withheld -- never silently truncate, because a hidden count reads as full coverage.
    for h in banned[:MAX_LISTED]:
        print(h)
    if len(banned) > MAX_LISTED:
        print(f"... and {len(banned) - MAX_LISTED} more (full list: "
              f"python3 tools/ascii_guard.py --check <path>)")
    if banned:
        counts = {}
        for h in banned:
            counts[h.kind] = counts.get(h.kind, 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
        files = len({h.path for h in banned})
        print(f"\nascii_guard: {len(banned)} banned finding(s) in {files} file(s) -- {summary}")
        print("  --fix rewrites dashes and quotes; emoji, comment metadata, invisible and\n"
              "         confusable characters are judgment calls and are never rewritten")
    if review:
        files = sorted({h.path for h in review})
        shown = ", ".join(files[:6]) + (" ..." if len(files) > 6 else "")
        print(f"ascii_guard: {len(review)} other non-ASCII character(s) in {len(files)} file(s), "
              f"not blocking: {shown}")
    if not banned:
        print("ascii_guard: clean")
        return 0
    if warn_only:
        print("ascii_guard: WARN-ONLY, not failing the run")
        return 0
    if fail_on and not fatal:
        print(f"ascii_guard: no {', '.join(sorted(fail_on))} finding(s); the rest is reported, "
              f"not gated")
        return 0
    if fail_on:
        print(f"ascii_guard: FAILING on {len(fatal)} finding(s) of kind "
              f"{', '.join(sorted({h.kind for h in fatal}))}")
    return 1


def inventory(paths):
    """Every non-ASCII character in scope: what it is, how often, where, and whether it is allowed.

    THE POINT IS THE UNALLOWED COLUMN. A guard that only ever says "clean" teaches nobody what is
    actually in the tree, and the allowlist can only be argued about by somebody who can see the whole
    set. This prints it: the permitted characters so the table can be checked against reality, and the
    refused ones so a stray is named, counted and located rather than merely blocked.
    """
    import collections
    counts = collections.Counter()
    where = collections.defaultdict(list)
    # TRACKED FILES ONLY, when scanning the repo wholesale. CI checks out the tracked tree and nothing
    # else, so anything ignored here would make the local report disagree with the one that gates a
    # push -- and it is the ignored scratch directories, full of scanned manuals in other languages,
    # that would dominate the output and bury the findings that matter.
    for f in scan_scope(paths):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            for ch in line:
                if ord(ch) < 128:
                    continue
                counts[ch] += 1
                if len(where[ch]) < 3:
                    where[ch].append(f"{f}:{lineno}")

    if not counts:
        print("ascii_guard inventory: no non-ASCII characters at all")
        return 0

    rows = []
    for ch, n in counts.items():
        kind = classify(ch)
        rows.append((kind is None, ch, n, kind))
    # Refused first: that is the half somebody has to act on.
    rows.sort(key=lambda r: (r[0], -r[2]))

    allowed = sum(1 for r in rows if r[0])
    refused = len(rows) - allowed
    print(f"ascii_guard inventory: {len(counts)} distinct non-ASCII character(s), "
          f"{sum(counts.values())} occurrence(s) -- {refused} refused, {allowed} allowlisted")
    print(f"{'':3s} {'CODEPOINT':10s} {'CAT':4s} {'COUNT':>6s}  {'VERDICT':18s} NAME / WHERE")
    for ok, ch, n, kind in rows:
        name = unicodedata.name(ch, "<unnamed>")
        verdict = "allowlisted" if ok else kind
        mark = " " if ok else "!"
        print(f"{mark:3s} U+{ord(ch):04X}{'':4s} {unicodedata.category(ch):4s} {n:6d}  "
              f"{verdict:18s} {name}")
        for loc in where[ch]:
            print(f"{'':36s}{loc}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tools/ascii_guard.py", description=__doc__.splitlines()[0])
    p.add_argument("paths", nargs="*", help="files or directories to scan (default: the whole repo)")
    p.add_argument("--check", action="store_true", help="scan and exit non-zero on a banned character")
    p.add_argument("--fix", action="store_true",
                   help="rewrite the unambiguous substitutions (never emoji)")
    p.add_argument("--staged", action="store_true", help="scan the git INDEX rather than paths")
    p.add_argument("--pushed", action="store_true", help="scan a push range, refs read from stdin")
    p.add_argument("--warn-only", action="store_true", help="report but always exit 0")
    p.add_argument("--inventory", action="store_true",
                   help="list EVERY non-ASCII character present, with counts and files, and say "
                        "whether the allowlist permits it. Reports; never fails.")
    p.add_argument("--fail-on", metavar="KIND[,KIND]",
                   help="fail ONLY on these kinds; everything else is still reported. Kinds: "
                        + ", ".join(_BANNED))
    args = p.parse_args(argv)

    if args.fix and (args.staged or args.pushed):
        print("ascii_guard: --fix works on the working tree, not on the index or a push range",
              file=sys.stderr)
        return 2
    if args.staged and args.pushed:
        print("ascii_guard: pick one of --staged or --pushed", file=sys.stderr)
        return 2

    if args.fix:
        print("ascii_guard: 0 finding(s) suppressed by allowlist")
        changed, emoji_left = fix_files(walk(args.paths or ["."]))
        for c in changed:
            print(f"fixed {c}")
        print(f"ascii_guard: rewrote {len(changed)} file(s)")
        if emoji_left:
            print(f"ascii_guard: {emoji_left} emoji left in place -- removing one is a product decision")
        return 0

    if args.inventory:
        return inventory(args.paths or ["."])

    if args.staged:
        hits = staged_hits()
    elif args.pushed:
        hits = pushed_hits(sys.stdin.read())
    else:
        hits = []
        for f in scan_scope(args.paths or ["."]):
            hits.extend(scan_file(f))
    if hits is None:
        return 2
    hits, suppressed = suppress_allowlisted(hits, load_allowlist())
    fail_on = None
    if args.fail_on:
        fail_on = {k.strip() for k in args.fail_on.split(",") if k.strip()}
        unknown = sorted(fail_on - set(_BANNED))
        if unknown:
            print(f"ascii_guard: unknown kind(s) for --fail-on: {', '.join(unknown)}", file=sys.stderr)
            return 2
    return report(hits, warn_only=args.warn_only, suppressed=suppressed, fail_on=fail_on)


if __name__ == "__main__":
    sys.exit(main())
