import contextlib
import json
from types import SimpleNamespace

import pytest

from shazzzbot.helper import constants, updates


@pytest.mark.parametrize(("value", "expected"), [
    ("0.0.0", (0, 0, 0)), ("v0.0.0", (0, 0, 0)), ("1.2.3", (1, 2, 3)),
    ("v10.20.30", (10, 20, 30)), ("999999999.999999999.999999999", (999999999, 999999999, 999999999)),
])
def test_version_policy_accepts_only_bounded_canonical_releases(value, expected):
    assert constants.version_number(value) == expected


@pytest.mark.parametrize("value", [
    None, True, 123, [], {}, b"1.2.3", "", "1", "1.2", "1.2.3.4", "v", "vv1.2.3", "V1.2.3",
    "01.2.3", "1.02.3", "1.2.03", "v00.0.0", "-1.2.3", "+1.2.3", "1.-2.3", "1.2.-3",
    "1.2.3-rc1", "1.2.3+build", " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3\r", "1.2.3\x00",
    "1000000000.2.3", "1.1000000000.3", "1.2.1000000000", "9" * 5000 + ".2.3",
    "\u0661.2.3", "1.\uff12.3",
])
def test_version_policy_rejects_malformed_prerelease_and_unbounded_values(value):
    with pytest.raises(ValueError, match="canonical"):
        constants.version_number(value)


def test_helper_update_comparison_is_the_shared_policy():
    assert updates.version_number is constants.version_number
    assert constants.version_number("1.10.0") > constants.version_number("1.9.9")
    assert constants.version_number("v1.2.3") == constants.version_number("1.2.3")


@pytest.mark.parametrize(("tag", "expected"), [("v1.2.3", "1.2.3"), ("1.2.3", "1.2.3"),
                                                ("v01.2.3", None), ("1.2.3-rc1", None), ("vv1.2.3", None)])
def test_helper_release_discovery_uses_the_same_version_policy(monkeypatch, tag, expected):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://example.invalid/releases/latest"

        def read(self, limit):
            assert limit == 65537
            return json.dumps({"tag_name": tag}).encode()

    opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
    monkeypatch.setattr(updates.urllib.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(updates, "deadline", lambda seconds: contextlib.nullcontext())
    if expected is None:
        with pytest.raises(ValueError, match="canonical"):
            updates.latest_release("https://example.invalid/releases/latest")
    else:
        assert updates.latest_release("https://example.invalid/releases/latest") == expected
