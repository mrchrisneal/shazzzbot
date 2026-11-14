# Changelog

## 1.0.0
Released on November 14, 2026

- FEAT: Show host health, disks, Docker stacks, services, scheduled tasks, and forge activity in Discord.
- FEAT: Choose image, embed, or text output for each section and configure separate channel schedules.
- FEAT: Save chart and per-app history across restarts, with usage trends and recorded task durations.
- FEAT: Read scheduled tasks and UPS shutdown settings through the optional signed host helper.
- FEAT: Show NUT battery status, runtime, and the configured UPS shutdown threshold.
- FEAT: Check local, VPN, and public services with HTTP, TCP, and optional relay nodes.
- FEAT: Monitor Forgejo and GitHub CI, commits, and issues, with runner status for Forgejo.
- FEAT: Manage allowlisted container, Compose stack, and configured group restarts from Discord.
- FEAT: Preview display settings in a browser with live data and sample profiles.
- FEAT: Send history charts on request with /chartgen, and show the learned Discord rate budget with /ratelimits.
- FEAT: Pace image uploads on one shared clock, under Discord's measured upload limit, and learn the rest from its replies.
- FIX: Keep configured render modes when a panel spans two or more messages.
- FIX: Match image timestamps and update footers to the rendered content and delivery schedule.
- SEC: Gate container restarts with permission checks, confirmation, and audit records.
- SEC: Reject unsupported stored formats and unsafe helper paths.
- SEC: Verify helper updates against configured version and SHA-256 pins before publication.
- DOCS: Document configuration, permissions, security, and third-party licenses.
- CHORE: Add installer prerequisite checks, backups, health checks, and rollback.
