"""Container healthcheck. Each loop writes the time it must beat again by; `python -m shazzzbot.health` checks it."""
import os
import sys
import time


def _path() -> str:
    return os.environ.get("HEARTBEAT", "/data/heartbeat")


def beat(every: float) -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(time.time() + 3 * every + 30))
    os.replace(tmp, _path())


def ok() -> bool:
    try:
        with open(_path()) as f:
            return time.time() < float(f.read())
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(0 if ok() else 1)
