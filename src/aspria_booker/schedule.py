from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from aspria_booker.config import BookerConfig


class ScheduleError(ValueError):
    """Raised when a requested scan range is invalid."""


@dataclass(frozen=True)
class PollWindow:
    start: str
    end: str
    interval_seconds: int


@dataclass(frozen=True)
class ReleaseSchedule:
    target_date: date
    tight: PollWindow
    slow: PollWindow


def release_schedule(config: BookerConfig, *, today: date) -> ReleaseSchedule:
    return ReleaseSchedule(
        target_date=today + timedelta(days=3),
        tight=PollWindow(
            start=config.release_job.poll_start,
            end=config.release_job.tight_poll_until,
            interval_seconds=config.release_job.tight_interval_seconds,
        ),
        slow=PollWindow(
            start=config.release_job.tight_poll_until,
            end=config.release_job.slow_poll_until,
            interval_seconds=config.release_job.slow_interval_seconds,
        ),
    )


def hourly_scan_dates(config: BookerConfig, *, today: date) -> list[date]:
    return [today + timedelta(days=offset) for offset in range(config.hourly_job.lookahead_days + 1)]


def manual_scan_dates(from_text: str, to_text: str) -> list[date]:
    from_date = _date_from_text(from_text, field_name="from")
    to_date = _date_from_text(to_text, field_name="to")
    if to_date < from_date:
        raise ScheduleError("--from must be before or equal to --to")
    return [from_date + timedelta(days=offset) for offset in range((to_date - from_date).days + 1)]


def _date_from_text(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ScheduleError(f"--{field_name} must use YYYY-MM-DD") from error
