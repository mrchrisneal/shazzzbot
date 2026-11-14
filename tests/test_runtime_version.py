import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from zipimport import zipimporter

import pytest

from shazzzbot.helper import constants

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("data,expected", [(b"1.0.0", "1.0.0"), (b"1.2.3\n", "1.2.3"),
                                           (b"999999999.999999999.999999999\n", "999999999.999999999.999999999")])
def test_runtime_version_accepts_only_canonical_file_bytes(data, expected):
    assert constants.parse_version(data) == expected


@pytest.mark.parametrize("data", [b"", b"v1.0.0", b"01.0.0", b"1.0.0-beta", b"1.0.0+build", b"1.0",
                                  b"1.0.0\n\n", b"1.0.0\r\n", b"1.0.0 ", b" 1.0.0", b"1.0.0\xff",
                                  b"1.0.0\x00", b"1000000000.0.0", "1.\u0660.0".encode()])
def test_runtime_version_refuses_noncanonical_or_non_ascii_file_bytes(data):
    with pytest.raises(ValueError, match="VERSION must contain canonical"):
        constants.parse_version(data)


def test_runtime_source_reads_only_its_project_version(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "__file__", str(tmp_path / "shazzzbot/helper/constants.py"))
    monkeypatch.setattr(constants, "__loader__", None)
    path = tmp_path / "VERSION"
    with pytest.raises(ValueError, match="missing or unreadable"):
        constants.read_version()
    path.write_bytes(b"3.4.5\n")
    assert constants.read_version() == "3.4.5"
    path.write_bytes(b"3.4.5\n" + b"x" * 100)
    with pytest.raises(ValueError, match="canonical"):
        constants.read_version()


@pytest.mark.parametrize("data", [None, b"4.5.6\n", b"bad"])
def test_runtime_zip_reads_the_fixed_root_version_member(tmp_path, monkeypatch, data):
    archive = tmp_path / "helper.pyz"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("helper/__init__.py", "")
        if data is not None:
            target.writestr("VERSION", data)
    monkeypatch.setattr(constants, "__loader__", zipimporter(str(archive)))
    if data == b"4.5.6\n":
        assert constants.read_version() == "4.5.6"
    else:
        message = "missing or unreadable" if data is None else "canonical"
        with pytest.raises(ValueError, match=message):
            constants.read_version()


@pytest.mark.parametrize("mode", ["source", "zip"])
@pytest.mark.parametrize("data", [None, b"not a release\n"])
def test_import_fails_closed_without_a_valid_version_file(tmp_path, mode, data):
    project = tmp_path / "project"
    helper = project / "shazzzbot/helper"
    helper.mkdir(parents=True)
    for name in ("__init__.py", "constants.py"):
        shutil.copyfile(ROOT / "shazzzbot/helper" / name, helper / name)
    shutil.copyfile(ROOT / "shazzzbot/__init__.py", project / "shazzzbot/__init__.py")
    if mode == "zip":
        source = tmp_path / "helper.pyz"
        with zipfile.ZipFile(source, "w") as target:
            for path in helper.glob("*.py"):
                target.write(path, "helper/" + path.name)
            if data is not None:
                target.writestr("VERSION", data)
        module = "helper"
    else:
        source, module = project, "shazzzbot"
        if data is not None:
            (project / "VERSION").write_bytes(data)
    command = f"import sys; sys.path.insert(0,sys.argv[1]); import {module}"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command, str(source)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert "VERSION" in result.stderr
    assert ("missing or unreadable" if data is None else "canonical") in result.stderr
