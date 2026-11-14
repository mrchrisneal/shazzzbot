import json
import runpy

import pytest

from tools import scan_summary as summary


def test_scanner_report_formats_preserve_relevant_findings():
    assert summary.pip_audit({"dependencies": [{"name": "p", "version": "1", "vulns": [{"id": "v"}]}]}) == ["p 1 v"]
    assert summary.bandit({"results": [{"filename": "f", "line_number": 2, "issue_severity": level,
                                        "test_id": "B1", "issue_text": "unsafe"} for level in ("LOW", "HIGH")]}) == [
                                            "f:2 HIGH B1 unsafe"]
    assert summary.gitleaks([{"File": "f", "StartLine": 2, "RuleID": "key"}]) == ["f:2 key"]
    assert summary.trivy({"Results": [
        {"Target": "image", "Vulnerabilities": [{"PkgName": "p", "InstalledVersion": "1",
                                                  "VulnerabilityID": "v", "Severity": "HIGH"}],
         "Secrets": [{"StartLine": 2, "RuleID": "key"}], "Misconfigurations": [{"ID": "m", "Title": "unsafe"}]},
        {}]}) == ["image p 1 v HIGH", "image:2 secret key", "image misconfig m unsafe"]
    assert summary.semgrep({"results": [{"path": "f", "start": {"line": 2},
                                        "extra": {"severity": "ERROR"}, "check_id": "rule"}],
                            "errors": [{"path": "f", "type": "Timeout"}, {"type": "other"}]}) == [
                                "f:2 ERROR rule", "TIMEOUT f"]
    assert summary.shellcheck({"comments": [{"file": "f", "line": 2, "code": 1, "level": level}
                                             for level in ("info", "warning")]}) == ["f:2 SC1 warning"]
    assert summary.zizmor([{"ident": "rule", "desc": "unsafe"}]) == ["rule unsafe"]
    assert summary.osv({"results": [{"packages": [{"package": {"name": "p", "version": "1"},
                                                  "vulnerabilities": [{"id": "v"}]}]}, {}]}) == ["p 1 v"]
    assert summary.bearer({"critical": [{"filename": "f", "line_number": 2, "id": "rule"}]}) == [
        "CRITICAL f:2 rule"]
    assert summary.ruff([{"filename": "f", "location": {"row": 2}, "code": "E1", "message": "invalid"}]) == [
        "f:2 E1 invalid"]


@pytest.mark.parametrize("state", ["missing", "broken", "failed", "finding", "clean"])
def test_summary_fails_closed_for_missing_or_broken_scanners(tmp_path, monkeypatch, capsys, state):
    monkeypatch.setattr(summary, "SCAN", tmp_path)
    monkeypatch.setattr(summary, "TOOLS", {"one": ("report.json", lambda value: value["results"])})
    if state != "missing":
        (tmp_path / "one.rc").write_text("1" if state == "failed" else "0")
    if state not in {"missing", "broken"}:
        (tmp_path / "report.json").write_text(json.dumps({"results": ["unsafe"] if state == "finding" else []}))
    result = summary.main()
    assert (result == 0) == (state == "clean")
    assert "SCANNER SUMMARY:" in capsys.readouterr().out and (tmp_path / "summary.line").exists()


def test_summary_entry_point_is_read_only_except_its_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".scan").mkdir()
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(summary.__file__, run_name="__main__")
    assert "SCANNER TROUBLE" in str(caught.value.code)


