import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest

from shazzzbot import forge, forges, panel, view
from tests.test_forge import AT, DATE, TOKEN, Response, api


def setting(kind="github", show=("ci", "pushes", "issues")):
    return forges.Settings(kind, "https://forge.example", "TEST_FORGE_TOKEN", ("owner/repo",), show)


@pytest.mark.parametrize("kind", ["github", "forgejo"])
def test_forge_provider_defaults_come_from_schema_and_keep_custom_semantics(monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    raw = {"type": kind, "token_env": "TEST_FORGE_TOKEN", "repos": ["owner/repo"]}
    if kind == "forgejo":
        raw["url"] = "https://forge.example"
    result = forges.validate([raw])[0]
    assert result.name == forges.schema.default("forges", "name", discriminator=kind)
    assert result.show == tuple(forges.schema.default("forges", "show", discriminator=kind))
    assert result.latest == forges.schema.default("forges", "latest")
    assert result.pinned == () and result.filter == {} and result.system_row is False
    with pytest.raises(SystemExit) as error:
        forges.validate([{**raw, "token_en": "TEST_FORGE_TOKEN"}])
    assert "token_en" in str(error.value) and "token_env" in str(error.value)


@pytest.mark.parametrize("raw", [None, {}, "forges", 1, [{}, {}, {}, {}]])
def test_forge_collection_rejects_invalid_shapes_and_schema_count_limit(raw):
    with pytest.raises(SystemExit, match="forges"):
        forges.validate(raw)


def test_polls_are_cached_per_forge_and_locks_coalesce_concurrent_cycles(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    now = [0.0]
    monkeypatch.setattr(forges.time, "monotonic", lambda: now[0])
    monitor = forges.Monitor(setting(show=("ci",)), 0)
    response = Response({"total_count": 1,
                         "workflow_runs": [{"status": "completed", "conclusion": "failure", "updated_at": DATE}]})
    session = api(response).session

    async def go():
        await asyncio.gather(monitor.refresh(session), monitor.refresh(session))
        assert session.get.call_count == 1 and monitor.ready
        now[0] = 59
        await monitor.refresh(session)
        assert session.get.call_count == 1
        now[0] = 60
        session.get.return_value = Response({"total_count": 0, "workflow_runs": []})
        await monitor.refresh(session)
        assert session.get.call_count == 2 and monitor.data["ci"][0].values == ("no runs",)
        await monitor.close()
    asyncio.run(go())


def test_enabled_items_only_and_instances_do_not_share_caches(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting("forgejo", ("issues",)), 0)
    session = api(Response({"open_issues_count": 3, "open_pr_counter": 1})).session
    asyncio.run(monitor.refresh(session))
    assert session.get.call_count == 1 and set(monitor.data) == {"issues"}
    assert session.get.call_args.args == ("https://forge.example/api/v1/repos/owner/repo",)
    assert forges.Monitor(setting("forgejo", ("issues",)), 1).data["issues"][0].values == ("checking",)


@pytest.mark.parametrize("response,text", [(Response(status=401), "auth failed"), (Response(status=403), "auth failed"),
                                         (Response(error=TimeoutError()), "timeout"),
                                         (Response(body=b"bad"), "invalid data"), (Response([]), "invalid data"),
                                         (Response({"total_count": 1, "workflow_runs": [None]}), "invalid data")])
def test_auth_timeouts_and_malformed_json_are_warning_rows_in_all_views(monkeypatch, response, text):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("ci",)), 0)
    session = api(response).session
    asyncio.run(monitor.refresh(session))
    section = monitor.section(AT)
    assert section.issues and section.issues[0][0] == panel.WARNING
    assert text in view.text(section).description and text in str(view.rich(section).to_dict())
    assert view.image(section, now=AT).startswith(b"\x89PNG")
    assert TOKEN not in repr(section)


def test_missing_token_after_startup_is_auth_failed_without_network(monkeypatch):
    monkeypatch.delenv("TEST_FORGE_TOKEN", raising=False)
    monitor = forges.Monitor(setting(), 0)
    session = api().session
    asyncio.run(monitor.refresh(session))
    assert all(rows[0].values == ("auth failed",) for rows in monitor.data.values())
    assert "auth failed" in view.text(monitor.section()).description
    session.get.assert_not_called()


def test_admin_only_runner_access_degrades_without_hiding_other_features(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting("forgejo", ("runners",)), 0)
    monitor.client.runners = AsyncMock(side_effect=forge.Problem("auth failed", 403))
    asyncio.run(monitor.refresh(api().session))
    assert monitor.data["runners"][0].values == ("needs admin token",)


def test_unexpected_provider_errors_are_isolated_without_logging_secrets(monkeypatch, caplog):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("ci",)), 0)
    monitor.client.ci = AsyncMock(side_effect=RuntimeError(TOKEN))
    asyncio.run(monitor.refresh(api().session))
    assert "RuntimeError" in caplog.text and TOKEN not in caplog.text
    assert monitor.data["ci"][0].values == ("could not read (invalid data)",)


