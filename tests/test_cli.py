from __future__ import annotations

from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from aspria_booker.cli import run
from aspria_booker.browser_adapter import ActionVerification, BrowserCourseObservation, BrowserExistingState
from aspria_booker.session import LoginOutcome, LoginStatus


def write_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    dry_run: bool = True,
    notifications: bool = False,
) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
enabled: {str(enabled).lower()}
dry_run: {str(dry_run).lower()}
club:
  name: Aspria Hannover Maschsee
courses:
  - LES MILLS BODYPUMP
time_windows:
  tuesday:
    - from: "00:00"
      to: "11:00"
release_job:
  poll_start: "20:58"
  tight_poll_until: "21:10"
  tight_interval_seconds: 5
  slow_poll_until: "22:00"
  slow_interval_seconds: 60
hourly_job:
  lookahead_days: 3
matching:
  exact_normalized: true
  fuzzy: false
default_duration_minutes: 60
buffer_minutes: 15
retention:
  history_days: 90
  artifacts_days: 14
notifications:
  enabled: {str(notifications).lower()}
  smtp:
    host: smtp.example.invalid
    port: 587
    username_env: SMTP_USERNAME
    password_env: SMTP_PASSWORD
    from_email: bot@example.invalid
    to_email: ops@example.invalid
""",
        encoding="utf-8",
    )
    return path


class CapturingMailer:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


class CapturingLoginRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[object, bool]] = []

    def __call__(self, config: object, *, headed: bool) -> LoginOutcome:
        self.calls.append((config, headed))
        return LoginOutcome(LoginStatus.AUTHENTICATED, "storage state saved")


class AuthenticatedJobSession:
    def ensure_authenticated(self, *, run_id: str | None = None) -> LoginOutcome:
        return LoginOutcome(LoginStatus.AUTHENTICATED, f"session ok for {run_id}")


class EmptyJobSource:
    def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        return [], []

    def book(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise AssertionError("not reached")

    def join_waitlist(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise AssertionError("not reached")


class AdvancingClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def sleep(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def test_help_lists_initial_commands(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = run(["--help"])

    output = capsys.readouterr().out
    assert exit_code == 0
    for command in ["setup-login", "release", "hourly", "scan", "test-email"]:
        assert command in output


def test_setup_login_headed_saves_storage_state_with_injected_runner(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True)
    login_runner = CapturingLoginRunner()

    exit_code = run(
        ["--config", str(config_path), "setup-login", "--headed"],
        login_runner=login_runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert login_runner.calls
    assert login_runner.calls[0][1] is True
    assert "storage state saved" in captured.out


def test_setup_login_does_not_require_configured_live_credentials(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=False)
    login_runner = CapturingLoginRunner()

    exit_code = run(
        ["--config", str(config_path), "setup-login", "--headed"],
        login_runner=login_runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert login_runner.calls
    assert "ASPRIA_PASSWORD" not in captured.err


def test_enabled_false_prevents_live_release_action(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=False, dry_run=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ASPRIA_EMAIL=a@example.invalid\nASPRIA_PASSWORD=server-password-secret\n",
        encoding="utf-8",
    )

    exit_code = run(["--config", str(config_path), "--env-file", str(env_path), "release"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "disabled by config" in captured.out
    assert "server-password-secret" not in captured.out


def test_scan_cli_dry_run_flag_keeps_operation_safe(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=False)
    env_path = tmp_path / ".env"
    env_path.write_text("ASPRIA_EMAIL=a@example.invalid\nASPRIA_PASSWORD=super-secret\n", encoding="utf-8")

    exit_code = run(
        ["--config", str(config_path), "--env-file", str(env_path), "scan", "--dry-run"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "dry-run" in captured.out
    assert "super-secret" not in captured.out


def test_release_dry_run_exposes_target_date_and_poll_windows(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True)

    exit_code = run(
        ["--config", str(config_path), "release"],
        today=date(2026, 5, 3),
        job_session=AuthenticatedJobSession(),
        job_source=EmptyJobSource(),
        job_clock=AdvancingClock(datetime(2026, 5, 3, 20, 58)),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "release: target date 2026-05-06" in captured.out
    assert "tight 20:58 through 21:10 every 5s" in captured.out
    assert "slow 21:10 through 22:00 every 60s" in captured.out


def test_hourly_dry_run_exposes_inclusive_scan_range(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True)

    exit_code = run(
        ["--config", str(config_path), "hourly"],
        today=date(2026, 5, 3),
        job_session=AuthenticatedJobSession(),
        job_source=EmptyJobSource(),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "hourly: scan 2026-05-03 through 2026-05-06" in captured.out


def test_scan_accepts_explicit_date_range_and_defaults_to_dry_run(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=False)
    env_path = tmp_path / ".env"
    env_path.write_text("ASPRIA_EMAIL=a@example.invalid\nASPRIA_PASSWORD=super-secret\n", encoding="utf-8")

    exit_code = run(
        [
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "scan",
            "--from",
            "2026-05-03",
            "--to",
            "2026-05-05",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "scan: dry-run mode" in captured.out
    assert "2026-05-03 through 2026-05-05" in captured.out
    assert "email notifications suppressed" in captured.out


def test_scan_dry_run_can_request_notifications_explicitly(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True)

    exit_code = run(
        [
            "--config",
            str(config_path),
            "scan",
            "--from",
            "2026-05-03",
            "--to",
            "2026-05-03",
            "--notify",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "email notifications allowed" in captured.out


def test_scan_rejects_invalid_manual_date_ranges(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True)

    exit_code = run(
        [
            "--config",
            str(config_path),
            "scan",
            "--from",
            "2026-05-06",
            "--to",
            "2026-05-05",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "scan date error" in captured.err


def test_test_email_sends_message_with_configured_smtp_settings(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = write_config(tmp_path, enabled=True, dry_run=True, notifications=True)
    env_path = tmp_path / ".env"
    env_path.write_text("SMTP_USERNAME=mailer\nSMTP_PASSWORD=secret-password\n", encoding="utf-8")
    mailer = CapturingMailer()

    exit_code = run(["--config", str(config_path), "--env-file", str(env_path), "test-email"], mailer=mailer)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "test email sent to ops@example.invalid" in captured.out
    assert "secret-password" not in captured.out
    assert len(mailer.messages) == 1
    assert mailer.messages[0]["To"] == "ops@example.invalid"
    assert "SMTP test" in mailer.messages[0]["Subject"]
