from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import Protocol

from aspria_booker.config import BookerConfig, ConfigError, effective_dry_run, load_config, redact
from aspria_booker.email import Mailer, NotificationService, SMTPMailer, render_test_email
from aspria_booker.history import HistoryStore
from aspria_booker.jobs import Authenticator, JobClock, JobPaths, run_hourly_job, run_release_job
from aspria_booker.schedule import ScheduleError, hourly_scan_dates, manual_scan_dates, release_schedule
from aspria_booker.session import LoginOutcome, setup_interactive_login
from aspria_booker.browser_adapter import CourseActionSource


LIVE_COMMANDS = {"release", "hourly"}


class LoginRunner(Protocol):
    def __call__(self, config: BookerConfig, *, headed: bool) -> LoginOutcome:
        raise NotImplementedError


def main() -> None:
    raise SystemExit(run())


def run(
    argv: list[str] | None = None,
    *,
    today: date | None = None,
    mailer: Mailer | None = None,
    login_runner: LoginRunner | None = None,
    job_source: CourseActionSource | None = None,
    job_session: Authenticator | None = None,
    job_clock: JobClock | None = None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code or 0)
    if args.command is None:
        parser.print_help()
        return 0
    today = today or date.today()

    try:
        config = load_config(
            args.config,
            env_path=args.env_file,
            require_live_credentials=args.command != "setup-login",
        )
    except ConfigError as error:
        print(redact(f"config error: {error}"), file=sys.stderr)
        return 2

    dry_run = effective_dry_run(
        config,
        cli_dry_run=getattr(args, "dry_run", False) or args.command == "scan",
    )
    if args.command == "setup-login":
        runner = login_runner or setup_interactive_login
        outcome = runner(config, headed=args.headed)
        print(redact(outcome.message))
        return 0 if outcome.ok else 1

    if args.command == "test-email":
        try:
            sender = mailer or SMTPMailer.from_config(config)
            sender.send(render_test_email(config))
        except ConfigError as error:
            print(redact(f"config error: {error}"), file=sys.stderr)
            return 2
        except OSError as error:
            print(redact(f"SMTP error: {error}"), file=sys.stderr)
            return 1
        print(f"test email sent to {config.notifications.smtp.to_email}")
        return 0

    try:
        scan_dates = _scan_dates(args)
    except ScheduleError as error:
        print(redact(f"scan date error: {error}"), file=sys.stderr)
        return 2
    if args.command in LIVE_COMMANDS and not config.enabled:
        print(f"{args.command}: disabled by config; no live operation attempted")
        return 0

    if dry_run and args.command not in LIVE_COMMANDS:
        print(f"{args.command}: dry-run mode; no booking or waitlist action attempted")
        if args.command == "release":
            schedule = release_schedule(config, today=today)
            print(f"release: target date {schedule.target_date.isoformat()}")
            print(
                "release: "
                f"tight {schedule.tight.start} through {schedule.tight.end} "
                f"every {schedule.tight.interval_seconds}s; "
                f"slow {schedule.slow.start} through {schedule.slow.end} "
                f"every {schedule.slow.interval_seconds}s"
            )
        if args.command == "hourly":
            hourly_dates = hourly_scan_dates(config, today=today)
            print(f"hourly: scan {hourly_dates[0].isoformat()} through {hourly_dates[-1].isoformat()}")
        if scan_dates:
            print(f"scan: {scan_dates[0].isoformat()} through {scan_dates[-1].isoformat()}")
        if args.command == "scan":
            if args.notify:
                print("scan: email notifications allowed by explicit --notify")
            else:
                print("scan: email notifications suppressed for dry-run")
        return 0

    if args.command in LIVE_COMMANDS:
        history = HistoryStore.open(JobPaths().history_path)
        notifications = None
        if config.notifications.enabled:
            try:
                notifications = NotificationService(history=history, mailer=mailer or SMTPMailer.from_config(config))
            except ConfigError as error:
                print(redact(f"config error: {error}"), file=sys.stderr)
                return 2
        try:
            if args.command == "release":
                run_release_job(
                    config=config,
                    history=history,
                    source=job_source,
                    session=job_session,
                    notifications=notifications,
                    today=today,
                    clock=job_clock,
                    dry_run=dry_run,
                )
            else:
                run_hourly_job(
                    config=config,
                    history=history,
                    source=job_source,
                    session=job_session,
                    notifications=notifications,
                    today=today,
                    dry_run=dry_run,
                )
        finally:
            history.close()
        return 0

    print(f"{args.command}: command shell ready; behavior will be implemented in later slices")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aspria-booker")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    subparsers = parser.add_subparsers(dest="command")

    for command in ["setup-login", "release", "hourly", "scan", "test-email"]:
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="force safe dry-run mode; cannot force live operation",
        )
        if command == "scan":
            command_parser.add_argument("--from", dest="from_date")
            command_parser.add_argument("--to", dest="to_date")
            command_parser.add_argument("--notify", action="store_true")
        if command == "setup-login":
            command_parser.add_argument("--headed", action="store_true")
    return parser


def _scan_dates(args: argparse.Namespace) -> list[date]:
    if args.command != "scan":
        return []
    if bool(args.from_date) != bool(args.to_date):
        raise ScheduleError("--from and --to must be provided together")
    if not args.from_date:
        return []
    return manual_scan_dates(args.from_date, args.to_date)


if __name__ == "__main__":
    main()
