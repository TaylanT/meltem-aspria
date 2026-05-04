from __future__ import annotations

from datetime import date

import pytest

from aspria_booker.config import (
    BookerConfig,
    ClubConfig,
    CourseConfig,
    HourlyJobConfig,
    MatchingConfig,
    NotificationConfig,
    ReleaseJobConfig,
    RetentionConfig,
    SmtpConfig,
    TimeWindow,
)
from aspria_booker.schedule import (
    ScheduleError,
    hourly_scan_dates,
    manual_scan_dates,
    release_schedule,
)


def config() -> BookerConfig:
    return BookerConfig(
        enabled=True,
        dry_run=True,
        club=ClubConfig(name="Aspria Hannover Maschsee"),
        courses=[CourseConfig("LES MILLS BODYPUMP")],
        time_windows={"tuesday": [TimeWindow("00:00", "11:00")]},
        release_job=ReleaseJobConfig("20:58", "21:10", 5, "22:00", 60),
        hourly_job=HourlyJobConfig(lookahead_days=3),
        matching=MatchingConfig(exact_normalized=True, fuzzy=False),
        default_duration_minutes=60,
        buffer_minutes=15,
        retention=RetentionConfig(history_days=90, artifacts_days=14),
        notifications=NotificationConfig(enabled=False, smtp=SmtpConfig()),
        secrets={},
    )


def test_release_schedule_targets_today_plus_three_days_and_uses_configured_windows() -> None:
    schedule = release_schedule(config(), today=date(2026, 5, 3))

    assert schedule.target_date == date(2026, 5, 6)
    assert schedule.tight.start == "20:58"
    assert schedule.tight.end == "21:10"
    assert schedule.tight.interval_seconds == 5
    assert schedule.slow.start == "21:10"
    assert schedule.slow.end == "22:00"
    assert schedule.slow.interval_seconds == 60


def test_hourly_scan_dates_include_today_through_configured_lookahead() -> None:
    assert hourly_scan_dates(config(), today=date(2026, 5, 3)) == [
        date(2026, 5, 3),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 5, 6),
    ]


def test_manual_scan_dates_accept_explicit_inclusive_ranges() -> None:
    assert manual_scan_dates("2026-05-03", "2026-05-05") == [
        date(2026, 5, 3),
        date(2026, 5, 4),
        date(2026, 5, 5),
    ]


@pytest.mark.parametrize(
    ("from_text", "to_text", "message"),
    [
        ("2026/05/03", "2026-05-05", "YYYY-MM-DD"),
        ("2026-05-06", "2026-05-05", "before or equal"),
    ],
)
def test_manual_scan_dates_reject_invalid_ranges(
    from_text: str,
    to_text: str,
    message: str,
) -> None:
    with pytest.raises(ScheduleError, match=message):
        manual_scan_dates(from_text, to_text)
