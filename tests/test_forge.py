import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import Mock

import aiohttp
import pytest

from shazzzbot import forge, forgejo, github, panel

TOKEN = "fixture-" + "forge-token"
DATE = "2026-11-01T12:00:00Z"
AT = datetime(2026, 11, 1, 12, tzinfo=UTC).timestamp()


class Response:
    def __init__(self, value=None, *, status=200, body=None, length=None, error=None):
        self.status = status
        self.content_length = length
        self.body = json.dumps(value).encode() if body is None else body
        self.error = error
        self.content = self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


def api(response=None):
    session = Mock()
    session.get.return_value = response or Response({"ok": True})
    return forge.API(session, "https://forge.example", TOKEN, "token")


def test_get_is_read_only_bounded_and_cached_including_concurrent_calls():
    client = api()
    async def go():
        values = await asyncio.gather(client.get("/repos/a/b"), client.get("/repos/a/b"))
        assert values == [{"ok": True}, {"ok": True}]
    asyncio.run(go())
    headers = {"Authorization": "token " + TOKEN, "Accept": "application/json"}
    client.session.get.assert_called_once_with("https://forge.example/repos/a/b", params=None, headers=headers,
                                               timeout=forge.TIMEOUT, allow_redirects=False)


@pytest.mark.parametrize("response,message", [(Response(status=401), "auth failed"),
                                             (Response(status=403), "auth failed"),
                                             (Response(status=302), "HTTP 302"),
                                             (Response(status=500), "HTTP 500"),
                                             (Response(body=b"not JSON"), "invalid data"),
                                             (Response(body=b"[" * 10000), "invalid data"),
                                             (Response(body=b"\xff"), "invalid data"),
                                             (Response(length=forge.BODY_LIMIT + 1), "response too large"),
                                             (Response(body=b"x" * (forge.BODY_LIMIT + 1)), "response too large"),
                                             (Response(error=TimeoutError()), "timeout"),
                                             (Response(error=aiohttp.ClientError(TOKEN)), "unreachable")])
def test_failures_are_safe_and_not_retried_inside_a_cycle(response, message):
    client = api(response)
    async def go():
        for _ in range(2):
            with pytest.raises(forge.Problem, match=message) as error:
                await client.get("/repos/a/b")
            assert TOKEN not in str(error.value)
    asyncio.run(go())
    assert client.session.get.call_count == 1


def test_invalid_auth_stops_other_requests_but_forbidden_scope_does_not():
    async def go():
        client = api(Response(status=401))
        for path in ("/one", "/two"):
            with pytest.raises(forge.Problem, match="auth failed"):
                await client.get(path)
        assert client.session.get.call_count == 1
        client = api(Response(status=403))
        with pytest.raises(forge.Problem):
            await client.get("/one")
        client.session.get.return_value = Response({"ok": True})
        assert await client.get("/two") == {"ok": True}
    asyncio.run(go())


@pytest.mark.parametrize("value", ["", " ", "a\nb", "x" * 4097, chr(0xE9)])
def test_tokens_must_be_safe_header_values(value):
    with pytest.raises(forge.Problem, match="auth failed"):
        forge.credential(value)


def test_remote_text_is_redacted_before_truncation_and_cannot_break_code_blocks_or_mention():
    client = api()
    assert TOKEN not in client.text(TOKEN + " extra")
    assert client.text("`@everyone`\nsecond line") == "'(at)everyone'"
    assert client.text("\x1bhello").strip() == "hello"
    assert client.text("") == "" and forge.clean("abc", "") == "abc"
    assert len(client.text("x" * 1000)) == 80
    with pytest.raises(forge.Problem):
        client.text(None)


@pytest.mark.parametrize("value", [None, [], "bad"])
def test_objects_are_validated(value):
    with pytest.raises(forge.Problem):
        forge.obj(value)


@pytest.mark.parametrize("value", [None, {}, [None], [{}] * 101])
def test_lists_are_bounded_and_validated(value):
    with pytest.raises(forge.Problem):
        forge.items(value)


