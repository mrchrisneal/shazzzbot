# Contributing to shazzzbot

Development happens in a private repository. This repository receives one commit per release. It may not be monitored regularly, and pull requests are not accepted until a future confirmed-stable release.

Shazzzbot supports Unraid only. Support for other systems, platforms, and integrations depends on feedback. For the planned work, see [Roadmap](README.md#roadmap).

These are the coding and project rules for shazzzbot. Contributors and the maintainer audit code against them.
A rule is a hard rule unless it says "should". A "should" rule is the default, and a change that breaks it says why.

> [!IMPORTANT]
> The house rules come first. If a house rule and any other rule here, or an outside style guide such as
> PEP 8, disagree, the house rule wins.

## House rules

- Priority order: security and reliability first, then performance, then everything else.
- Host mounts are read-only. The bot writes to the host only through the Docker socket proxy (list and restart).
- The bot never reboots or shuts down the server, and never stops or starts the array.
- Every privileged command must go through `shazzzbot/perms.py`: deny by default, a confirm button when the
  command is destructive, an audit post, and fail closed.
- Every file is ASCII only, with no emoji anywhere: code, comments, UI text, and docs. `tools/ascii_guard.py`
  enforces it. Status glyphs come from the escapes in `shazzzbot/glyphs.py`.
- The only dash is the plain hyphen. For a longer dash, write `--` joined to the words: `word--word`.
- Comments are rare. Write one only where the next reader would otherwise be confused. Never narrate code.
- Comments carry no attribution, dates, task numbers, or change notes. Git history is the changelog. The one
  exception is the `reviewed YEAR-MONTH` tag on a suppression.
- Docstrings are one line, and only where they help. Args and Returns blocks are not required.
- A file over 1000 lines gets split by responsibility.
- Never silence a finding for convenience. A last-resort suppression carries the rule id, a reason, and
  `reviewed YEAR-MONTH`, for example `# nosec B603 -- fixed argv, no shell. reviewed 2026-11`.
- Secrets, hostnames, and Discord ids live only in gitignored files (`.env`, `config.toml`).
- Tests keep 100% line and branch coverage. mypy, ruff, and bandit report no findings.

## Discord

- Request only the gateway intents the code uses. Today that is `guilds` only. Add a privileged intent
  (members, presences, message content) only for a real need.
- Send the first interaction response within 3 seconds. Call `defer()` before any network or disk I/O.
- Finish follow-ups and edits within 15 minutes, the lifetime of the interaction token.
- Send errors, denials, and private data as ephemeral replies.
- Let py-cord handle rate limits and 429 replies. Do not add retry loops around Discord calls.
- Stop and log on repeated 401 or 403 replies. Discord blocks the IP for a time after 10,000 invalid requests in 10 minutes.
- Validate the config before login, so a crash loop cannot use up the daily IDENTIFY limit.
- Let the library resume after a disconnect. Do not write custom reconnect code around `bot.run`.
- Change the presence sparingly. The gateway allows 120 sends per 60 seconds per connection.
- Register commands for the configured guild only (`debug_guilds`). The bot acts only in `guild_id`.
- Do not set `default_member_permissions`. It would hide commands from users that `shazzzbot/perms.py`
  grants by user or role id, and `perms.py` is the only authority.
- Install an `on_application_command_error` handler and a `View.on_error` handler. Each one logs through
  `logging` and answers the user.
- When a View's `interaction_check` rejects a user, answer that user ephemerally.
- Declare slash-command options with `@discord.option`.
- Pass `allowed_mentions` on every message that holds user-controlled text.
- Return at most 25 autocomplete choices.
- Run the same version on every instance of one Discord app. Each instance syncs the command set on connect,
  so two versions overwrite each other.
- Close owned resources, such as the aiohttp session and background tasks, in an overridden `close()`.
  Compose waits 10 seconds after SIGTERM before it kills the container.

## Python and style

- The package uses a flat layout: `shazzzbot/` at the repo root, tests in `tests/`.
- The target is Python 3.14 (`ruff.toml`, `mypy.ini`, and the `python:3.14.10-slim` base image).
- Pin every direct dependency to an exact version (`==`) in `requirements.in` and `requirements-dev.in`.
- `requirements.txt` and `requirements-dev.txt` are locks with hashes for every package, transitive ones too.
  Do not edit them by hand. Regenerate them with `uv pip compile`, using the command in each file's header.
- Lines are at most 120 characters. ruff enforces it (E501), and `.editorconfig` sets the editor defaults.
- Follow PEP 8 names: lowercase modules, CapWords classes, UPPER_CASE constants, and a leading `_` for private
  names. Compare to `None` with `is`.
- Type every public function. mypy checks `shazzzbot/` with `check_untyped_defs`.
- Scope `ignore_missing_imports` to the module that needs it, and do not set it globally.
- Use one logger per module. Configure handlers only in `main()`.
- Pass log arguments in %-style (`log.info("x=%s", x)`), never as f-strings. Call `log.exception` only
  inside `except`.
- Log to stdout. Docker collects the output.
- The default level is INFO. At DEBUG, the log alone must be enough to find a fault on a machine you cannot
  access: log the inputs read, the decisions made, each external call with its result and time, and the time
  of each cycle. Never log a secret.
- Validate the config when it loads. Fail fast, name the bad setting, and accept only known keys and values.
- Catch specific exceptions. Catch `Exception` only at an isolation point, such as a loop that must keep
  running, and always log it.
- Keep mutable state on an instance. Do not change module-level state from another module.
- Keep functions near 40 lines. Past that, check whether a split makes the code clearer.
- Keep config keys stable. Deprecate a key before you remove it.

## Tests

- Tests live in `tests/`, in `test_*.py` files, with `test_*` functions named for the behavior they check,
  for example `test_restart_refused_when_not_allowlisted`.
- Test file names are unique.
- Test the negative paths: denied users, bad config, timeouts, Discord HTTP errors, and every fail-closed branch.
- Every test asserts an outcome. Coverage finds untested code. Review the assertions too.
- Put shared setup in fixtures in `tests/conftest.py`.
- Unit tests use no real network, Discord, or Docker. Fake the time.
- Keep tests independent of order and of each other. Wait for every task a test starts.
- Compare floats with `pytest.approx`.

## Docker

- The bot runs as a non-root user with a fixed UID, 10001. The proxy and the tasks exporter run as root only
  to read the Docker socket and root-only cron files, with every capability dropped.
- Each service sets `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, and `read_only: true`,
  with a `tmpfs` where the service must write.
- Each service sets `mem_limit` and `pids_limit`, so one container cannot starve the host.
- Each service sets json-file log rotation (`max-size`). The Docker default has no limit.
- Never expose the Docker socket. Only the allowlisting proxy in `docker-proxy/haproxy.cfg` mounts it, and
  that proxy publishes no port.
- Pin base images to an exact version and digest. `test.sh` reads its image from the Dockerfile, so both match.
- Merge the weekly Dependabot digest updates, which carry the base-image security fixes. Rebuild with
  `--no-cache` so the Dockerfile's `apt-get upgrade` also picks up new Debian fixes.
- Keep a `HEALTHCHECK` that proves the main loop runs (`python -m shazzzbot.health`).
- Never put a secret in `ARG`, `ENV`, or `COPY`. Image layers keep them.
- `.dockerignore` is an allowlist, so `.env`, config files, and caches never enter the build context.

> [!WARNING]
> Access to the Docker socket gives root on the host. A change to `docker-proxy/haproxy.cfg` or to the
> socket mount needs a security review.

## Security

- Use multi-factor authentication on every account that can push or release.
- Never commit a token. Discord resets a bot token that GitHub secret scanning finds in a public repository.
- Scan the full git history for secrets before the repository goes public. A secret in an old commit is
  public too.
- Keep GitHub secret scanning and push protection on for the repository.
- Validate input at every trust boundary with an allowlist: slash-command arguments, autocomplete values,
  relay replies, Docker API JSON, host files, and the exporter's `tasks.json`.
- Each parser has a test with hostile input.
- Error replies to users stay generic. Details go to the log.
- Vet each new direct dependency: a real project, the correct name, and active maintenance.
- Keep dependencies current with a dependency update tool, and act on known vulnerabilities fast.
- Report a suspected vulnerability privately to the maintainer. Do not open a public issue for it.

## CI and releases

- CI on a public repository runs only on GitHub-hosted runners. A self-hosted runner would run code from any
  pull request.
- Pin each action to a full commit SHA. Set top-level `permissions: contents: read`.
- Never put `${{ github.event.* }}` inside a `run:` step. Do not use `pull_request_target`.
- Protect `main`: no force push, and CI must pass before a merge.
- Keep changes small, with one purpose each.
- Commit messages are plain imperative sentences, for example `Defer before the Docker list call`.
  Conventional Commits are not required.
- Version with SemVer. Stay on 0.y.z while the config format can change. The version is `__version__` in
  `shazzzbot/__init__.py`.
- Tag each release `vX.Y.Z` with an annotated tag, and publish a GitHub release with its notes.
- Releases are source only, with no image or package. The hash-locked `requirements.txt` is the dependency
  manifest, and GitHub's dependency graph can export an SBOM from it.
- The license is AGPL-3.0-or-later, in `LICENSE`. Source files carry no license or copyright headers.

## Running the checks

Local Python can be older than 3.14, so the test and lint commands run in the base image. Run them from the
repo root:

```bash
./test.sh --cov                         # pytest with the 100% floor from .coveragerc, then mypy
python3 tools/ascii_guard.py --check    # characters, whole repo

# Lint and SAST
docker run --rm -v "$PWD:/src:ro" -w /src python:3.14.10-slim sh -c \
  "pip install -q --root-user-action=ignore ruff==0.17.4 bandit==1.9.4 \
   && ruff check --no-cache . && bandit -c .bandit.yaml -r shazzzbot/"
```

> [!NOTE]
> On GitHub, `.github/workflows/ci.yml` runs the same checks on every push and pull request.
> `.forgejo/workflows/forge.yml` is the maintainer's self-hosted pipeline with more scanners. GitHub does not read it.