def test_poll_startup_wait_is_bounded_and_background_work_is_owned(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    clock = [0.0]
    monkeypatch.setattr(forges.time, "monotonic", lambda: clock[0])
    monitor = forges.Monitor(setting(show=("ci",)), 0)
    session = api(Response({"total_count": 0, "workflow_runs": []})).session

    async def go():
        first = monitor.start(session)
        assert first is not None and monitor.start(session) is None
        await first
        await forges.poll([monitor], session, 0)
        assert session.get.call_count == 1
        clock[0] = 60
        await forges.poll([monitor], session, 0)
        await monitor.task
        assert session.get.call_count == 2
        fresh = forges.Monitor(setting(show=("ci",)), 1)
        await forges.poll([fresh], session, 1)
        assert fresh.ready
        await fresh.close()
        clock[0] = 120
        entered = asyncio.Event()
        async def pending(*_):
            entered.set()
            await asyncio.Future()
        monitor._one = pending
        await forges.poll([monitor], session, 0)
        await entered.wait()
        await monitor.close()
        assert monitor.task.cancelled()
    asyncio.run(go())


def test_all_items_render_with_ages_units_and_issue_footers_in_every_view(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting("forgejo", ("ci", "runners", "pushes", "issues")), 2)
    monitor.data = {
        "ci": (forge.Entry("owner/repo", ("fail",), AT, panel.CRITICAL),),
        "runners": (forge.Entry("online", ("online",), AT),
                    forge.Entry("offline", ("offline",), AT, panel.WARNING),
                    forge.Entry("busy", ("busy",))),
        "pushes": (forge.Entry("owner/repo abcdef0", ("a commit",), AT),),
        "issues": (forge.Entry("owner/repo", ("3", "2")),),
    }
    section = monitor.section(AT + 2520)
    assert section.key == "forge_2" and section.title == "Forgejo"
    assert {level for level, _ in section.issues} == {panel.WARNING, panel.CRITICAL}
    assert all("Forgejo" in reason and "forge.example" not in reason for _, reason in section.issues)
    text = view.text(section).description
    rich = str(view.rich(section).to_dict())
    for value in ("fail", "online", "offline", "busy", "abcdef0", "a commit", "42m ago"):
        assert value in text and value in rich
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines() if not line.startswith(("**", "```")))
    assert view.image(section, now=AT).startswith(b"\x89PNG")
    assert monitor.section(AT - 1).parts[0].rows[1][1][-1] == "-"
    assert forges.group("forge_2") == "forges" and forges.group("system") == "system"


def test_many_repositories_keep_each_repos_three_commits_in_all_views(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN",
                          tuple(f"owner/repo{repo}" for repo in range(5)), ("pushes",))
    monitor = forges.Monitor(cfg, 0)
    monitor.data["pushes"] = tuple(forge.Entry(f"owner/repo{repo} {repo}{index}abcde", (f"message-{repo}-{index}",), AT)
                                   for repo in range(5) for index in range(3))
    section = monitor.section(AT + 60)
    assert len(section.parts) == 5
    for repo in range(5):
        for index in range(3):
            assert f"message-{repo}-{index}" in view.text(section).description
            assert f"message-{repo}-{index}" in str(view.rich(section).to_dict())
    assert view.image(section, now=AT).startswith(b"\x89PNG")


def more_rows(parts):
    return [" ".join(" ".join(cells).split()) for part in parts if not part.only or "embed" in part.only.split()
            for level, cells in part.rows if level == panel.MORE]


def rendered(section, mode):
    if mode == "image":
        with patch.object(view, "draw_box", wraps=view.draw_box) as draw:
            assert view.image(section, now=AT).startswith(b"\x89PNG")
        return section.title + " " + repr([call.args[1] for call in draw.call_args_list])
    return str(getattr(view, mode)(section).to_dict())


@pytest.mark.parametrize("kind", ["forgejo", "github"])
@pytest.mark.parametrize("status", [409, 500])
@pytest.mark.parametrize("mode", ["text", "rich", "image"])
def test_empty_repository_conflicts_are_normal_but_server_errors_remain_alerts(monkeypatch, kind, status, mode):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(kind, ("pushes",)), 0)
    session = api(Response(status=status)).session
    asyncio.run(monitor.refresh(session))
    section = monitor.section(AT)
    level = panel.FINE if status == 409 else panel.WARNING
    assert monitor.data["pushes"][0].level == level
    assert monitor.summary() == (level, "1 repo")
    output = rendered(section, mode)
    if status == 409:
        assert not section.issues and "no commits" in output
        assert "409" not in output and view.ISSUES not in output
    else:
        assert section.issues == [(panel.WARNING,
            f"{monitor.client.label} Pushes: owner/repo could not read (HTTP 500)")]
        assert "500" in output and view.ISSUES in output
    session.get.assert_called_once()


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
@pytest.mark.parametrize("name", ["", "Home Forge", "https://forge.example"])
def test_names_hide_endpoint_addresses_and_push_rows_only_show_sha(monkeypatch, mode, name):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("owner/repo",),
                          ("pushes",), name)
    monitor = forges.Monitor(cfg, 0)
    monitor.data["pushes"] = (forge.Entry("owner/repo abcdef0", ("a commit",), AT),)
    section = monitor.section(AT + 60)
    output = rendered(section, mode)
    assert "forge.example" not in output and "https://" not in output
    assert section.title == ("Forgejo" if not name else "[redacted]" if name.startswith("https://") else name)
    part = section.parts[0]
    assert part.heading == "Pushes - owner/repo"
    assert part.rows[0][1] == ("SHA", "Commit", "Age")
    assert part.rows[1][1] == ("abcdef0", "a commit", "1m ago")
    assert "abcdef0" in output and "a commit" in output


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
@pytest.mark.parametrize("response,state", [(Response(error=TimeoutError()), "could not read (timeout)"),
                                         (Response(length=forge.BODY_LIMIT + 1),
                                          "could not read (response too large)"),
                                         (Response({"total_count": 1, "workflow_runs": [{"status": "failure"}]}),
                                          "fail")])
