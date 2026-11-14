from pathlib import Path

import pytest

from tests import test_setup_runtime_layout

shell = test_setup_runtime_layout.shell
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a" * 40
IMAGE = "sha256:" + "c" * 64

PREPARE = r'''
METHOD=compose-build SOURCE_DIR="$STATE_ROOT/source" REF=REVISION
INSTALL_ID=test-install DEV_IMAGE=private:dev PREV_IMAGE=private:prev
mkdir -p "$SOURCE_DIR"
fs() { [ "$1" = read ] && cat -- "$2"; }
docker() {
  trace "$*"
  case "$1" in
    image) return 1 ;;
    build) printf 'BUILT_DIGEST\n' ;;
  esac
}
bounded() {
  while [ "$1" != docker ]; do shift; done
  "$@"
}
'''.replace("REVISION", REVISION).replace("BUILT_DIGEST", IMAGE)


def test_compose_build_labels_the_version_file_and_keeps_the_revision(shell, tmp_path):
    version = (ROOT / "VERSION").read_text().strip()
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text(version + "\n")
    response, trace = shell(PREPARE + '\nensure_image\n')
    assert response.returncode == 0, response.stderr
    build = next(line for line in trace if line.startswith("build "))
    assert f"--label org.opencontainers.image.version={version}" in build
    assert f"--label org.opencontainers.image.revision={REVISION}" in build
    assert "--label org.shazzzbot.install=test-install" in build
    assert trace[-1] == f"tag {IMAGE} private:dev"


@pytest.mark.parametrize("value", [None, "", "v1.0.0", "01.0.0", "1.0", "1.0.0\nextra", "1.0.0 --label other=x"])
def test_invalid_build_version_refuses_before_any_docker_operation(shell, tmp_path, value):
    source = tmp_path / "source"
    source.mkdir()
    if value is not None:
        (source / "VERSION").write_text(value)
    response, trace = shell(PREPARE + '\nensure_image\n')
    assert response.returncode != 0 and "VERSION" in response.stderr
    assert trace == []


def test_a_ready_build_does_not_read_or_rebuild_the_image(shell):
    response, trace = shell('''
IMAGE_READY=1 METHOD=compose-build
fs() { trace unexpected-read; return 1; }
docker() { trace unexpected-docker; return 1; }
ensure_image
''')
    assert response.returncode == 0, response.stderr
    assert trace == []
