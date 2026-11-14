"""Recent log records for the preview page: a bounded buffer on the root logger, scrubbed of secrets first."""
import logging
import os
import re
from collections import deque
from typing import Any

SIZE = 2000
REDACTED = "[redacted]"
_ENV = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|FINGERPRINT", re.IGNORECASE)
_AUTH = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:(?:basic|bearer|token)\s+)?[^\s\"',}]+")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_QUERY = re.compile(r"(?i)([?&][^=&\s#]*(?:key|token|pass|secret|auth|sig)[^=&\s#]*=)[^&\s#\"']+")
_SKIP = "preview login, shown once"


def secrets_of(cfg: Any) -> set[str]:
    """Every secret value the bot holds: config secrets, relay keys, and secret-named environment variables."""
    found = {getattr(cfg, "token", ""), getattr(cfg, "jellyfin_key", ""),
             *(r.key for r in getattr(cfg, "relays", ()))}
    if getattr(cfg, "preview", None) is not None:
        found.add(cfg.preview.password_hash)
    found |= {v for k, v in os.environ.items() if _ENV.search(k)}
    return {s for s in found if isinstance(s, str) and len(s) >= 6}


class Buffer(logging.Handler):
    def __init__(self, secrets: set[str], size: int = SIZE) -> None:
        super().__init__(logging.DEBUG)
        self.secrets = sorted(secrets, key=len, reverse=True)
        self.records: deque[dict] = deque(maxlen=size)
        self.seq = 0

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, REDACTED)
        text = _AUTH.sub(r"\g<1>" + REDACTED, text)
        text = _USERINFO.sub(r"\g<1>" + REDACTED + "@", text)
        return _QUERY.sub(r"\g<1>" + REDACTED, text)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if text.startswith(_SKIP):
                return
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.seq += 1
            self.records.append({"seq": self.seq, "t": record.created, "level": record.levelname,
                                 "logger": record.name, "message": self.scrub(text)})
        except Exception:  # noqa: BLE001 -- a log handler must never raise into the code that logged. reviewed 2026-11
            self.handleError(record)

    def after(self, seq: int) -> list[dict]:
        return [r for r in list(self.records) if r["seq"] > seq]