def test_ci_alerts_have_forge_names_and_states_without_age_placeholders(monkeypatch, mode, response, state):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting("forgejo", ("ci",)), 0)
    asyncio.run(monitor.refresh(api(response).session))
    section = monitor.section(AT)
    expected = f"Forgejo CI: owner/repo {state}"
    assert section.issues == [(panel.CRITICAL if state == "fail" else panel.WARNING, expected)]
    output = rendered(section, mode)
    assert "CI" in output and "forge.example" not in output
    assert not section.issues[0][1].endswith(" -")


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
def test_long_ages_are_days_in_every_view(monkeypatch, mode):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("ci",)), 0)
    monitor.data["ci"] = (forge.Entry("owner/repo", ("pass",), AT),)
    output = rendered(monitor.section(AT + 258 * 3600 + 19 * 60), mode)
    assert "10d 18h ago" in output and "258h" not in output


def test_system_summary_uses_only_cached_data_and_the_worst_enabled_item():
    from shazzzbot.forge import Entry
    from shazzzbot.panel import CRITICAL, FINE, WARNING

    monitor = forges.Monitor(setting(), 0)
    assert monitor.summary() == (FINE, "checking")
    monitor.ready = True
    monitor.data = {"ci": (Entry("one", ("pass",)), Entry("two", ("fail",), level=CRITICAL),
                            Entry("three", ("fail",), level=CRITICAL))}
    assert monitor.summary() == (CRITICAL, "1 repo: 1 passing, 2 failing")
    monitor.data = {"ci": (Entry("one", ("running",)), Entry("two", ("cancelled",)), Entry("three", ("no runs",)))}
    assert monitor.summary() == (FINE, "1 repo: 1 running, 1 cancelled, 1 without runs")
    monitor.data = {"runners": (Entry("runner", ("offline",), level=WARNING),)}
    assert monitor.summary() == (WARNING, "1 repo")
    monitor.data = {"ci": (Entry("one", ("could not read (unreachable)",), level=WARNING),
                            Entry("two", ("could not read (timeout)",), level=WARNING))}
    assert monitor.summary() == (CRITICAL, "unreachable")
    monitor.data = {}
    assert monitor.summary() == (FINE, "1 repo")


@pytest.fixture
def maximum_forge_sections(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ["W" * 100 + "/" + "W" * 98 + f"{index:02d}" for index in range(10)]
    features = ("ci", "runs", "runners", "pushes", "issues")
    raw = [{"type": "forgejo", "url": f"https://forge{index}.example", "name": "W" * 64,
            "token_env": "TEST_FORGE_TOKEN", "repos": repos, "show": list(features),
            "pinned": [repos[-1], repos[0]],
            "filter": {kind: list(reversed(repos)) for kind in ("ci", "runs", "pushes", "issues")}}
           for index in range(3)]
    sections = []
    for index, settings in enumerate(forges.validate(raw)):
        monitor = forges.Monitor(settings, index)
        monitor.data = {
            "ci": tuple(forge.Entry(repo, ("fail",), AT - 98 * 86400, panel.CRITICAL) for repo in repos),
            "runs": tuple(forge.Entry(repos[0], ("W" * 62 + f"{run:02d} #1000000000", "fail"), AT - 98 * 86400,
                                      panel.CRITICAL, AT - 86400) for run in range(5))
                    + tuple(forge.Entry(repo, ("could not read (timeout)",), level=panel.WARNING)
                            for repo in repos[1:]),
            "runners": (forge.Entry("W" * 64, ("offline",), AT - 98 * 86400, panel.WARNING),),
            "pushes": tuple(forge.Entry(f"{repo} {commit:07x}", ("W" * 64,), AT - 98 * 86400)
                            for repo in repos for commit in range(3)),
            "issues": tuple(forge.Entry(repo, (str(10**9), str(10**9))) for repo in repos),
        }
        section = monitor.section(AT)
        assert len(section.issues) == 25 and section.issues_limit == 5
        sections.append(section._replace(footer=view.footer(AT + 60, None)))
    return sections


@pytest.mark.parametrize("mode", ["text", "rich"])
def test_repository_cap_with_three_maximum_forges_obeys_discord_limits(maximum_forge_sections, mode):
    from shazzzbot.boards import pack

    embeds = [getattr(view, mode)(section) for section in maximum_forge_sections]
    assert len(embeds) == 3
    for section in maximum_forge_sections:
        parts = view._live(section, "embed" if mode == "rich" else "text")
        pushes = [part for part in parts if part.heading.startswith("Pushes - ")]
        assert len(pushes) == 5 and all(len(part.rows) == 4 for part in pushes)
        for heading in ("CI", "Recent runs", "Open issues and PRs"):
            rows = next(part for part in parts if part.heading == heading).rows
            assert len(rows) == 7 and rows[-1][0] == panel.MORE
            assert sum(level != panel.MORE for level, _ in rows[1:]) == 5
    for embed in embeds:
        assert len(embed) <= 6000 and len(embed.description or "") <= 4096 and len(embed.fields) <= 25
        assert len(embed.title) == 64 and len(embed.footer.text) <= 2048
        assert "fail" in str(embed.to_dict()) and view.ISSUES in str(embed.to_dict())
        assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)
        if mode == "text":
            lines = re.sub(r"\x1b\[[0-9;]*m", "", embed.description).splitlines()
            assert all(len(line) <= panel.WIDTH + 3 for line in lines if not line.startswith(("**", "```")))
    packets = pack(embeds)
    assert [embed for packet in packets for embed in packet] == embeds
    assert all(len(packet) <= 10 and sum(map(len, packet)) <= 6000 for packet in packets)


