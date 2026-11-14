from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

from .constants import MAX_REQUEST, PROTOCOL


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


class ProtocolMismatch(ValueError):
    def __init__(self, received: int):
        self.expected = PROTOCOL
        self.received = received
        super().__init__("helper protocol mismatch")


class Verifier:
    def __init__(self, key: bytes):
        self.key = key
        self.nonces: dict[str, float] = {}

    def verify(self, raw: bytes) -> tuple[str, dict[str, Any]]:
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            raise ValueError("denied")
        obj = json.loads(raw)
        fields = {"action", "args", "ts", "nonce", "sig", "protocol"}
        if not isinstance(obj, dict) or set(obj) != fields:
            raise ValueError("denied")
        sig = obj.pop("sig")
        if not isinstance(sig, str) or re.fullmatch(r"[0-9a-f]{64}", sig) is None:
            raise ValueError("denied")
        if not isinstance(obj["action"], str) or not isinstance(obj["args"], dict):
            raise TypeError("denied")
        ts, nonce = obj["ts"], obj["nonce"]
        if type(ts) not in (int, float) or not 0 <= ts <= 10**12 or abs(time.time() - ts) > 30:
            raise ValueError("denied")
        if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce) is None:
            raise ValueError("denied")
        expected = hmac.new(self.key, canonical(obj), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("denied")
        protocol = obj["protocol"]
        if type(protocol) is not int or not 0 <= protocol <= 65535:
            raise ValueError("denied")
        now = time.time()
        self.nonces = {n: t for n, t in self.nonces.items() if now - t < 61}
        if nonce in self.nonces or len(self.nonces) >= 4096:
            raise ValueError("denied")
        self.nonces[nonce] = now
        if protocol != PROTOCOL:
            raise ProtocolMismatch(protocol)
        return obj["action"], obj["args"]
