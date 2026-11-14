import asyncio
import json
from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import Mock

import aiohttp
import pytest

from shazzzbot import releases


class Response:
    def __init__(self, value=None, *, status=200, body=None, chunks=None, headers=None, error=None):
        self.status = status
        self.headers = headers or {}
        self.content = self
        self.body = json.dumps(value).encode() if body is None else body
        self.chunks = chunks
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, size):
        if self.chunks is not None:
            for chunk in self.chunks:
                yield chunk
        else:
            for start in range(0, len(self.body), size):
                yield self.body[start:start + size]


def fetch(response):
    http = Mock()
    http.get.return_value = response
    result = asyncio.run(releases.fetch(http, "example/project"))
    assert http.get.call_count == 1
    assert http.get.call_args.args == ("https://api.github.com/repos/example/project/releases/latest",)
    return result, http


@pytest.mark.parametrize("tag", ["v1.2.3", "1.2.3", "v0.0.0", "999999999.999999999.999999999"])
def test_fetch_returns_only_valid_release_tags(tag):
    result, _ = fetch(Response({"tag_name": tag, "irrelevant": "not rendered"}))
    assert result == releases.Check(tag, None)
    assert releases.TIMEOUT == 10


@pytest.mark.parametrize("value", [
    {}, {"tag_name": ""}, {"tag_name": None}, {"tag_name": 123}, {"tag_name": "v01.2.3"},
    {"tag_name": "v1.2.3-rc1"}, {"tag_name": "v1.2.3+build"}, {"tag_name": "1.2"},
    {"tag_name": "v1000000000.2.3"}, [], None, "v1.2.3", True,
])
def test_fetch_rejects_missing_noncanonical_and_nonobject_data(value):
    result, _ = fetch(Response(value))
    assert result == releases.Check(None, "invalid release data")


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b"{\"tag_name\":", b"[" * 2000 + b"]" * 2000])
def test_fetch_rejects_malformed_bodies(body):
    result, _ = fetch(Response(body=body))
    assert result == releases.Check(None, "invalid release data")


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500, 503])
def test_fetch_preserves_http_errors_and_retry_header(status):
    result, _ = fetch(Response(status=status, body=b"not JSON", headers={"Retry-After": "17.5"}))
    assert result == releases.Check(None, "HTTP " + str(status), 17.5)


def test_fetch_http_error_without_retry_header():
    result, _ = fetch(Response(status=403))
    assert result == releases.Check(None, "HTTP 403")


@pytest.mark.parametrize("error,message", [(TimeoutError(), "timeout"),
                                          (aiohttp.ClientError("private details"), "network error")])
def test_fetch_returns_clean_transport_errors(error, message):
    result, _ = fetch(Response(error=error))
    assert result == releases.Check(None, message)
    assert "private details" not in str(result)


def test_fetch_deadline_covers_a_stalled_response_body(monkeypatch):
    class Stalled(Response):
        async def iter_chunked(self, _size):
            await asyncio.Event().wait()
            yield b""

    monkeypatch.setattr(releases, "TIMEOUT", 0)
    result, _ = fetch(Stalled())
    assert result == releases.Check(None, "timeout")


def test_read_failure_retains_retry_after_from_the_received_headers():
    class Interrupted(Response):
        async def iter_chunked(self, _size):
            raise TimeoutError
            yield b""
    result, _ = fetch(Interrupted(headers={"Retry-After": "600"}))
    assert result == releases.Check(None, "timeout", 600)


def test_fetch_counts_bytes_across_chunks_before_parsing():
    chunks = [b" " * 16384] * 4 + [b"x"]
    result, _ = fetch(Response(chunks=chunks))
    assert result == releases.Check(None, "response too large")


def test_fetch_accepts_exact_cap_and_split_json():
    body = b'{"tag_name":"v1.2.3"}'
    chunks = [body[:5], body[5:13], body[13:], b" " * (65536 - len(body))]
    result, _ = fetch(Response(chunks=chunks))
    assert result == releases.Check("v1.2.3", None)


@pytest.mark.parametrize("raw,expected", [(None, 0), ("", 0), ("garbage", 0), ("-1", 0), ("0", 0),
                                        ("12.5", 12.5), ("7200", 3600), ("nan", 0), ("inf", 0),
                                        ("-inf", 0), ("1e99999", 0)])
def test_retry_after_numeric_invalid_and_nonfinite_values(raw, expected):
    assert releases.retry_after(raw, 1000) == expected


@pytest.mark.parametrize("offset,expected", [(-60, 0), (0, 0), (60, 60), (7200, 3600)])
def test_retry_after_http_dates_are_relative_and_bounded(offset, expected):
    now = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    raw = format_datetime(datetime.fromtimestamp(now + offset, UTC), usegmt=True)
    assert releases.retry_after(raw, now) == expected


def test_retry_after_invalid_calendar_date_is_ignored():
    assert releases.retry_after("Sun, 99 Nov 2026 00:00:00 GMT", 0) == 0


def test_retry_after_http_date_without_zone_uses_utc():
    now = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    assert releases.retry_after("Sun, 01 Nov 2026 00:01:00", now) == 60