def test_latest_five_image_layout_bounds_each_feature_without_allocating_canvases(maximum_forge_sections, monkeypatch):
    class Measured(Exception):
        pass

    for section in maximum_forge_sections:
        canvases = []

        def measure(mode, size, *_args, canvases=canvases, **_kwargs):
            canvases.append((mode, size))
            raise Measured

        with monkeypatch.context() as context, patch.object(view, "boxes", wraps=view.boxes) as boxes:
            context.setattr(view.Image, "new", measure)
            context.setattr(view, "placeholder", lambda *_args, **_kwargs: pytest.fail("valid forge needs real image"))
            with pytest.raises(Measured):
                view.image(section, now=AT)
        mode, (width, height) = canvases[0]
        assert mode == "RGB" and view.K == 2
        assert width >= view.MIN_W and height > 0
        assert view._image_bytes(width, height) < view.renderbudget.RENDER_BUDGET
        parts = boxes.call_args.args[0]
        pushes = [part for part in parts if part.heading.startswith("Pushes - ")]
        assert len(pushes) == 5 and all(len(part.rows) == 4 for part in pushes)
        for heading in ("CI", "Recent runs", "Open issues and PRs"):
            rows = next(part for part in parts if part.heading == heading).rows
            assert len(rows) == 7 and rows[-1][0] == panel.MORE
            assert sum(level != panel.MORE for level, _ in rows[1:]) == 5
        issue_rows = next(part.rows for part in parts if part.heading == view.ISSUES)
        assert sum(level > panel.FINE for level, _ in issue_rows) == 5
        assert sum(level == panel.MORE for level, _ in issue_rows) == 1


