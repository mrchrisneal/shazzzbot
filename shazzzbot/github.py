"""Read-only GitHub REST queries through the shared forge interface."""
from . import forgeruns
from .forge import API, Entry, Problem, ci, commits, count, latest_run, obj, stamp


class Client:
    name = "github"
    label = "GitHub"
    default_url = "https://api.github.com"
    prefix = ""
    scheme = "Bearer"
    features: tuple[str, ...] = ("ci", "runs", "pushes", "issues")

    async def ci(self, api: API, repo: str) -> list[Entry]:
        if api.recent_runs:
            return forgeruns.latest(await self.runs(api, repo))
        row, at = await latest_run(api, repo, "per_page", ("created_at", "run_started_at", "updated_at"))
        if row is None:
            return [Entry(api.text(repo, 210), ("no runs",))]
        state, level = ci(row.get("conclusion") if row.get("status") == "completed" else row.get("status"))
        return [Entry(api.text(repo, 210), (state,), at, level)]

    async def runs(self, api: API, repo: str) -> list[Entry]:
        return await forgeruns.read(api, repo, github=True)

    async def pushes(self, api: API, repo: str) -> list[Entry]:
        return await commits(api, repo, f"/repos/{repo}/commits", {"page": 1, "per_page": 3})

    async def issues(self, api: API, repo: str) -> list[Entry]:
        counts = []
        for kind in ("issue", "pr"):
            row = obj(await api.get("/search/issues", {"q": f"repo:{repo} is:open is:{kind}", "per_page": 1}))
            if row.get("incomplete_results") is not False:
                raise Problem("invalid data")
            counts.append(str(count(row.get("total_count"))))
        row = obj(await api.get(f"/repos/{repo}"))
        return [Entry(api.text(repo, 210), tuple(counts), stamp(row.get("updated_at")))]