@pytest.mark.parametrize("value", [-1, True, "1", None, 10**10])
def test_counts_cannot_be_guessed_or_unbounded(value):
    with pytest.raises(forge.Problem):
        forge.count(value)


def test_times_are_explicitly_zoned_or_unknown():
    assert forge.stamp(DATE) == AT
    assert all(forge.stamp(value) is None for value in (None, "bad", "2026-11-01T12:00:00"))


@pytest.mark.parametrize("value,state,level", [("success", "pass", panel.FINE), ("neutral", "pass", panel.FINE),
                                              ("failure", "fail", panel.CRITICAL),
                                              ("timed_out", "fail", panel.CRITICAL),
                                              ("cancelled", "cancelled", panel.FINE),
                                              ("skipped", "cancelled", panel.FINE),
                                              ("running", "running", panel.FINE),
                                              ("waiting", "running", panel.FINE),
                                              ("in_progress", "running", panel.FINE)])
def test_ci_states_are_normalized(value, state, level):
    assert forge.ci(value) == (state, level)


def test_unknown_ci_state_is_not_reported_as_a_pass():
    with pytest.raises(forge.Problem):
        forge.ci("surprise")


@pytest.mark.parametrize("provider,params,time_key", [(forgejo.Client, {"page": 1, "limit": 1}, "updated"),
                                                    (github.Client, {"page": 1, "per_page": 1}, "updated_at")])
def test_latest_ci_uses_the_documented_endpoints_and_fields(provider, params, time_key):
    row = {"status": "completed" if provider == github.Client else "failure", "conclusion": "failure", time_key: DATE}
    client = api(Response({"total_count": 1, "workflow_runs": [row]}))
    entries = asyncio.run(provider().ci(client, "owner/repo"))
    assert entries == [forge.Entry("owner/repo", ("fail",), AT, panel.CRITICAL)]
    assert client.session.get.call_args.args == ("https://forge.example/repos/owner/repo/actions/runs",)
    assert client.session.get.call_args.kwargs["params"] == params
    client = api(Response({"total_count": 0, "workflow_runs": []}))
    assert asyncio.run(provider().ci(client, "owner/repo"))[0].values == ("no runs",)


def test_github_in_progress_does_not_use_an_absent_conclusion():
    client = api(Response({"total_count": 1, "workflow_runs": [{"status": "in_progress", "updated_at": DATE}]}))
    assert asyncio.run(github.Client().ci(client, "owner/repo"))[0].values == ("running",)


@pytest.mark.parametrize("provider,params", [
    (forgejo.Client, {"page": 1, "limit": 3, "stat": "false", "verification": "false", "files": "false"}),
    (github.Client, {"page": 1, "per_page": 3})])
def test_commits_are_short_redacted_first_lines_with_commit_dates(provider, params):
    rows = [{"sha": "a" * 40, "commit": {"message": TOKEN + " title\nbody", "committer": {"date": DATE}}}] * 4
    client = api(Response(rows))
    entries = asyncio.run(provider().pushes(client, "owner/repo"))
    assert len(entries) == 3 and entries[0] == forge.Entry("owner/repo aaaaaaa", ("[redacted] title",), AT)
    assert TOKEN not in repr(entries) and "body" not in repr(entries)
    assert client.session.get.call_args.kwargs["params"] == params
    client = api(Response([]))
    assert asyncio.run(provider().pushes(client, "owner/repo"))[0].values == ("no commits",)


@pytest.mark.parametrize("provider", [forgejo.Client, github.Client])
def test_empty_repositories_return_normal_commit_rows_on_conflict(provider):
    client = api(Response(status=409))

    async def go():
        for _ in range(2):
            assert await provider().pushes(client, "owner/repo") == [forge.Entry("owner/repo", ("no commits",))]

    asyncio.run(go())
    client.session.get.assert_called_once()
    assert client.session.get.call_args.args == ("https://forge.example/repos/owner/repo/commits",)