@pytest.mark.parametrize("kind", ["ci", "runs", "pushes", "issues"])
@pytest.mark.parametrize("latest", [1, 3, 5])
def test_activity_limits_use_stable_ties_and_unknown_dates_last(kind, latest, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ("o/old", "o/tie1", "o/tie2", "o/new", "o/empty1", "o/empty2")
    settings = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", repos, (kind,), latest=latest)
    monitor = forges.Monitor(settings, 0)
    times = (AT - 30, AT - 20, AT - 20, AT - 10, None, None)
    entries = []
    for repo, at in zip(repos, times):
        values = ("pass",) if kind == "ci" else ("Tests #1", "pass") if kind == "runs" else ("1", "2")
        if kind == "pushes":
            entries.extend(forge.Entry(f"{repo} {index:07x}", ("commit",), at) for index in range(3))
        else:
            entries.append(forge.Entry(repo, values, at, finished=AT))
    monitor.data[kind] = tuple(entries)
    section = monitor.section(AT)
    expected = ["o/new", "o/tie1", "o/tie2", "o/old", "o/empty1"][:latest]
    if kind == "pushes":
        pushes = [part for part in section.parts if part.heading.startswith("Pushes - ")]
        assert [part.heading for part in pushes] == [f"Pushes - {repo}" for repo in expected]
        assert all(len(part.rows) == 4 for part in pushes)
    else:
        assert [cells[0] for level, cells in section.parts[0].rows[1:] if level != panel.MORE] == expected
    hidden = len(repos) - latest
    noun = "run" if kind == "runs" else "repo"
    noun += "s" if hidden != 1 else ""
    assert more_rows(section.parts) == [f"{hidden} additional {noun} not visible"]
    assert monitor.data[kind] == tuple(entries)


def test_push_activity_uses_the_newest_commit_and_empty_repos_sort_last(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("github", "https://forge.example", "TEST_FORGE_TOKEN", ("o/one", "o/two", "o/empty"),
                          ("pushes",), latest=2)
    monitor = forges.Monitor(cfg, 0)
    monitor.data["pushes"] = (forge.Entry("o/one 0000000", ("old",), AT - 50),
                              forge.Entry("o/one 0000001", ("new",), AT),
                              forge.Entry("o/two 0000002", ("middle",), AT - 10),
                              forge.Entry("o/empty", ("no commits",)))
    section = monitor.section(AT)
    assert [part.heading for part in section.parts] == ["Pushes - o/one", "Pushes - o/two", "Pushes"]
    assert more_rows(section.parts) == ["1 additional repo not visible"]


def test_old_hidden_failures_still_feed_all_issues_and_the_system_summary(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/old", "o/new"),
                          ("ci", "runs", "pushes"), latest=1)
    monitor = forges.Monitor(cfg, 0)
    monitor.ready = True
    monitor.data = {
        "ci": (forge.Entry("o/old", ("fail",), AT - 100, panel.CRITICAL),
               forge.Entry("o/new", ("pass",), AT)),
        "runs": (forge.Entry("o/new", ("Tests #1", "pass"), AT),
                 forge.Entry("o/old", ("could not read (timeout)",), level=panel.WARNING)),
        "pushes": (forge.Entry("o/old", ("could not read (timeout)",), level=panel.WARNING),
                   forge.Entry("o/new abcdef0", ("commit",), AT)),
    }
    section = monitor.section(AT)
    assert section.issues_limit == 5 and len(section.issues) == 3
    assert section.issues == [(panel.CRITICAL, "Forgejo CI: o/old fail"),
                              (panel.WARNING, "Forgejo Recent runs: o/old could not read (timeout)"),
                              (panel.WARNING, "Forgejo Pushes: o/old could not read (timeout)")]
    assert monitor.summary() == (panel.CRITICAL, "2 repos: 1 passing, 1 failing")
    assert section.parts[0].rows[1][1][0] == "o/new"
    assert len(section.parts[1].rows) == 3 and section.parts[1].rows[-1][0] == panel.MORE
    assert [part.heading for part in section.parts if part.heading.startswith("Pushes - ")] == ["Pushes - o/new"]
    assert more_rows(section.parts) == ["1 additional repo not visible", "1 additional run not visible",
                                        "1 additional repo not visible"]


@pytest.mark.parametrize("kind", ["ci", "issues", "pushes"])
def test_filters_precede_pins_and_caps_and_ties_follow_tracked_order(monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ["o/first", "o/second", "o/new", "o/empty", "o/tie"]
    cfg = forges.validate([{"type": "forgejo", "url": "https://forge.example", "token_env": "TEST_FORGE_TOKEN",
                            "repos": repos, "show": [kind], "latest": 3, "pinned": ["O/SECOND", "O/FIRST"],
                            "filter": {kind: ["O/TIE", "O/EMPTY", "O/NEW", "O/SECOND"]}}])[0]
    monitor = forges.Monitor(cfg, 0)
    dates = {"o/first": AT + 100, "o/second": None, "o/new": AT, "o/empty": None, "o/tie": AT}
    entries = []
    for repo in reversed(repos):
        if kind == "pushes":
            entries.extend(forge.Entry(f"{repo} {index:07x}", ("commit",), dates[repo]) for index in range(3))
        else:
            entries.append(forge.Entry(repo, ("pass",) if kind == "ci" else ("1", "2"), dates[repo]))
    monitor.data[kind] = tuple(entries)
    section = monitor.section(AT + 200)
    expected = ["o/second", "o/new", "o/tie"]
    if kind == "pushes":
        pushes = [part for part in section.parts if part.heading.startswith("Pushes - ")]
        assert [part.heading for part in pushes] == [f"Pushes - {repo}" for repo in expected]
        assert all(len(part.rows) == 4 for part in pushes)
        assert section.parts[-1].heading == "Pushes"
    else:
        assert [cells[0] for level, cells in section.parts[0].rows[1:] if level != panel.MORE] == expected
    assert more_rows(section.parts) == ["1 additional repo not visible"]
    assert monitor.data[kind] == tuple(entries)


@pytest.mark.parametrize("kind", ["ci", "issues", "pushes", "runs", "runners"])
def test_no_hidden_entries_means_no_summary_and_default_filter_keeps_everything(monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ("o/one", "o/two")
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", repos, (kind,))
    monitor = forges.Monitor(cfg, 0)
    values = ("pass",) if kind == "ci" else ("Tests #1", "pass") if kind == "runs" else ("1", "2")
    monitor.data[kind] = tuple(forge.Entry(f"{repo} abcdef0" if kind == "pushes" else repo, values) for repo in repos)
    section = monitor.section(AT)
    assert more_rows(section.parts) == []
    if kind == "pushes":
        assert [part.heading for part in section.parts] == ["Pushes - o/one", "Pushes - o/two"]
    else:
        assert [cells[0] for _, cells in section.parts[0].rows[1:]] == list(repos)


def test_runner_activity_ignores_repository_pins_and_summarizes_hidden_runners(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/repo",), ("runners",),
                          latest=2, pinned=("o/repo",))
    monitor = forges.Monitor(cfg, 0)
    monitor.data["runners"] = (forge.Entry("unknown", ("online",)), forge.Entry("older", ("online",), AT - 20),
                               forge.Entry("newest", ("online",), AT), forge.Entry("middle", ("online",), AT - 10))
    section = monitor.section(AT)
    assert [cells[0] for level, cells in section.parts[0].rows[1:] if level != panel.MORE] == ["newest", "middle"]
    assert more_rows(section.parts) == ["2 additional runners not visible"]


def test_filtered_failures_are_excluded_but_cap_hidden_failures_are_retained(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ("o/excluded", "o/hidden", "o/new")
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", repos, ("ci",), latest=1,
                          filter={"ci": ("o/hidden", "o/new")})
    monitor = forges.Monitor(cfg, 0)
    monitor.data["ci"] = (forge.Entry("o/excluded", ("fail",), AT, panel.CRITICAL),
                          forge.Entry("o/hidden", ("fail",), AT - 10, panel.CRITICAL),
                          forge.Entry("o/new", ("pass",), AT))
    section = monitor.section(AT)
    assert section.issues == [(panel.CRITICAL, "Forgejo CI: o/hidden fail")]
    assert section.parts[0].rows[1][1][0] == "o/new"
    assert more_rows(section.parts) == ["1 additional repo not visible"]


def test_per_feature_filters_avoid_unneeded_requests_and_preserve_the_runs_cache(monkeypatch):
    from tests.test_forgeruns import run

    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    raw = {"type": "forgejo", "url": "https://forge.example", "token_env": "TEST_FORGE_TOKEN",
           "repos": ["o/a", "o/b", "o/c", "o/unused"], "show": ["ci", "runs", "pushes", "issues"],
           "pinned": ["o/a"], "latest": 2,
           "filter": {"ci": ["o/b"], "runs": ["o/b"], "pushes": ["o/a"], "issues": ["o/c"]}}
    monitor = forges.Monitor(forges.validate([raw])[0], 0)
    session = api().session
    def response(url, **_kwargs):
        if url.endswith("/repos/o/b/actions/runs"):
            return Response({"total_count": 1, "workflow_runs": [run()]})
        if url.endswith("/repos/o/a/commits"):
            return Response([{"sha": "a" * 40, "commit": {"message": "commit", "committer": {"date": DATE}}}])
        assert url.endswith("/repos/o/c")
        return Response({"open_issues_count": 3, "open_pr_counter": 1, "updated_at": DATE})
    session.get.side_effect = response
    asyncio.run(monitor.refresh(session))
    assert session.get.call_count == 3
    assert {call.args[0] for call in session.get.call_args_list} == {
        "https://forge.example/api/v1/repos/o/b/actions/runs", "https://forge.example/api/v1/repos/o/a/commits",
        "https://forge.example/api/v1/repos/o/c"}
    assert [entry.name for entry in monitor.data["ci"]] == ["o/b"]
    assert [entry.name for entry in monitor.data["runs"]] == ["o/b"]
    assert [entry.name for entry in monitor.data["issues"]] == ["o/c"]
    section = monitor.section(AT + 100)
    assert not section.issues and more_rows(section.parts) == []
    assert [part.heading for part in section.parts if part.heading.startswith("Pushes - ")] == ["Pushes - o/a"]


def test_real_refresh_keeps_raw_repo_identity_when_display_redaction_collides(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    pinned, other = TOKEN + "/repo", "forge.example/repo"
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", (pinned, other), ("ci",),
                          latest=1, pinned=(pinned,))
    monitor = forges.Monitor(cfg, 0)
    async def ci(client, repo):
        return [forge.Entry(client.text(repo, 210), ("pass" if repo == pinned else "fail",),
                            AT - 10 if repo == pinned else AT, panel.FINE if repo == pinned else panel.CRITICAL)]
    monitor.client.ci = ci
    asyncio.run(monitor.refresh(api().session))
    assert monitor.data["ci"][0].name == monitor.data["ci"][1].name == "[redacted]/repo"
    section = monitor.section(AT)
    assert section.parts[0].rows[1][1][1] == "pass"
    assert section.issues == [(panel.CRITICAL, "Forgejo CI: [redacted]/repo fail")]
    assert more_rows(section.parts) == ["1 additional repo not visible"]
    assert TOKEN not in repr(section) and "forge.example" not in repr(section)


@pytest.mark.parametrize("state", ["checking", "auth failed"])
def test_global_startup_and_auth_rows_survive_repo_filters_and_pins(monkeypatch, state):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/a", "o/b"),
                          ("ci", "runs", "pushes", "issues"), latest=1, pinned=("o/a",),
                          filter={"ci": ("o/b",), "runs": ("o/b",), "pushes": ("o/b",), "issues": ("o/b",)})
    monitor = forges.Monitor(cfg, 0)
    session = api().session
    if state == "auth failed":
        monkeypatch.delenv("TEST_FORGE_TOKEN")
        asyncio.run(monitor.refresh(session))
    section = monitor.section(AT)
    for part in section.parts:
        expected = panel.clip(state, 9) if part.only == "text" else state
        assert all(expected in " ".join(cells) for level, cells in part.rows[1:] if level != panel.MORE)
    assert len(section.parts) == 5 and all(len(part.rows) == 2 for part in section.parts)
    assert more_rows(section.parts) == []
    assert len(section.issues) == (4 if state == "auth failed" else 0)
    if state == "auth failed":
        assert all(state in reason for _, reason in section.issues)
        assert state in view.text(section).description and state in str(view.rich(section).to_dict())
        assert state in rendered(section, "image")
    session.get.assert_not_called()


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
def test_pins_filters_and_exact_hidden_summary_are_present_in_every_render(monkeypatch, mode):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/pin", "o/new", "o/excluded"),
                          ("ci",), latest=1, pinned=("o/pin",), filter={"ci": ("o/pin", "o/new")})
    monitor = forges.Monitor(cfg, 0)
    monitor.data["ci"] = (forge.Entry("o/new", ("pass",), AT), forge.Entry("o/pin", ("pass",), AT - 50),
                          forge.Entry("o/excluded", ("pass",), AT + 10))
    section = monitor.section(AT)
    assert section.parts[0].rows[1][1][0] == "o/pin"
    assert more_rows(section.parts) == ["1 additional repo not visible"]
    if mode == "image":
        with patch.object(view, "_rows", wraps=view._rows) as draws:
            assert view.image(section, now=AT).startswith(b"\x89PNG")
        output = " ".join(" ".join(cells) for call in draws.call_args_list for _, cells in call.args[1].rows)
    else:
        embed = getattr(view, mode)(section)
        output = (embed.description or "") + " " + " ".join(field.name + " " + field.value for field in embed.fields)
    output = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).replace("`", "").split())
    assert "o/pin" in output and "o/new" not in output and "o/excluded" not in output
    assert "1 additional repo not visible" in output


