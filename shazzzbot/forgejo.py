"""Forgejo endpoints verified against the instance's public Swagger schema."""
from . import forgeruns
from .forge import API, Entry, ci, commits, count, latest_run, obj, stamp
from .panel import WARNING


class Client:
    name = "forgejo"
    label = "Forgejo"
    default_url = ""
    prefix = "/api/v1"
    scheme = "token"
    features: tuple[str, ...] = ("ci", "runs", "runners", "pushes", "issues")

    async def ci(self, api: API, repo: str) -> list[Entry]:
        if api.recent_runs:
            return forgeruns.latest(await self.runs(api, repo))
        row, at = await latest_run(api, repo, "limit", ("created", "started", "stopped", "updated"))
        if row is None:
            return [Entry(api.text(repo, 210), ("no runs",))]
        state, level = ci(row.get("status"))
        return [Entry(api.text(repo, 210), (state,), at, level)]

    async def runs(self, api: API, repo: str) -> list[Entry]:
        return await forgeruns.read(api, repo)

    async def runners(self, api: API) -> list[Entry]:
        return [Entry("Runners", ("runner API unavailable",), level=WARNING)]

    async def pushes(self, api: API, repo: str) -> list[Entry]:
        return await commits(api, repo, f"/repos/{repo}/commits",
                             {"page": 1, "limit": 3, "stat": "false", "verification": "false", "files": "false"})

    async def issues(self, api: API, repo: str) -> list[Entry]:
        row = obj(await api.get(f"/repos/{repo}"))
        counts = (str(count(row.get("open_issues_count"))), str(count(row.get("open_pr_counter"))))
        return [Entry(api.text(repo, 210), counts, stamp(row.get("updated_at")))]
