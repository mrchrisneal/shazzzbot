"""Validated recent workflow runs shared by read-only forge clients."""
from .forge import API, Entry, Problem, ci, count, items, obj, stamp

LIMIT = 5


async def _page(api: API, repo: str, size: str, page: int) -> tuple[list[dict], int]:
    body = obj(await api.get(f"/repos/{repo}/actions/runs", {"page": page, size: LIMIT}))
    rows = items(body.get("workflow_runs"))
    total = count(body.get("total_count"))
    if total < (page - 1) * LIMIT + len(rows) or total and not rows or len(rows) > LIMIT:
        raise Problem("invalid data")
    return rows, total


async def _recent(api: API, repo: str, github: bool) -> list[dict]:
    size = "per_page" if github else "limit"
    first, total = await _page(api, repo, size, 1)
    if github or total <= LIMIT:
        return first
    page = (total + LIMIT - 1) // LIMIT
    last, _ = await _page(api, repo, size, page)
    if len(last) < LIMIT:
        previous = first if page == 2 else (await _page(api, repo, size, page - 1))[0]
        last = [*previous, *last]
    return last


async def read(api: API, repo: str, *, github: bool = False) -> list[Entry]:
    rows = await _recent(api, repo, github)
    out = []
    for row in rows:
        state, level = ci(row.get("conclusion") if github and row.get("status") == "completed" else row.get("status"))
        start = stamp(row.get("run_started_at" if github else "started"))
        created = stamp(row.get("created_at" if github else "created"))
        start = start if start is not None and start > 0 else created
        end = None
        if state != "running":
            raw_end = row.get("updated_at" if github else "stopped")
            end = stamp(raw_end)
            if not github and (raw_end is None or end is not None and end <= 0):
                end = None
            elif end is None:
                raise Problem("invalid data")
        if start is None or start <= 0 or end is not None and end < start:
            raise Problem("invalid data")
        name = api.text(row.get("name" if github else "workflow_id"), 64)
        number = count(row.get("run_number" if github else "index_in_repo"))
        if not name or not number:
            raise Problem("invalid data")
        out.append((start, 0 if github else number,
                    Entry(api.text(repo, 210), (f"{name} #{number}", state), start, level, end)))
    return [entry for _, _, entry in sorted(out, key=lambda item: item[:2], reverse=True)[:LIMIT]] \
        or [Entry(api.text(repo, 210), ("no runs",))]


def latest(entries: list[Entry]) -> list[Entry]:
    row = entries[0]
    return [Entry(row.name, (row.values[-1],), row.finished or row.at, row.level)]
