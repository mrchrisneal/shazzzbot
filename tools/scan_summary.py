#!/usr/bin/env python3
"""Summarise the forge scan job's reports in .scan/.

Fails when a scanner did not run, left an unreadable report, or reported a finding.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCAN = Path(".scan")
def pip_audit(d):
    return [f"{p['name']} {p['version']} {v['id']}" for p in d.get("dependencies", []) for v in p.get("vulns", [])]


def bandit(d):
    return [f"{r['filename']}:{r['line_number']} {r['issue_severity']} {r['test_id']} {r['issue_text'][:90]}"
            for r in d.get("results", []) if r["issue_severity"] in ("HIGH", "MEDIUM")]


def gitleaks(d):
    return [f"{r['File']}:{r['StartLine']} {r['RuleID']}" for r in d]


def trivy(d):
    out = []
    for res in d.get("Results") or []:
        t = res.get("Target")
        out += [f"{t} {v['PkgName']} {v['InstalledVersion']} {v['VulnerabilityID']} {v['Severity']}"
                for v in res.get("Vulnerabilities") or []]
        out += [f"{t}:{s['StartLine']} secret {s['RuleID']}" for s in res.get("Secrets") or []]
        out += [f"{t} misconfig {m['ID']} {m['Title']}" for m in res.get("Misconfigurations") or []]
    return out


def semgrep(d):
    out = [f"{r['path']}:{r['start']['line']} {r['extra']['severity']} {r['check_id']}" for r in d["results"]]
    return out + [f"TIMEOUT {e.get('path')}" for e in d.get("errors", []) if "timeout" in str(e.get("type")).lower()]


def shellcheck(d):
    return [f"{c['file']}:{c['line']} SC{c['code']} {c['level']}" for c in d["comments"]
            if c["level"] in ("error", "warning")]


def zizmor(d):
    return [f"{f['ident']} {f['desc'][:80]}" for f in d]


def osv(d):
    return [f"{p['package']['name']} {p['package']['version']} {v['id']}"
            for r in d.get("results") or [] for p in r.get("packages") or [] for v in p.get("vulnerabilities") or []]


def bearer(d):
    return [f"{k.upper()} {f['filename']}:{f['line_number']} {f['id']}" for k in ("critical", "high")
            for f in d.get(k) or []]


def ruff(d):
    return [f"{os.path.relpath(f['filename'])}:{f['location']['row']} {f['code']} {f['message'][:80]}" for f in d]


TOOLS = {
    "pip-audit": ("pip-audit.json", pip_audit),
    "osv": ("osv.json", osv),
    "bandit": ("bandit.json", bandit),
    "gitleaks": ("gitleaks.json", gitleaks),
    "trivy-fs": ("trivy-fs.json", trivy),
    "trivy-image": ("trivy-image.json", trivy),
    "semgrep": ("semgrep.json", semgrep),
    "shellcheck": ("shellcheck.json", shellcheck),
    "zizmor": ("zizmor.json", zizmor),
    "bearer": ("bearer.json", bearer),
    "ruff": ("ruff.json", ruff),
}


def main() -> int | str:
    counts, lines, trouble, gate = {}, [], [], []
    for tool, (report, parse) in TOOLS.items():
        if not (SCAN / f"{tool}.rc").is_file():
            counts[tool] = "DID-NOT-RUN"
            trouble.append(f"{tool} (never ran)")
            continue
        try:
            found = parse(json.loads((SCAN / report).read_text()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            counts[tool] = "BROKEN"
            trouble.append(f"{tool} (missing or unreadable report)")
            continue
        rc = (SCAN / f"{tool}.rc").read_text().strip()
        if rc != "0" and not found:
            counts[tool] = "FAILED"
            trouble.append(f"{tool} (exit {rc} with no findings)")
            continue
        counts[tool] = str(len(found))
        lines += [f"  {tool:<11} {f}" for f in found[:40]]
        if found:
            gate.append(f"{tool}: {len(found)}")
    summary = " ".join(f"{k}={v}" for k, v in counts.items())
    print("SCANNER SUMMARY: " + summary)
    print("\n".join(lines) if lines else "  (no findings)")
    (SCAN / "summary.line").write_text(summary)
    if trouble:
        return "SCANNER TROUBLE: " + ", ".join(trouble) + " -- a broken pipeline, not a clean result"
    if gate:
        return "NEW FINDINGS in gated tools: " + "; ".join(gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
