import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shazzzbot import SOURCE_URL
from shazzzbot.helper.constants import RELEASE_URL, REPO
from tools import setup_config, setup_detect

ROOT = Path(__file__).resolve().parents[1]


def test_python_repository_urls_share_one_identity():
    assert SOURCE_URL == setup_detect.SOURCE == f"https://github.com/{REPO}"
    assert RELEASE_URL == f"https://api.github.com/repos/{REPO}/releases/latest"
    assert setup_config.ICON_URL == f"https://raw.githubusercontent.com/{REPO}/main/assets/icon.png"


@pytest.mark.parametrize("name", ["REPO_URL", "RELEASE_API", "SOURCE_URL", "IMAGE"])
def test_standalone_setup_defaults_keep_the_canonical_repository(name):
    source = (ROOT / "setup.sh").read_text()
    found = re.search(r"^" + name + r'=\"\$\{SHAZZZBOT_[A-Z_]+:-([^}]+)\}\"$', source, re.MULTILINE)
    assert found is not None
    assert REPO in found[1]


@pytest.mark.parametrize("name", ["Repository", "Registry", "Support", "Project", "TemplateURL", "Icon"])
def test_unraid_repository_urls_keep_the_canonical_repository(name):
    value = ET.parse(ROOT / "unraid/shazzzbot.xml").findtext(name)
    assert value is not None and REPO in value


def test_isolated_wizard_resolves_the_shared_identity_from_its_application():
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/setup_config.py"), "--help"],
                            env={**os.environ, "SHAZZZBOT_APP": str(ROOT)}, capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "--examples" in result.stdout