@pytest.mark.parametrize("kind", ["ci", "issues", "pushes"])
def test_two_visible_pins_follow_the_pinned_list_order_and_fill_the_limit(monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repos = ("o/first", "o/newest", "o/last")
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", repos, (kind,), latest=2,
                          pinned=("o/last", "o/first"))
    monitor = forges.Monitor(cfg, 0)
    values = ("pass",) if kind == "ci" else ("1", "2")
    monitor.data[kind] = tuple(forge.Entry(repo + " abcdef0" if kind == "pushes" else repo, values, at)
                              for repo, at in (("o/newest", AT), ("o/first", AT - 10), ("o/last", None)))
    section = monitor.section(AT)
    if kind == "pushes":
        assert [part.heading for part in section.parts if part.heading.startswith("Pushes - ")] == [
            "Pushes - o/last", "Pushes - o/first"]
    else:
        assert [cells[0] for level, cells in section.parts[0].rows[1:] if level != panel.MORE] == ["o/last", "o/first"]
    assert more_rows(section.parts) == ["1 additional repo not visible"]


@pytest.mark.parametrize("kind", ["ci", "pushes"])
def test_manual_sanitized_names_match_their_unambiguous_repo_without_exposing_the_host(monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    repo = "forge.example/private"
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", (repo, "safe/repo"), (kind,),
                          latest=1, pinned=(repo,), filter={kind: (repo,)})
    monitor = forges.Monitor(cfg, 0)
    monitor.data[kind] = (forge.Entry("[redacted]/private abcdef0" if kind == "pushes" else "[redacted]/private",
                                     ("pass",), AT - 10),
                          forge.Entry("safe/repo abcdef0" if kind == "pushes" else "safe/repo", ("pass",), AT))
    section = monitor.section(AT)
    assert "[redacted]/private" in repr(section)
    assert "forge.example" not in repr(section) and "safe/repo" not in repr(section)
    assert more_rows(section.parts) == []


