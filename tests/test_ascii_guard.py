import subprocess
import sys

GUARD = "tools/ascii_guard.py"


def check(*paths):
    return subprocess.run([sys.executable, GUARD, "--check", *paths], capture_output=True, text=True, check=False)


def test_the_guard_passes_its_own_check():
    assert check("tools").returncode == 0


def test_banned_characters_fail(tmp_path):
    for i, ch in enumerate(map(chr, (0x2014, 0x1F600, 0x201C, 0x200B))):
        f = tmp_path / f"f{i}.py"
        f.write_text(f"x = 1  # a{ch}b\n", encoding="utf-8")
        assert check(str(f)).returncode == 1, hex(ord(ch))
    clean = tmp_path / "ok.py"
    clean.write_text("x = 1  # a--b\n")
    assert check(str(clean)).returncode == 0
