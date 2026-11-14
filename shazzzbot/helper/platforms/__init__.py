from pathlib import Path

from .base import Platform
from .unraid import Unraid

UNRAID_VERSION = Path("/etc/unraid-version")


def detect() -> Platform:
    if UNRAID_VERSION.is_file():
        return Unraid()
    raise SystemExit("unsupported host platform: Unraid marker /etc/unraid-version not found")