@pytest.mark.parametrize("state", ["pass", "running", "cancelled"])
@pytest.mark.parametrize("latest", [1, 5])
@pytest.mark.parametrize("newest_first", [False, True])
def test_latest_workflow_state_clears_old_alerts_and_summary_without_rewriting_history(
        monkeypatch, state, latest, newest_first):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/repo",), ("runs",),
                          latest=latest)
    monitor = forges.Monitor(cfg, 0)
    monitor.ready = True
    old = forge.Entry("o/repo", ("Tests #1", "fail"), AT - 100, panel.CRITICAL, AT - 90)
    new = forge.Entry("o/repo", ("Tests #2", state), AT)
    entries = (new, old) if newest_first else (old, new)
    monitor.data["runs"] = entries
    section = monitor.section(AT + 10)
    assert not section.issues and monitor.summary() == (panel.FINE, "1 repo")
    rows = section.parts[0].rows[1:]
    visible = [(level, cells[1], cells[2]) for level, cells in rows if level != panel.MORE]
    expected = [(panel.FINE, "Tests #2", state), (panel.CRITICAL, "Tests #1", "fail")]
    assert visible == expected[:latest]
    assert more_rows(section.parts) == (["1 additional run not visible"] if latest == 1 else [])
    assert monitor.data["runs"] == entries


