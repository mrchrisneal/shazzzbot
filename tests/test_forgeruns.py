import asyncio
import re
from unittest.mock import patch

import pytest

from shazzzbot import forge, forgejo, forges, github, panel, view
from tests.test_forge import AT, TOKEN, Response, api

START = "2026-01-01T00:00:00Z"
END = "2026-01-01T00:12:00Z"


def run(git=False, state="success", **changes):
    row = ({"status": "completed", "conclusion": state, "name": "Tests", "run_number": 12,
            "run_started_at": START, "created_at": START, "updated_at": END} if git else
           {"status": state, "workflow_id": "tests.yaml", "index_in_repo": 12,
            "started": START, "created": START, "stopped": END})
    row.update(changes)
    return row


@pytest.mark.parametrize("git", [False, True])
def test_recent_runs_use_one_small_cached_fetch_for_ci_and_runs(git, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    rows = [run(git, "failure"), run(git, "success", **({"run_started_at": END} if git else {"started": END}))]
    session = api(Response({"total_count": 50 if git else 2, "workflow_runs": rows})).session
    settings = forges.Settings("github" if git else "forgejo", "https://forge.example", "TEST_FORGE_TOKEN",
                               ("owner/repo",), ("ci", "runs"))
    monitor = forges.Monitor(settings, 0)
    asyncio.run(monitor.refresh(session))
    assert session.get.call_count == 1
    assert session.get.call_args.kwargs["params"] == {"page": 1, "per_page" if git else "limit": 5}
    assert monitor.data["ci"][0].values == ("pass",)
    assert [entry.values[-1] for entry in monitor.data["runs"]] == ["pass", "fail"]
    section = monitor.section(1767227400)
    text = view.text(section).description
    rich = str(view.rich(section).to_dict())
    with patch.object(view, "_rows", wraps=view._rows) as drawn:
        assert view.image(section).startswith(b"\x89PNG")
    image_text = str([call.args[1].rows for call in drawn.call_args_list])
    for output in (text, rich, image_text):
        for value in ("Recent runs", "12", "pass", "fail", "12m"):
            if value != "Recent runs" or output is not image_text:
                assert value in output
    assert section.issues == []
    run_rows = next(part for part in section.parts if part.heading == "Recent runs").rows[1:]
    assert any(level == panel.CRITICAL and cells[2] == "fail" for level, cells in run_rows)
    assert all(len(re.sub(r"\x1b\[[0-9;]*m", "", line)) <= panel.WIDTH + 3
               for line in text.splitlines() if not line.startswith(("```", "**")))


@pytest.mark.parametrize("git", [False, True])
def test_empty_and_running_runs_keep_a_real_start_and_no_finished_time(git):
    client = github.Client() if git else forgejo.Client()
    empty = asyncio.run(client.runs(api(Response({"total_count": 0, "workflow_runs": []})), "o/r"))
    assert empty[0].values == ("no runs",)
    data = run(git, "in_progress" if git else "running")
    if git:
        data["status"] = "in_progress"
    result = asyncio.run(client.runs(api(Response({"total_count": 1, "workflow_runs": [data]})), "o/r"))[0]
    assert result.finished is None and result.values[-1] == "running"
    settings = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN", ("o/r",), ("runs",))
    monitor = forges.Monitor(settings, 0)
    monitor.data["runs"] = (result,)
    assert "1h" in view.text(monitor.section(result.at + 3600)).description


@pytest.mark.parametrize("git", [False, True])
@pytest.mark.parametrize("bad", ["start", "stop", "name", "number", "state", "backwards", "total", "empty", "many"])
def test_bad_run_fields_fail_closed(git, bad):
    row = run(git)
    if bad == "start":
        row.update({key: "bad" for key in ("run_started_at", "started", "created", "created_at")})
    elif bad == "stop":
        row["updated_at" if git else "stopped"] = "bad"
    elif bad == "name":
        row["name" if git else "workflow_id"] = ""
    elif bad == "number":
        row["run_number" if git else "index_in_repo"] = 0
    elif bad == "state":
        row["status"] = "bad"
    elif bad == "backwards":
        row["started" if not git else "run_started_at"] = "2026-01-02T00:00:00Z"
    rows = [] if bad == "empty" else [row] * (6 if bad == "many" else 1)
    total = 0 if bad == "total" else max(1, len(rows))
    client = github.Client() if git else forgejo.Client()
    with pytest.raises(forge.Problem):
        asyncio.run(client.runs(api(Response({"total_count": total, "workflow_runs": rows})), "o/r"))


def test_recent_runs_merge_repositories_by_start_time_and_keep_five(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/one", "o/two"), ("runs",))
    monitor = forges.Monitor(cfg, 0)
    async def one(_api, _kind, repo):
        offset = 1 if repo == "o/two" else 0
        return [forge.Entry(repo, (f"Tests #{i}", "pass"), AT + i * 2 + offset, finished=AT + 100) for i in range(5)]
    monitor._one = one
    asyncio.run(monitor.refresh(api().session))
    assert len(monitor.data["runs"]) == 10
    section = monitor.section(AT + 100)
    rows = [(level, cells) for level, cells in section.parts[0].rows[1:] if level != panel.MORE]
    assert section.parts[0].rows[-1] == (panel.MORE, ("5 additional runs not visible", "", "", "", ""))
    assert [cells[0] for _, cells in rows] == ["o/two", "o/one", "o/two", "o/one", "o/two"]
    assert [cells[1] for _, cells in rows] == ["Tests #4", "Tests #4", "Tests #3", "Tests #3", "Tests #2"]


def test_recent_run_fetch_problems_and_unknown_ages_render_cleanly_in_all_modes(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN", ("o/r",), ("runs",))
    monitor = forges.Monitor(cfg, 0)
    asyncio.run(monitor.refresh(api(Response(error=TimeoutError())).session))
    sec = monitor.section(AT)
    assert "could not read (timeout)" in view.text(sec).description
    assert "could not read (timeout)" in str(view.rich(sec).to_dict())
    assert view.image(sec).startswith(b"\x89PNG")
    monitor.data["runs"] = (forge.Entry("o/r", ("Tests #1", "running")),)
    assert monitor.section(AT).parts[0].rows[1][1][-2:] == ("-", "-")
    monitor.data["runs"] = (forge.Entry("o/r", ("Tests #1", "running"), AT + 10),)
    assert monitor.section(AT).parts[0].rows[1][1][-1] == "-"


def test_worst_case_run_names_fit_the_text_phone_width():
    repo = "owner/" + "r" * 100
    cfg = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN", (repo,), ("runs",))
    monitor = forges.Monitor(cfg, 0)
    monitor.data["runs"] = (forge.Entry(repo, ("long workflow " * 20, "cancelled"), 1,
                                       finished=1000000),)
    text = view.text(monitor.section(1767225600)).description
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines() if not line.startswith(("```", "**")))


def test_failed_repository_reads_stay_in_alerts_without_exceeding_five_recent_runs(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN", ("o/ok", "o/offline"), ("runs",))
    monitor = forges.Monitor(cfg, 0)
    async def one(_api, _kind, repo):
        if repo == "o/offline":
            return [forge.Entry(repo, ("could not read (timeout)",), level=panel.WARNING)]
        return [forge.Entry(repo, (f"Tests #{i}", "pass"), AT + i, finished=AT + 10) for i in range(5)]
    monitor._one = one
    asyncio.run(monitor.refresh(api().session))
    assert len(monitor.data["runs"]) == 6
    section = monitor.section(AT + 20)
    assert len(section.parts[0].rows) == 7
    assert section.parts[0].rows[-1] == (panel.MORE, ("1 additional run not visible", "", "", "", ""))
    assert all("timeout" not in " ".join(cells) for _, cells in section.parts[0].rows)
    assert section.issues == [(panel.WARNING, "GitHub Recent runs: o/offline could not read (timeout)")]
    assert "timeout" in view.text(section).description
    assert "timeout" in str(view.rich(section).to_dict())


def ascending_api(total, *, same_start=False, zero_times=False):
    from datetime import UTC, datetime, timedelta

    client = api()
    def page(_url, *, params, **_kwargs):
        first = (params["page"] - 1) * params["limit"] + 1
        rows = []
        for number in range(first, min(total + 1, first + params["limit"])):
            start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=0 if same_start else number)
            rows.append(run(state="cancelled" if zero_times else "success", index_in_repo=number,
                            created=start.isoformat(),
                            started="1969-12-31T16:00:00-08:00" if zero_times else start.isoformat(),
                            stopped="1969-12-31T16:00:00-08:00" if zero_times else
                            (start + timedelta(seconds=60)).isoformat()))
        return Response({"total_count": total, "workflow_runs": rows})
    client.session.get.side_effect = page
    return client


@pytest.mark.parametrize("total,pages", [(5, [1]), (6, [1, 2]), (10, [1, 2]), (11, [1, 3, 2]),
                                        (74, [1, 15, 14]), (75, [1, 15]),
                                        (999999999, [1, 200000000, 199999999])])
def test_forgejo_reads_only_the_tail_pages_and_returns_the_newest_five(total, pages):
    client = ascending_api(total)
    entries = asyncio.run(forgejo.Client().runs(client, "owner/repo"))
    assert [int(entry.values[0].rsplit("#", 1)[1]) for entry in entries] == list(range(total, total - 5, -1))
    assert [call.kwargs["params"] for call in client.session.get.call_args_list] == [
        {"page": page, "limit": 5} for page in pages]
    assert all(call.args[0].endswith("/repos/owner/repo/actions/runs") for call in client.session.get.call_args_list)
    assert len(entries) == 5 and entries[0].at >= entries[-1].at


def test_forgejo_latest_ci_and_recent_runs_share_the_cached_tail_fetches(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    client = ascending_api(74)
    settings = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("owner/repo",), ("ci", "runs"))
    monitor = forges.Monitor(settings, 0)
    asyncio.run(monitor.refresh(client.session))
    assert client.session.get.call_count == 3
    assert monitor.data["ci"][0].at == monitor.data["runs"][0].finished
    assert [entry.values[0] for entry in monitor.data["runs"]] == [f"tests.yaml #{n}" for n in range(74, 69, -1)]


@pytest.mark.parametrize("page", [14, 15])
@pytest.mark.parametrize("bad", ["empty", "over-limit", "bad-total", "bad-list", "total-type",
                                 "offset-total", "zero-empty"])
def test_every_forgejo_tail_page_keeps_the_row_and_total_validation(page, bad):
    client = ascending_api(74)
    original = client.session.get.side_effect
    def corrupt(url, *, params, **kwargs):
        if params["page"] != page:
            return original(url, params=params, **kwargs)
        rows = [run()] * (6 if bad == "over-limit" else 4)
        body = {"total_count": 74, "workflow_runs": rows}
        if bad == "empty":
            body["workflow_runs"] = []
        elif bad == "bad-total":
            body["total_count"] = 0
        elif bad == "bad-list":
            body["workflow_runs"] = None
        elif bad == "total-type":
            body["total_count"] = True
        elif bad == "offset-total":
            body["total_count"] = 6
        elif bad == "zero-empty":
            body.update(total_count=0, workflow_runs=[])
        return Response(body)
    client.session.get.side_effect = corrupt
    with pytest.raises(forge.Problem, match="invalid data"):
        asyncio.run(forgejo.Client().runs(client, "owner/repo"))


@pytest.mark.parametrize("stopped", [None, "1969-12-31T16:00:00-08:00", "1969-12-31T23:59:59Z"])
@pytest.mark.parametrize("started", [START, None, "1969-12-31T16:00:00-08:00"])
def test_forgejo_never_started_and_cancelled_runs_keep_created_time_and_unknown_duration(started, stopped):
    row = run(state="cancelled", started=started, stopped=stopped)
    entry = asyncio.run(forgejo.Client().runs(api(Response({"total_count": 1, "workflow_runs": [row]})), "o/r"))[0]
    assert entry.at == forge.stamp(START) and entry.finished is None
    assert entry.values == ("tests.yaml #12", "cancelled")


def test_worst_case_tail_pagination_handles_zero_dates_and_equal_created_times():
    client = ascending_api(74, same_start=True, zero_times=True)
    entries = asyncio.run(forgejo.Client().runs(client, "owner/repo"))
    assert [entry.values[0] for entry in entries] == [f"tests.yaml #{n}" for n in range(74, 69, -1)]
    assert all(entry.at == forge.stamp(START) and entry.finished is None for entry in entries)
    assert client.session.get.call_count == 3
    from shazzzbot import forgeruns

    latest = forgeruns.latest(entries)[0]
    assert latest.values == ("cancelled",) and latest.at == forge.stamp(START)


@pytest.mark.parametrize("stopped", ["not a timestamp", False, 0, [], {}])
def test_forgejo_malformed_end_timestamps_still_fail_closed(stopped):
    row = run(stopped=stopped)
    with pytest.raises(forge.Problem, match="invalid data"):
        asyncio.run(forgejo.Client().runs(api(Response({"total_count": 1, "workflow_runs": [row]})), "o/r"))


@pytest.mark.parametrize("updated", [None, "1969-12-31T16:00:00-08:00"])
def test_github_keeps_rejecting_missing_or_backwards_completed_timestamps(updated):
    row = run(True, updated_at=updated)
    with pytest.raises(forge.Problem, match="invalid data"):
        asyncio.run(github.Client().runs(api(Response({"total_count": 1, "workflow_runs": [row]})), "o/r"))


def test_github_keeps_the_api_order_for_equal_timestamps_across_workflows():
    rows = [run(True, name="New workflow", run_number=2), run(True, name="Older workflow", run_number=100)]
    client = api(Response({"total_count": 74, "workflow_runs": rows}))
    entries = asyncio.run(github.Client().runs(client, "o/r"))
    assert [entry.values[0] for entry in entries] == ["New workflow #2", "Older workflow #100"]
    client.session.get.assert_called_once()
    assert client.session.get.call_args.kwargs["params"] == {"page": 1, "per_page": 5}


@pytest.mark.parametrize("state,duration", [("success", "-"), ("failure", "-"), ("cancelled", "-"), ("running", "1h")])
def test_epoch_zero_ends_show_unknown_completed_durations_in_every_view_but_keep_running_elapsed(monkeypatch,
                                                                                              state, duration):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    row = run(state=state, started="1969-12-31T16:00:00-08:00", stopped="1969-12-31T16:00:00-08:00")
    entries = asyncio.run(forgejo.Client().runs(api(Response({"total_count": 1, "workflow_runs": [row]})), "o/r"))
    start = forge.stamp(START)
    assert start is not None
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/r",), ("runs",))
    monitor = forges.Monitor(cfg, 0)
    monitor.data["runs"] = tuple(entries)
    section = monitor.section(start + 3600)
    assert all(part.rows[1][1][-2:] == (duration, "1h ago") for part in section.parts)
    text = view.text(section).description
    assert re.search(rf"\b{entries[0].values[-1]}\s+{re.escape(duration)}\s+1h ago", text)
    rich = view.rich(section)
    assert any(re.search(rf"Duration:\s+{re.escape(duration)}\n", field.value) for field in rich.fields)
    drawn = []
    original = view.ImageDraw.ImageDraw.text
    def draw(image_draw, xy, value, **kwargs):
        drawn.append(value)
        return original(image_draw, xy, value, **kwargs)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", draw)
    assert view.image(section, now=start + 3600).startswith(b"\x89PNG")
    assert duration in drawn and "1h ago" in drawn
    if state != "running":
        assert "1h" not in drawn


@pytest.mark.parametrize("filtered", [False, True])
def test_runs_ignore_repository_pins_and_filter_before_the_run_limit(monkeypatch, filtered):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN",
                          ("o/pin", "o/new", "o/hidden"), ("runs",), latest=2, pinned=("o/pin",),
                          filter={"runs": ("o/new", "o/hidden")} if filtered else {})
    monitor = forges.Monitor(cfg, 0)
    monitor.data["runs"] = (forge.Entry("o/pin", ("Old #1", "pass"), AT - 100, finished=AT - 90),
                            forge.Entry("o/new", ("Newest #3", "pass"), AT, finished=AT + 10),
                            forge.Entry("o/new", ("Next #2", "pass"), AT - 1, finished=AT + 9),
                            forge.Entry("o/hidden", ("could not read (timeout)",), level=panel.WARNING))
    section = monitor.section(AT + 20)
    for part in section.parts:
        visible = [cells for level, cells in part.rows[1:] if level != panel.MORE]
        assert [cells[1] for cells in visible] == ["Newest #3", "Next #2"]
        assert [cells[0] for cells in visible] == ["o/new", "o/new"]
        summary = "1 additional run not visible" if filtered else "2 additional runs not visible"
        assert part.rows[-1] == (panel.MORE, (summary, "", "", "", ""))
    assert section.issues == [(panel.WARNING, "Forgejo Recent runs: o/hidden could not read (timeout)")]


@pytest.fixture
def active_runs_monitor(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    def make(entries, latest=5):
        repos = tuple(dict.fromkeys(entry.name for entry in entries if entry.name != "Status")) or ("o/repo",)
        cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", repos, ("runs",), latest=latest)
        monitor = forges.Monitor(cfg, 0)
        monitor.data["runs"] = tuple(entries)
        monitor.ready = True
        return monitor
    return make


@pytest.mark.parametrize("state", ["success", "failure", "running", "cancelled"])
def test_only_the_latest_workflow_run_affects_alerts_and_system_severity(active_runs_monitor, state):
    text, level = forge.ci(state)
    entries = [forge.Entry("o/repo", ("forge.yml #1", "fail"), AT - 100, panel.CRITICAL),
               forge.Entry("o/repo", ("forge.yml #2", "fail"), AT - 50, panel.CRITICAL),
               forge.Entry("o/repo", ("forge.yml #3", text), AT, level)]
    monitor = active_runs_monitor(entries)
    section = monitor.section(AT + 10)
    expected = [(panel.CRITICAL, "Forgejo Recent runs: o/repo forge.yml #3 fail")] if state == "failure" else []
    assert section.issues == expected
    assert monitor.summary()[0] == level
    rows = [row for row in section.parts[0].rows[1:] if row[0] != panel.MORE]
    assert [cells[1] for _, cells in rows] == ["forge.yml #3", "forge.yml #2", "forge.yml #1"]
    assert [row[0] for row in rows[1:]] == [panel.CRITICAL, panel.CRITICAL]
    assert monitor.data["runs"] == tuple(entries)


def test_latest_failures_are_independent_for_each_workflow(active_runs_monitor):
    entries = [forge.Entry("o/repo", ("one.yml #1", "fail"), AT - 100, panel.CRITICAL),
               forge.Entry("o/repo", ("two.yml #2", "fail"), AT - 20, panel.CRITICAL),
               forge.Entry("o/repo", ("one.yml #3", "fail"), AT, panel.CRITICAL)]
    section = active_runs_monitor(entries).section(AT + 10)
    assert len(section.issues) == 2
    assert {reason for _, reason in section.issues} == {
        "Forgejo Recent runs: o/repo two.yml #2 fail", "Forgejo Recent runs: o/repo one.yml #3 fail"}


def test_a_passing_workflow_in_one_repo_does_not_clear_another_repos_failure(active_runs_monitor):
    entries = [forge.Entry("o/one", ("forge.yml #1", "fail"), AT - 100, panel.CRITICAL),
               forge.Entry("o/two", ("forge.yml #1", "fail"), AT - 10, panel.CRITICAL),
               forge.Entry("o/one", ("forge.yml #2", "pass"), AT)]
    monitor = active_runs_monitor(entries)
    assert monitor.section(AT + 10).issues == [(panel.CRITICAL, "Forgejo Recent runs: o/two forge.yml #1 fail")]
    assert monitor.summary()[0] == panel.CRITICAL


@pytest.mark.parametrize("dated_failure", [False, True])
@pytest.mark.parametrize("unknown_first", [False, True])
def test_unknown_start_never_supersedes_a_dated_workflow_run(active_runs_monitor, dated_failure, unknown_first):
    dated = forge.Entry("o/repo", ("forge.yml #2", "fail" if dated_failure else "pass"), AT,
                        panel.CRITICAL if dated_failure else panel.FINE)
    unknown = forge.Entry("o/repo", ("forge.yml #3", "pass" if dated_failure else "fail"), None,
                          panel.FINE if dated_failure else panel.CRITICAL)
    monitor = active_runs_monitor([unknown, dated] if unknown_first else [dated, unknown])
    expected = [(panel.CRITICAL, "Forgejo Recent runs: o/repo forge.yml #2 fail")] if dated_failure else []
    assert monitor.section(AT + 10).issues == expected
    assert monitor.summary()[0] == (panel.CRITICAL if dated_failure else panel.FINE)


@pytest.mark.parametrize("at", [None, AT])
@pytest.mark.parametrize("first_failure", [False, True])
def test_equal_start_times_keep_the_sources_first_run(active_runs_monitor, at, first_failure):
    entries = [forge.Entry("o/repo", ("forge.yml #9", "fail" if first_failure else "pass"), at,
                           panel.CRITICAL if first_failure else panel.FINE),
               forge.Entry("o/repo", ("forge.yml #8", "pass" if first_failure else "fail"), at,
                           panel.FINE if first_failure else panel.CRITICAL)]
    monitor = active_runs_monitor(entries)
    expected = [(panel.CRITICAL, "Forgejo Recent runs: o/repo forge.yml #9 fail")] if first_failure else []
    assert monitor.section(AT + 10).issues == expected
    assert monitor.summary()[0] == (panel.CRITICAL if first_failure else panel.FINE)


def test_status_rows_keep_their_existing_alert_text_beside_a_passing_run(active_runs_monitor):
    entries = [forge.Entry("o/repo", ("forge.yml #1", "fail"), AT - 10, panel.CRITICAL),
               forge.Entry("o/repo", ("could not read (timeout)",), level=panel.WARNING),
               forge.Entry("Status", ("auth failed",), level=panel.WARNING),
               forge.Entry("o/empty", ("no runs",)), forge.Entry("o/repo", ("forge.yml #2", "pass"), AT)]
    monitor = active_runs_monitor(entries)
    assert monitor.section(AT + 10).issues == [
        (panel.WARNING, "Forgejo Recent runs: o/repo could not read (timeout)"),
        (panel.WARNING, "Forgejo Recent runs: Status auth failed")]
    assert monitor.summary()[0] == panel.WARNING


def test_a_cap_hidden_latest_failed_workflow_still_alerts_once(active_runs_monitor):
    entries = [forge.Entry("o/repo", ("old.yml #1", "fail"), AT - 100, panel.CRITICAL),
               forge.Entry("o/repo", ("old.yml #2", "fail"), AT - 50, panel.CRITICAL),
               forge.Entry("o/repo", ("new.yml #3", "pass"), AT)]
    monitor = active_runs_monitor(entries, latest=1)
    section = monitor.section(AT + 10)
    assert section.issues == [(panel.CRITICAL, "Forgejo Recent runs: o/repo old.yml #2 fail")]
    assert section.parts[0].rows[1][1][1] == "new.yml #3"
    assert section.parts[0].rows[-1] == (panel.MORE, ("2 additional runs not visible", "", "", "", ""))
    assert monitor.summary()[0] == panel.CRITICAL


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
def test_long_workflow_alerts_preserve_each_identity_and_run_number(active_runs_monitor, mode):
    workflows = ("w" * 63 + "a", "w" * 63 + "b")
    entries = [forge.Entry("o/repo", (workflow + f" #{number}", "fail"), AT, panel.CRITICAL)
               for workflow, number in zip(workflows, (999999999, 1000000000))]
    section = active_runs_monitor(entries).section(AT + 10)
    assert len(section.issues) == 2
    assert {reason.rsplit(" #", 1)[1] for _, reason in section.issues} == {"999999999 fail", "1000000000 fail"}
    if mode == "image":
        with patch.object(view, "_rows", wraps=view._rows) as rows:
            assert view.image(section, now=AT).startswith(b"\x89PNG")
        output = " ".join(" ".join(cells) for call in rows.call_args_list for _, cells in call.args[1].rows)
    else:
        embed = getattr(view, mode)(section)
        output = (embed.description or "") + " " + " ".join(field.name + " " + field.value for field in embed.fields)
    assert "999999999" in output and "1000000000" in output
