#!/usr/bin/env bash
# Runs the suite and mypy in a throwaway Python container, so no local Python setup is needed.
set -euo pipefail
cd "$(dirname "$0")"
img="$(sed -n 's/^FROM \([^ ]*\).*/\1/p' Dockerfile | head -1)"
cpus=$(nproc)
if ((cpus > 4)); then cpus=4; fi
exec docker run --rm --cpus "$cpus" --memory 3g -v "$PWD:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 -e COVERAGE_FILE=/tmp/.coverage "$img" sh -c \
  "{ [ -e /usr/bin/python3 ] || ln -s /usr/local/bin/python3 /usr/bin/python3; } && pip install -q --root-user-action=ignore -r requirements-dev.txt && python -m pytest -p no:cacheprovider $* && python -m mypy --cache-dir=/tmp/mypy shazzzbot"