@pytest.mark.parametrize("provider", [forgejo.Client, github.Client])
@pytest.mark.parametrize("status", [403, 500])
def test_commit_failures_other_than_empty_repository_conflicts_still_raise(provider, status):
    client = api(Response(status=status))
    with pytest.raises(forge.Problem) as error:
        asyncio.run(provider().pushes(client, "owner/repo"))
    assert error.value.status == status
    client.session.get.assert_called_once()


def test_empty_commit_messages_are_explicit_and_invalid_shas_fail_closed():
    client = api(Response([{"sha": "b" * 64, "commit": {"message": "", "committer": {"date": "bad"}}}]))
    entry = asyncio.run(forgejo.Client().pushes(client, "owner/repo"))[0]
    assert entry.values == ("(empty message)",) and entry.at is None
    client = api(Response([{"sha": "../wrong"}]))
    with pytest.raises(forge.Problem):
        asyncio.run(forgejo.Client().pushes(client, "owner/repo"))


def test_a_single_large_webhook_payload_is_bounded_but_not_mistaken_for_a_full_run_list():
    row = {"status": "failure", "updated": DATE, "event_payload": "x" * 300000}
    client = api(Response({"total_count": 1, "workflow_runs": [row]}))
    result = asyncio.run(forgejo.Client().ci(client, "owner/repo"))
    assert result[0].values == ("fail",) and result[0].level == panel.CRITICAL
    client.session.get.assert_called_once()
    assert client.session.get.call_args.kwargs["params"] == {"page": 1, "limit": 1}
    assert forge.TIMEOUT.total == 10


def test_display_redacts_endpoint_and_token_before_clipping():
    client = api()
    assert client.text("forge.example https://forge.example " + TOKEN) == "[redacted] [redacted] [redacted]"
    assert forge.display("abc localhost secret", "localhost secret", "http://localhost", 80) == "abc [redacted]"
    assert forge.display("message", "", "", 80) == "message"


def test_forgejo_counts_use_separate_repo_fields_and_runner_inventory_is_not_guessed():
    client = api(Response({"open_issues_count": 3, "open_pr_counter": 2}))
    assert asyncio.run(forgejo.Client().issues(client, "owner/repo"))[0].values == ("3", "2")
    assert client.session.get.call_args.args == ("https://forge.example/repos/owner/repo",)
    client.session.get.reset_mock()
    runner = asyncio.run(forgejo.Client().runners(client))[0]
    assert runner.values == ("runner API unavailable",) and runner.level == panel.WARNING
    client.session.get.assert_not_called()


def test_github_counts_issues_and_prs_separately_and_refuses_incomplete_results():
    client = api()
    client.session.get.side_effect = [Response({"total_count": 7, "incomplete_results": False}),
                                      Response({"total_count": 2, "incomplete_results": False}), Response({})]
    assert asyncio.run(github.Client().issues(client, "owner/repo"))[0].values == ("7", "2")
    assert [call.kwargs["params"]["q"] for call in client.session.get.call_args_list[:2]] == [
        "repo:owner/repo is:open is:issue", "repo:owner/repo is:open is:pr"]
    assert client.session.get.call_args.args == ("https://forge.example/repos/owner/repo",)
    client = api(Response({"total_count": 7, "incomplete_results": True}))
    with pytest.raises(forge.Problem):
        asyncio.run(github.Client().issues(client, "owner/repo"))


@pytest.mark.parametrize("provider", [forgejo.Client, github.Client])
@pytest.mark.parametrize("metadata,expected", [({}, None), ({"updated_at": None}, None),
    ({"updated_at": "bad"}, None), ({"updated_at": "2026-11-01T12:00:00"}, None), ({"updated_at": DATE}, AT)])
