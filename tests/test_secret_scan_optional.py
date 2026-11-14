import runpy
import sys
from types import SimpleNamespace

import pytest

secrets = pytest.importorskip("tools.secret_scan", reason="local secret scanner is not part of the release")


@pytest.mark.parametrize("value", ["", "none", "....", "xxxxx", "${ENV}", "$ENV", "{{ value }}", "%s",
                                   "%(name)s", "<value>", "CONFIG_KEY", "some description", "fixture-token"])
def test_secret_placeholders_are_not_credentials(value):
    assert secrets.is_placeholder_value(value)
    assert not secrets.looks_like_real_secret_token(value)


def test_secret_token_entropy_distinguishes_generated_values_from_fixtures():
    assert secrets.shannon_entropy("") == 0
    assert secrets.shannon_entropy("ab") == 1
    assert not secrets.looks_like_real_secret_token("shortValue1")
    assert secrets.looks_like_real_secret_token("0123456789abcdef" * 2)
    assert secrets.looks_like_real_secret_token("0123456789abcdefghijklmnopQRSTUV")
    assert not secrets.looks_like_real_secret_token("abcdefghijABCDEFGHIJ")
    assert not secrets.looks_like_real_secret_token("a" * 22 + "B1")
    assert not secrets.looks_like_real_secret_token("notAllowed!" * 4)


@pytest.mark.parametrize("address,kind", [("invalid", None), ("::1", None), ("192.0.2.1", None),
                                         ("127.0.0.1", None), ("0.0.0.0", None), ("169.254.1.1", None),
                                         ("224.0.0.1", None), ("240.0.0.1", None),
                                         ("100.64.0.1", "CGNAT-TAILSCALE"),
                                         ("10.0.0.1", "LAN-PRIVATE-IP"), ("8.8.8.8", None)])
def test_ip_classification(address, kind):
    assert secrets.classify_ip(address) == kind


def test_secret_scanning_checks_every_supported_type_without_real_secrets():
    text = "\n".join(["safe", "-----BEGIN " + "PRIVATE KEY-----", "999.999.999.999 192.0.2.1",
                      "100.64.0.1 10.0.0.1", "00:11:22:33:44:55 " + secrets.MAC_DOC_PLACEHOLDER,
                      "person@example.com person@invalid.test", "/home/user /home/privateperson",
                      secrets.OWNER_HOSTNAMES[0], "fake.ts.net", "scratch" + "pads",
                      "https://user:password@example.com https://${USER}:${PASS}@example.com",
                      "https://fakeuser:fakepassword@example.com", "ghp_" + "a" * 36,
                      'token="' + "0123456789abcdef" * 2 + '"', 'password="fakeValue1"', 'password="test"'])
    rows = secrets.scan_content("app.py", text)
    kinds = {row.rule for row in rows}
    assert {"PEM-PRIVATE-KEY", "CGNAT-TAILSCALE", "LAN-PRIVATE-IP", "MAC-ADDRESS", "EMAIL-ADDRESS",
            "HOME-DIR-PATH", "OWNER-HOSTNAME", "TAILNET-HOSTNAME", "BANNED-STRING-SCRATCHPADS", "USERPASS-URL",
            "GITHUB-TOKEN", "CRED-LONG", "CRED-SHORT-PASSWORD"} <= kinds
    assert secrets.scan_content("tests/test_fake.py", 'password="fakeValue1"') == []
    assert all(row.path == "app.py" and row.line > 0 and row.message and row.value for row in rows)


def test_allowlist_and_disk_sources_are_exact_and_binary_safe(tmp_path, monkeypatch):
    allow = tmp_path / "allowlist.txt"
    monkeypatch.setattr(secrets, "ALLOWLIST_PATH", allow)
    assert secrets.load_allowlist() == set()
    allow.write_text("# comment\n\nmalformed\nf | RULE | exact\n")
    assert secrets.load_allowlist() == {("f", "RULE", "exact")}
    clean, binary, picture = (tmp_path / name for name in ("file.py", "binary", "image.png"))
    clean.write_bytes(b"clean\xff")
    binary.write_bytes(b"\0binary")
    picture.write_bytes(b"data")
    assert list(secrets.iter_disk_files([str(tmp_path / "missing"), str(picture), str(binary), str(clean)])) == [
        (str(clean), "clean\ufffd")]


def test_git_sources_skip_unreadable_and_binary_blobs_without_touching_git(monkeypatch):
    answers = iter([b"\0image.png\0missing\0binary\0file.py\0", RuntimeError("missing"), b"\0data", b"clean"])
    def fake(args):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(secrets, "git", fake)
    assert list(secrets.iter_tree_files("commit")) == [("file.py", "clean")]


def test_secret_run_and_entry_points_are_fail_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(secrets, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(secrets, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")
    monkeypatch.setattr(secrets, "iter_tree_files", lambda ref: [("file.py", "clean")])
    assert secrets.run(None, None) == 0
    source = tmp_path / "source.py"
    source.write_text("clean")
    assert secrets.run(None, [str(source)]) == 0
    source.write_text("fake.ts.net")
    assert secrets.run(None, [str(source)]) == 1
    (tmp_path / "allowlist.txt").write_text(f"{source}|TAILNET-HOSTNAME|fake.ts.net")
    assert secrets.run(None, [str(source)]) == 0
    assert "allowlisted" in capsys.readouterr().out
    def failed(ref):
        raise RuntimeError("failed source")
    monkeypatch.setattr(secrets, "iter_tree_files", failed)
    assert secrets.run("commit", None) == 1
    monkeypatch.setattr(sys, "argv", ["secret_scan", "--paths", str(source)])
    assert secrets.main() == 0
    monkeypatch.setattr(secrets.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stderr=b"bad", stdout=b""))
    with pytest.raises(RuntimeError, match="failed"):
        secrets.git(["status"])
    monkeypatch.setattr(secrets.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr=b"", stdout=b"clean"))
    assert secrets.git(["status"]) == b"clean"
    monkeypatch.setattr(sys, "argv", ["secret_scan", "--paths", str(tmp_path / "missing")])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(secrets.__file__, run_name="__main__")
    assert caught.value.code == 0
