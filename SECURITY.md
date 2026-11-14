# Security policy

## Supported versions

Only the latest release gets security fixes.

## Report a vulnerability

Report a vulnerability privately through GitHub private vulnerability reporting. On the **Security** tab of the repository, click **Report a vulnerability**.

> [!WARNING]
> Do not open a public issue for a vulnerability. A public issue shows the problem to everyone before a fix is available.

Include this information in the report:

- The shazzzbot version, or the commit that you run.
- Your `config.toml` and `compose.yaml`, with all secrets, ids, host names, and IP addresses removed.
- The steps that cause the problem, and what happens.

## Scope

These parts of the project are in scope:

- The bot, in `shazzzbot/`.
- The Docker proxy allowlist, in `docker-proxy/haproxy.cfg`.
- The host helper, in `shazzzbot/helper/`.
- The Compose files, `compose.yaml` and `compose.relay.yaml`.
- The relay, in `shazzzbot/relay.py`.
