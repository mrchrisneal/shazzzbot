"""One memory ceiling and worker lock for image renderers."""
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps

SCALE = 2
RENDER_BUDGET = 128 * 1024 * 1024
LOCK = threading.RLock()
log = logging.getLogger("shazzzbot")


class _Budget:
    def __init__(self) -> None:
        self.warned: set[str] = set()
        self.held = 0

    def check(self, key: str, required_bytes: object) -> bool:
        if fits(required_bytes):
            self.warned.discard(key)
            return True
        if key not in self.warned:
            self.warned.add(key)
            log.warning("image %s exceeds render budget (%s bytes; limit %d)", key, required_bytes, RENDER_BUDGET)
        return False


_budget = _Budget()


def fits(required_bytes: object) -> bool:
    return type(required_bytes) is int and 0 <= required_bytes <= available()


def available() -> int:
    with LOCK:
        return RENDER_BUDGET - _budget.held


@contextmanager
def retained(size: int) -> Iterator[None]:
    with LOCK:
        if not fits(size):
            raise ValueError("retained image bytes exceed the available render budget")
        _budget.held += size
        try:
            yield
        finally:
            _budget.held -= size


def check(key: str, required_bytes: object) -> bool:
    with LOCK:
        return _budget.check(key, required_bytes)


def serialized[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with LOCK:
            return func(*args, **kwargs)
    return wrapped