def test_issue_counts_carry_repository_activity_and_reuse_the_cached_metadata(provider, metadata, expected):
    data = {"open_issues_count": 7, "open_pr_counter": 2, "pushed_at": "2026-10-01T12:00:00Z", **metadata}
    client = api(Response(data))
    if provider is github.Client:
        client.session.get.side_effect = [Response({"total_count": 7, "incomplete_results": False}),
                                          Response({"total_count": 2, "incomplete_results": False}), Response(data)]

    async def go():
        entries = await provider().issues(client, "owner/repo")
        assert await provider().issues(client, "owner/repo") == entries
        return entries

    assert asyncio.run(go()) == [forge.Entry("owner/repo", ("7", "2"), expected)]
    calls = client.session.get.call_args_list
    assert len(calls) == (3 if provider is github.Client else 1)
    assert sum(call.args == ("https://forge.example/repos/owner/repo",) for call in calls) == 1


def run_row(provider, minute, state="success", run_id=1):
    date = f"2026-11-01T12:{minute:02d}:00Z"
    if provider == forgejo.Client:
        return {"id": run_id, "status": state, "created": date, "stopped": date,
                "updated": "2026-10-30T12:00:00Z"}
    return {"id": run_id, "status": "completed", "conclusion": state, "created_at": date,
            "run_started_at": date, "updated_at": date}


@pytest.mark.parametrize("provider,size_key", [(forgejo.Client, "limit"), (github.Client, "per_page")])
@pytest.mark.parametrize("ascending", [False, True])
def test_ci_checks_both_single_row_ends_and_uses_timestamps_not_position(provider, size_key, ascending):
    old, new = run_row(provider, 0, "failure"), run_row(provider, 5)
    first, last = (old, new) if ascending else (new, old)
    client = api()
    client.session.get.side_effect = [Response({"total_count": 10000, "workflow_runs": [first]}),
                                      Response({"total_count": 10000, "workflow_runs": [last]})]
    async def go():
        entries = await provider().ci(client, "owner/repo")
        assert entries == [forge.Entry("owner/repo", ("pass",), AT + 300)]
        assert await provider().ci(client, "owner/repo") == entries
    asyncio.run(go())
    assert [call.kwargs["params"] for call in client.session.get.call_args_list] == [
        {"page": 1, size_key: 1}, {"page": 10000, size_key: 1}]


@pytest.mark.parametrize("provider", [forgejo.Client, github.Client])
def test_ci_selects_newest_timestamp_from_unsorted_results_and_ties_use_run_id(provider):
    rows = [run_row(provider, 0, "failure"), run_row(provider, 5), run_row(provider, 1, "failure")]
    client = api(Response({"total_count": 3, "workflow_runs": rows}))
    assert asyncio.run(provider().ci(client, "owner/repo")) == [forge.Entry("owner/repo", ("pass",), AT + 300)]
    client.session.get.assert_called_once()
    rows = [run_row(provider, 5, "failure", 22000000001), run_row(provider, 5, run_id=22000000002)]
    client = api(Response({"total_count": 2, "workflow_runs": rows}))
    assert asyncio.run(provider().ci(client, "owner/repo"))[0].values == ("pass",)


@pytest.mark.parametrize("body", [{"workflow_runs": []}, {"total_count": 1, "workflow_runs": []},
                                  {"total_count": 0, "workflow_runs": [{"status": "success"}]},
                                  {"total_count": 1, "workflow_runs": [{"id": "bad", "status": "success"}]},
                                  {"total_count": 1, "workflow_runs": [{"id": -1, "status": "success"}]},
                                  {"total_count": 2, "workflow_runs": [{"status": "success"},
                                                                        {"status": "failure"}]}])
def test_inconsistent_counts_ids_and_unknown_candidate_times_do_not_claim_a_latest_run(body):
    with pytest.raises(forge.Problem, match="invalid data"):
        asyncio.run(forgejo.Client().ci(api(Response(body)), "owner/repo"))


def test_a_disappearing_tail_run_does_not_fall_back_to_an_old_first_row():
    client = api()
    client.session.get.side_effect = [Response({"total_count": 2, "workflow_runs": [run_row(forgejo.Client, 0)]}),
                                      Response({"total_count": 0, "workflow_runs": []})]
    with pytest.raises(forge.Problem, match="invalid data"):
        asyncio.run(forgejo.Client().ci(client, "owner/repo"))