@pytest.mark.parametrize("at", [None, AT])
@pytest.mark.parametrize("first_state", ["pass", "fail"])
def test_equal_or_unknown_workflow_times_keep_the_first_run_as_the_current_state(monkeypatch, at, first_state):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("runs",)), 0)
    monitor.ready = True
    first_level = panel.CRITICAL if first_state == "fail" else panel.FINE
    second_state, second_level = ("pass", panel.FINE) if first_state == "fail" else ("fail", panel.CRITICAL)
    monitor.data["runs"] = (forge.Entry("owner/repo", ("Tests #11", first_state), at, first_level),
                            forge.Entry("owner/repo", ("Tests #10", second_state), at, second_level))
    section = monitor.section(AT + 10)
    assert monitor.summary() == (first_level, "1 repo")
    assert section.issues == ([(panel.CRITICAL, "GitHub Recent runs: owner/repo Tests #11 fail")]
                              if first_state == "fail" else [])
    assert len(section.parts[0].rows) == 3


def test_workflow_names_are_grouped_before_display_clipping_and_only_the_last_run_number_is_removed(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("runs",)), 0)
    monitor.ready = True
    failed_workflow = "W" * 63 + "A"
    passed_workflow = "W" * 63 + "B"
    monitor.data["runs"] = (
        forge.Entry("owner/repo", (failed_workflow + " #1000000000", "fail"), AT - 20, panel.CRITICAL),
        forge.Entry("owner/repo", (passed_workflow + " #1000000000", "pass"), AT),
        forge.Entry("owner/repo", ("Tests # nightly #1", "fail"), AT - 10, panel.CRITICAL),
        forge.Entry("owner/repo", ("Tests # nightly #2", "pass"), AT))
    section = monitor.section(AT + 10)
    assert section.issues == [(panel.CRITICAL,
        f"GitHub Recent runs: owner/repo {failed_workflow} #1000000000 fail")]
    assert monitor.summary() == (panel.CRITICAL, "1 repo")
    assert len(section.parts[0].rows) == 5


def test_a_current_failed_workflow_hidden_by_the_activity_limit_remains_an_alert(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("o/repo",), ("runs",), latest=1)
    monitor = forges.Monitor(cfg, 0)
    monitor.ready = True
    monitor.data["runs"] = (forge.Entry("o/repo", ("Tests #1", "pass"), AT - 100),
                            forge.Entry("o/repo", ("Tests #2", "fail"), AT - 50, panel.CRITICAL),
                            forge.Entry("o/repo", ("Deploy #3", "pass"), AT))
    section = monitor.section(AT + 10)
    assert section.issues == [(panel.CRITICAL, "Forgejo Recent runs: o/repo Tests #2 fail")]
    assert monitor.summary() == (panel.CRITICAL, "1 repo")
    assert section.parts[0].rows[1][1][1:3] == ("Deploy #3", "pass")
    assert more_rows(section.parts) == ["2 additional runs not visible"]


def test_raw_repository_identity_keeps_workflow_alerts_separate_when_display_names_collide(monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    failed_repo, passed_repo = TOKEN + "/repo", "forge.example/repo"
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", (failed_repo, passed_repo),
                          ("runs", "pushes"), latest=1)
    monitor = forges.Monitor(cfg, 0)

    async def runs(client, repo):
        label, state, at, level = ("Tests #1", "fail", AT - 20, panel.CRITICAL) if repo == failed_repo else (
            "Tests #2", "pass", AT, panel.FINE)
        return [forge.Entry(client.text(repo, 210), (label, state), at, level)]

    async def pushes(client, repo):
        return [forge.Entry(client.text(repo, 210) + f" {index:07x}", ("commit",),
                            AT - 10 if repo == failed_repo else AT) for index in range(3)]

    monitor.client.runs, monitor.client.pushes = runs, pushes
    asyncio.run(monitor.refresh(api().session))
    assert monitor.data["runs"][0].name == monitor.data["runs"][1].name == "[redacted]/repo"
    section = monitor.section(AT + 10)
    assert section.issues == [(panel.CRITICAL, "Forgejo Recent runs: [redacted]/repo Tests #1 fail")]
    assert monitor.summary() == (panel.CRITICAL, "2 repos")
    assert section.parts[0].rows[1][1][1:3] == ("Tests #2", "pass")
    pushes_parts = [part for part in section.parts if part.heading.startswith("Pushes - ")]
    assert len(pushes_parts) == 1 and len(pushes_parts[0].rows) == 4
    assert more_rows(section.parts) == ["1 additional run not visible", "1 additional repo not visible"]
    assert TOKEN not in repr(section) and "forge.example" not in repr(section)


@pytest.mark.parametrize("mode", ["text", "rich", "image"])
def test_superseded_failure_stays_in_history_but_not_the_issue_footer_in_every_render(monkeypatch, mode):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    monitor = forges.Monitor(setting(show=("runs",)), 0)
    monitor.ready = True
    monitor.data["runs"] = (forge.Entry("owner/repo", ("Tests #1", "fail"), AT - 20, panel.CRITICAL, AT - 10),
                            forge.Entry("owner/repo", ("Tests #2", "pass"), AT - 5, finished=AT))
    section = monitor.section(AT + 10)
    output = rendered(section, mode)
    assert "Tests #1" in output and "Tests #2" in output and "fail" in output and "pass" in output
    assert not section.issues and view.ISSUES not in output
    assert monitor.summary() == (panel.FINE, "1 repo")
