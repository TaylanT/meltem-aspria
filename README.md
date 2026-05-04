# Aspria Booker

Private Python CLI for cautious Aspria Hannover Maschsee course booking automation.

The bot is intended to run on a private Linux server as a dedicated non-root user. It reads `config.yaml`, loads secrets from `.env`, keeps Playwright login state in `storage/aspria-state.json`, records run history in `storage/history.sqlite`, and writes local diagnostic artifacts under `storage/artifacts`.

## Local Setup

Install Python 3.12 or newer and `uv` on the server, then install project and browser dependencies:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin aspria-booker
sudo mkdir -p /opt/aspria-booker
sudo chown aspria-booker:aspria-booker /opt/aspria-booker

cd /opt/aspria-booker
sudo -u aspria-booker uv sync
sudo -u aspria-booker uv run playwright install chromium
```

Keep the checkout, `config.yaml`, `.env`, and `storage/` owned by the bot user:

```bash
sudo chown -R aspria-booker:aspria-booker /opt/aspria-booker
sudo chmod 750 /opt/aspria-booker
sudo chmod 640 /opt/aspria-booker/config.yaml
sudo chmod 600 /opt/aspria-booker/.env
sudo chmod 700 /opt/aspria-booker/storage
```

## Configuration

Edit `config.yaml` for the club URL, target courses, allowed time windows, release polling, hourly lookahead, retention, and notifications. Keep `dry_run: true` until dry-run scans and email testing are successful. Set `enabled: false` when you want systemd jobs to start but skip live operation.

Example secret file:

```dotenv
ASPRIA_EMAIL=you@example.com
ASPRIA_PASSWORD=your-aspria-password
SMTP_USERNAME=smtp-user
SMTP_PASSWORD=smtp-password
```

The `.env` file is required for live headless re-login when `dry_run: false`. SMTP secrets are required only when `notifications.enabled: true` and the SMTP config references those env names.

The default storage state path is `storage/aspria-state.json`. Treat it as sensitive session material: keep it readable only by the bot user, do not commit it, and transfer it only over trusted channels if you prepare it on another machine.

## Login Renewal

Create or renew the saved browser session interactively:

```bash
sudo -u aspria-booker env HOME=/home/aspria-booker XDG_CACHE_HOME=/home/aspria-booker/.cache \
  uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env setup-login --headed
```

The command opens Chromium, waits while you complete the Aspria login and navigate to the booking flow, then saves `storage/aspria-state.json` after you press Enter in the terminal.

If you generate storage state locally, copy it to `/opt/aspria-booker/storage/aspria-state.json`, then run:

```bash
sudo chown aspria-booker:aspria-booker /opt/aspria-booker/storage/aspria-state.json
sudo chmod 600 /opt/aspria-booker/storage/aspria-state.json
```

Renew storage state after password changes, expired sessions, protective challenge pages, or repeated `login_required` failures in history.

## Dry-Run Testing

Before enabling live operation, run dry scans with the same config and secrets that systemd will use:

```bash
sudo -u aspria-booker uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env scan --dry-run --from 2026-05-04 --to 2026-05-07
sudo -u aspria-booker uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env release --dry-run
sudo -u aspria-booker uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env hourly --dry-run
```

Dry-run mode may collect observations and record no-op decisions, but it must not book or join waitlists. Leave `dry_run: true` in `config.yaml` until the observed target dates, courses, time windows, and logs are correct.

Test SMTP delivery separately:

```bash
sudo -u aspria-booker uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env test-email
```

## Live Enablement

After dry-run verification:

1. Set `dry_run: false` in `config.yaml`.
2. Keep `enabled: true`.
3. Confirm `.env` contains `ASPRIA_EMAIL` and `ASPRIA_PASSWORD`.
4. Confirm `storage/aspria-state.json` exists and is owned by `aspria-booker`.
5. Start the timers below.

The CLI still refuses unsafe live credential states: live `release` and `hourly` runs require Aspria credentials when `dry_run: false`, and `enabled: false` makes live commands exit without attempting actions.

## systemd Units

Create `/etc/systemd/system/aspria-booker-release.service`:

```ini
[Unit]
Description=Aspria Booker release job
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=aspria-booker
Group=aspria-booker
WorkingDirectory=/opt/aspria-booker
Environment=HOME=/home/aspria-booker
Environment=XDG_CACHE_HOME=/home/aspria-booker/.cache
ExecStart=/usr/bin/env uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env release
```

Create `/etc/systemd/system/aspria-booker-release.timer`:

```ini
[Unit]
Description=Run Aspria Booker release job before 21:00 release

[Timer]
OnCalendar=*-*-* 20:55:00
Persistent=true
Unit=aspria-booker-release.service

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/aspria-booker-hourly.service`:

```ini
[Unit]
Description=Aspria Booker hourly scan job
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=aspria-booker
Group=aspria-booker
WorkingDirectory=/opt/aspria-booker
Environment=HOME=/home/aspria-booker
Environment=XDG_CACHE_HOME=/home/aspria-booker/.cache
ExecStart=/usr/bin/env uv run aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env hourly
```

Create `/etc/systemd/system/aspria-booker-hourly.timer`:

```ini
[Unit]
Description=Run Aspria Booker hourly scan job

[Timer]
OnCalendar=hourly
Persistent=true
Unit=aspria-booker-hourly.service

[Install]
WantedBy=timers.target
```

Enable timers:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aspria-booker-release.timer
sudo systemctl enable --now aspria-booker-hourly.timer
systemctl list-timers 'aspria-booker-*'
```

## Operations

Find systemd journal logs:

```bash
journalctl -u aspria-booker-release.service --since today
journalctl -u aspria-booker-hourly.service --since today
journalctl -u aspria-booker-release.timer -u aspria-booker-hourly.timer --since today
```

Inspect SQLite history:

```bash
sudo -u aspria-booker sqlite3 /opt/aspria-booker/storage/history.sqlite '.tables'
sudo -u aspria-booker sqlite3 /opt/aspria-booker/storage/history.sqlite \
  'select run_id, command, started_at from runs order by started_at desc limit 10;'
```

Inspect local artifacts:

```bash
sudo -u aspria-booker find /opt/aspria-booker/storage/artifacts -maxdepth 3 -type f
```

Artifacts are created for unclear page state, failed actions, technical failures, and manual login intervention. They may include `page.html`, `screenshot.png`, and redacted `metadata.json`. History retention and artifact retention are configured in `config.yaml` as `retention.history_days` and `retention.artifacts_days`; the current code exposes retention cleanup APIs but does not yet install a separate cleanup timer.

## Failure Recovery

For an expired session or `login_required` failure, run `setup-login --headed`, verify `storage/aspria-state.json` permissions, then rerun `hourly --dry-run` before returning to live operation.

For manual login intervention, protective challenges, Captcha, or 2FA screens, stop the timers, complete login manually with `setup-login --headed`, and restart timers only after a dry-run command succeeds. The bot does not bypass protective checks.

For unclear page state, inspect the latest `storage/artifacts/<run_id>/...` directory and the matching `storage/history.sqlite` run. Keep `dry_run: true` or stop timers until the page state is understood.

The notification behavior is conservative: `test-email` sends a standalone test message, successful live actions and known failures can send emails when notifications are enabled, and dry-run manual scans suppress email unless `scan --notify` is used. Login and known-failure notifications are deduplicated in SQLite history.

## Out Of Scope

The bot does not perform cancellation, rebooking, Captcha bypass, 2FA bypass, or English UI fallback. Those states require manual handling.
