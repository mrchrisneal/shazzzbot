import shlex
from pathlib import Path


def test_direct_deploy_copies_the_required_root_version_file():
    root = Path(__file__).resolve().parents[1]
    command = next(line for line in (root / "deploy.sh").read_text().splitlines()
                   if line.startswith("rsync -rlt --delete "))
    assert "VERSION" in shlex.split(command)
