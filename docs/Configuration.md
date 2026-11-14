# Configuration

`shazzzbot/schema.py` defines these settings.
Config checks and editor metadata use this schema at startup.
The example and this reference exist before startup, so the build creates them.

Set secrets in environment variables. Do not put tokens in TOML.
Replace the required Discord ids in `config.example.toml` before startup.
Patterns must match the whole value.

## config

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `config_format` | integer | `1` | - | Configuration format. Required. Minimum: 1. Maximum: 1. |
| `guild_id` | integer | Required | - | Discord server id. Minimum: 1. |
| `audit_channel_id` | integer | Unset | - | Audit channel id. Zero disables the channel. Minimum: 0. |
| `commands_channel_id` | integer | Unset | - | Extra slash-command channel id. Zero disables it. Minimum: 0. |
| `blowup_user_id` | integer | `0` | - | User to ping with /blowup. Zero disables it. Minimum: 0. |
| `interval` | integer | `10` | seconds | Seconds between host samples. Minimum: 5. |
| `edit_every` | integer | `60` | seconds | Default time between edits of a message without images. Minimum: 5. |
| `image_every` | integer | `60` | seconds | Default time between image uploads. A message with images changes only when they upload. The learned budget can delay uploads. Minimum: 60. |
| `state_file` | string | `"/data/state.json"` | - | State file in the bot data volume. |
| `log_level` | string | `"info"` | - | Log level. Changes need a restart. Choices: `"debug"`, `"info"`, `"warning"`, `"error"`. |
| `debug_hours` | integer | `24` | hours | Hours that debug logging stays on after a start, then info. Zero keeps it on. Minimum: 0. Maximum: 168. |
| `timezone` | string | `"UTC"` | - | IANA time zone for displayed times. |
| `green_ok` | boolean | `false` | - | Use a green dot for healthy rows. |
| `order` | list | `["system", "history", "disks", "services", "docker", "apps", "tasks", "forges"]` | - | Section order. Unlisted sections follow. No duplicates. Each item: choices: `"system"`, `"history"`, `"disks"`, `"services"`, `"docker"`, `"apps"`, `"tasks"`, `"forges"`. |
| `history` | table | `{}` | - | Chart history. See [history](#history). |
| `system` | table | `{}` | - | System card text. See [system](#system). |
| `host` | table | `{}` | - | Read-only host access. See [host](#host). |
| `jellyfin` | table | `{}` | - | Jellyfin now-playing. Requires JELLYFIN_KEY. See [jellyfin](#jellyfin). |
| `ups` | table | `{}` | - | Read-only NUT UPS. See [ups](#ups). |
| `netdata` | table | `{}` | - | Netdata alarms. See [netdata](#netdata). |
| `tasks` | table | `{}` | - | Scheduled and running tasks. Running-task colors do not add alerts. See [tasks](#tasks). |
| `services` | table | `{}` | - | Container and external service checks. See [services](#services). |
| `restarts` | table | `{}` | - | Container restarts by /docker. See [restarts](#restarts). |
| `forges` | list | `[]` | - | Code forges to monitor. Maximum length: 3. Each item: see [forges](#forges). |
| `helper` | table | Unset | - | Signed host-helper connection. The key goes in .env, under the name that key_env sets. See [helper](#helper). |
| `render` | table | `{}` | - | Render mode per section. Unset sections use image mode. See [render](#render). |
| `thresholds` | table | `{resource = [80, 90], load = [0.7, 1.0], iowait = [13, 24], temp = [80, 95], disk_full = [90, 95], hdd_temp = [45, 55], ssd_temp = [60, 70], disk_busy = 95}` | - | Warning and critical levels. One number sets warning only. See [thresholds](#thresholds). |
| `perms` | table | `{}` | - | Command permissions. Unlisted users and roles have no tier. See [perms](#perms). |
| `node_updates` | table | `{}` | - | Trusted terminal commands printed by /update. The bot never runs them. See [node_updates](#node_updates). |
| `preview` | table | Unset | - | Authenticated config preview. Omit it to disable the page. See [preview](#preview). |
| `outputs` | list | Required | - | One block per output channel. Channel ids must be different. Minimum length: 1. Each item: see [outputs](#outputs). |

## outputs

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `channel` | integer | Required | - | Discord channel id. Minimum: 1. |
| `render` | table | `{}` | - | Per-section overrides of the top-level [render]. See [render](#render). |
| `sections` | list | Unset | - | Sections to show, in the top-level order. Omit to show all. Minimum length: 1. No duplicates. Each item: choices: `"system"`, `"history"`, `"disks"`, `"services"`, `"docker"`, `"apps"`, `"tasks"`, `"forges"`. |
| `image_every` | integer | Unset | seconds | Image period. Omit to inherit the global value. Minimum: 60. |
| `edit_every` | integer | Unset | seconds | Edit period. Omit to inherit the global value. Minimum: 5. |

## preview

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `enabled` | boolean | `false` | - | Enable the authenticated preview page. |
| `bind` | string | `"0.0.0.0"` | - | Container listen IP. compose.yaml publishes no port; add one on a single host address. Pattern: `[0-9A-Fa-f:.]{2,45}`. |
| `port` | integer | `8780` | - | Preview listen port. Minimum: 1024. Maximum: 65535. |
| `user` | string | `""` | - | Login name. Omit both credentials to generate a login. Pattern: `[A-Za-z0-9_.-]{0,64}`. |
| `password_hash` | string | Not shown | - | Password hash from python -m shazzzbot.preview hash. |

## history

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `days` | integer | `7` | days | History retained in the data volume. Minimum: 1. Maximum: 7. |

## system

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `title` | string | `"Unraid System Info"` | - | System card title. |
| `subtitle` | string | `""` | - | System card subtitle. |

## host

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `root` | string | `"/host"` | - | Read-only host mount. |
| `emhttp` | string | `"/host/emhttp"` | - | Read-only emhttp data directory. |
| `docker_proxy` | string | Unset | - | Docker socket proxy URL. Omit it to disable Docker checks. |

## jellyfin

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `url` | string | `""` | - | Jellyfin base URL. Requires JELLYFIN_KEY. |

## ups

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `target` | string | `""` | - | Read-only NUT target as name@host[:port]. The default port is 3493. |

## netdata

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `url` | string | `""` | - | Netdata base URL. Empty disables Netdata. |
| `ignore` | list | `[]` | - | Netdata chart and alarm globs to hide. |

## services

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `columns` | list | `["Local", "VPN", "Public"]` | - | Labels for Local, VPN and Public checks. Minimum length: 3. Maximum length: 3. Each item: minimum length: 1. |
| `ignore` | list | `[]` | - | Service names or globs to skip entirely. |
| `public_host` | string | `""` | - | Public name or IP to probe through configured relays. |
| `checks` | list | `[]` | - | One block per service check. Each item: see [service_check](#service_check). |

## restarts

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `allow` | list | `[]` | - | Container-name globs /docker restart may touch. Empty disables restarts. |
| `groups` | table | `{}` | - | Group names mapped to container-name globs. See [restart_groups](#restart_groups). |

## service_check

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `name` | string | Required | - | Display name. Minimum length: 1. |
| `container` | string | `""` | - | Docker container name. Omit it for an external check. |
| `url` | string | `""` | - | Local HTTP or HTTPS probe URL. |
| `expect` | string | `""` | - | Text that an HTTP response must contain. |
| `public_url` | string | `""` | - | Public HTTP or HTTPS URL, checked through a relay when configured. |
| `vpn_url` | string | `""` | - | VPN HTTP or HTTPS probe URL. |
| `tcp` | string | `""` | - | Local TCP target as host:port or [IPv6]:port. |
| `vpn_tcp` | string | `""` | - | VPN TCP target. |
| `public_tcp` | string | `""` | - | Public TCP target, checked through a relay when configured. |
| `every` | integer | Unset | seconds | Check period. External checks need at least 60 seconds. Omit for the global cycle. Minimum: 1. |

## forges

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `type` | string | Required | - | Forge provider. Choices: `"forgejo"`, `"github"`. |
| `url` | string | forgejo: `""`, github: `"https://api.github.com"` | - | Forge API base URL. Forgejo needs its server URL without /api/v1. |
| `token_env` | string | Required | - | Name of an environment variable. Pattern: `[A-Za-z_][A-Za-z0-9_]{0,127}`. |
| `repos` | list | Required | - | Tracked owner/name repositories. Minimum length: 1. Maximum length: 10. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |
| `show` | list | forgejo: `["ci", "runs", "runners", "pushes", "issues"]`, github: `["ci", "runs", "pushes", "issues"]` | - | Features to show. The default includes every feature the provider supports. Minimum length: 1. No duplicates. Each item: choices: `"ci"`, `"runs"`, `"runners"`, `"pushes"`, `"issues"`. |
| `name` | string | forgejo: `"Forgejo"`, github: `"GitHub"` | - | Display name. Do not include a private address. Maximum length: 64. Printable text. Must include a non-space character. |
| `system_row` | boolean | `false` | - | Add a short System row. At most one forge may enable it. |
| `latest` | integer | `5` | - | Maximum displayed items per activity list. Minimum: 1. Maximum: 5. |
| `pinned` | list | `[]` | - | Tracked repos shown first. They count toward latest. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |
| `filter` | table | `{}` | - | Enabled activity kinds mapped to tracked repos to monitor. See [forge_filter](#forge_filter). |

## helper

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `socket` | string | Required | - | Unix socket path inside the bot container. |
| `key_env` | string | Required | - | Name of an environment variable. Pattern: `[A-Za-z_][A-Za-z0-9_]{0,127}`. |

## tasks

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `shown` | integer | `20` | - | Number of upcoming tasks to show. Minimum: 1. |
| `ignore` | list | `[]` | - | Task names or globs to hide. |
| `long_running_minutes` | duration | `60` | minutes | Emphasize long tasks. Zero or false disables the time trigger. Finite non-negative number or false. |
| `heavy_kinds` | list | `["parity", "file_integrity", "appdata_backup_plugin", "appdata_backup_script"]` | - | Task kinds to emphasize. An empty list disables this trigger. No duplicates. Each item: choices: `"parity"`, `"file_integrity"`, `"appdata_backup_plugin"`, `"appdata_backup_script"`. |
| `rename` | table | `{}` | - | Task names mapped to display names. See [text_map](#text_map). |

## thresholds

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `resource` | threshold | `[80, 90]` | percent | CPU and RAM use. One warning number or [warning, critical]. |
| `load` | threshold | `[0.7, 1.0]` | per core | Load average per CPU core. One warning number or [warning, critical]. |
| `iowait` | threshold | `[13, 24]` | percent | CPU time waiting for disk I/O. One warning number or [warning, critical]. |
| `temp` | threshold | `[80, 95]` | C | CPU temperature. One warning number or [warning, critical]. |
| `disk_full` | threshold | `[90, 95]` | percent | Disk space used. One warning number or [warning, critical]. |
| `hdd_temp` | threshold | `[45, 55]` | C | Hard disk temperature. One warning number or [warning, critical]. |
| `ssd_temp` | threshold | `[60, 70]` | C | SSD temperature. One warning number or [warning, critical]. |
| `disk_busy` | threshold | `95` | percent | Time a disk is busy. One number sets warning only. One warning number or [warning, critical]. |

## permission

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `roles` | list | `[]` | - | Allowed Discord role ids. Each item: minimum: 1. |
| `users` | list | `[]` | - | Allowed Discord user ids. Each item: minimum: 1. |

## node_update

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `hostname` | string | Required | - | Node hostname or IP. Pattern: `[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}`. |
| `run_on` | string | Unset | - | Host where the command is pasted. Omit to use hostname. Pattern: `[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}`. |
| `command` | string | Required | - | One trusted ASCII command line. The bot only prints it. Minimum length: 1. Maximum length: 1000. Must include a non-space character. |

## render

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `system` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `history` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `disks` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `services` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `docker` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `apps` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `tasks` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |
| `forges` | string | `"image"` | - | text, embed, or image. Choices: `"text"`, `"embed"`, `"image"`. |

## perms

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `view` | table | `{}` | - | Allowed users and roles. See [permission](#permission). |
| `operate` | table | `{}` | - | Allowed users and roles. See [permission](#permission). |
| `admin` | table | `{}` | - | Allowed users and roles. See [permission](#permission). |

## forge_filter

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `ci` | list | `[]` | - | Repos to monitor. Minimum length: 1. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |
| `runs` | list | `[]` | - | Repos to monitor. Minimum length: 1. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |
| `pushes` | list | `[]` | - | Repos to monitor. Minimum length: 1. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |
| `issues` | list | `[]` | - | Repos to monitor. Minimum length: 1. Each item: pattern: `[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}`. |

## Environment variables

| Key | Type | Default | Unit | Description |
|---|---|---|---|---|
| `DISCORD_TOKEN` | string | Not shown | - | Discord bot token. Never put it in TOML. Required. |
| `JELLYFIN_KEY` | string | Not shown | - | Jellyfin API key. |
| `RELAY_URL` | string | `""` | - | Relay HTTPS URL. Add _2, _3 and so on for more relays. |
| `RELAY_KEY` | string | Not shown | - | Relay API key. Use the same suffix as its URL. |
| `RELAY_FINGERPRINT` | string | `""` | - | Relay TLS certificate SHA-256. Use the same suffix as its URL. |

## Named tables

### text_map

Names map to string values.

### restart_groups

Names map to list values. Minimum length: 1. Each item: must include a non-space character.

### node_updates

Names map to table values. See [node_update](#node_update).
