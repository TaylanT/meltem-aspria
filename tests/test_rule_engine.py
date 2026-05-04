from __future__ import annotations

from datetime import date, time

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
from aspria_booker.rules import ExistingCourseState, ExistingState, ObservedCourse, choose_booking_targets


def config() -> BookerConfig:
    return BookerConfig(
        enabled=True,
        dry_run=True,
        club=ClubConfig(name="Aspria Hannover Maschsee"),
        courses=[CourseConfig("LES MILLS BODYPUMP"), CourseConfig("Hyrox Starter")],
        time_windows={
            "tuesday": [TimeWindow("00:00", "11:00")],
            "thursday": [TimeWindow("18:00", "23:59")],
            "friday": [TimeWindow("18:00", "23:59")],
            "saturday": [TimeWindow("09:00", "20:00")],
            "sunday": [TimeWindow("09:00", "20:00")],
        },
        release_job=ReleaseJobConfig("20:58", "21:10", 5, "22:00", 60),
        hourly_job=HourlyJobConfig(lookahead_days=3),
        matching=MatchingConfig(exact_normalized=True, fuzzy=False),
        default_duration_minutes=60,
        buffer_minutes=15,
        retention=RetentionConfig(history_days=90, artifacts_days=14),
        notifications=NotificationConfig(enabled=False, smtp=SmtpConfig()),
        secrets={},
    )


def course(name: str, day: date, start: time, duration: int | None = 60) -> ObservedCourse:
    return ObservedCourse(name=name, day=day, start=start, duration_minutes=duration)


def existing(
    name: str,
    day: date,
    start: time,
    *,
    state: ExistingState = "booked",
    duration: int | None = 60,
) -> ExistingCourseState:
    return ExistingCourseState(
        name=name,
        day=day,
        start=start,
        state=state,
        duration_minutes=duration,
    )


def test_matches_course_names_exactly_after_case_and_whitespace_normalization() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("  les   mills\tbodypump  ", date(2026, 5, 5), time(10, 0)),
            course("LES MILLS BODYPUMP 60", date(2026, 5, 5), time(9, 0)),
        ],
        existing_states=[],
    )

    assert [choice.course.name for choice in choices] == ["  les   mills\tbodypump  "]


def test_applies_weekly_window_boundaries_without_fuzzy_matching() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("LES MILLS BODYPUMP", date(2026, 5, 4), time(10, 0)),
            course("LES MILLS BODYPUMP", date(2026, 5, 5), time(11, 0)),
            course("LES MILLS BODYPUMP", date(2026, 5, 6), time(10, 0)),
            course("Hyrox Starter", date(2026, 5, 7), time(17, 59)),
            course("Hyrox Starter", date(2026, 5, 8), time(18, 0)),
            course("LES MILLS BODYPUMP", date(2026, 5, 9), time(9, 0)),
            course("Hyrox Starter", date(2026, 5, 10), time(20, 0)),
            course("Hyrox Starter", date(2026, 5, 10), time(20, 1)),
        ],
        existing_states=[],
    )

    assert [(choice.course.name, choice.course.day, choice.course.start) for choice in choices] == [
        ("LES MILLS BODYPUMP", date(2026, 5, 5), time(11, 0)),
        ("Hyrox Starter", date(2026, 5, 8), time(18, 0)),
        ("LES MILLS BODYPUMP", date(2026, 5, 9), time(9, 0)),
        ("Hyrox Starter", date(2026, 5, 10), time(20, 0)),
    ]


def test_existing_bookings_block_overlaps_and_same_course_type_duplicates() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("Hyrox Starter", date(2026, 5, 5), time(10, 0)),
            course("LES MILLS BODYPUMP", date(2026, 5, 5), time(11, 0)),
        ],
        existing_states=[
            existing("LES MILLS BODYPUMP", date(2026, 5, 5), time(8, 0)),
            existing("Pilates", date(2026, 5, 5), time(10, 30)),
        ],
    )

    assert choices == []


def test_existing_waitlist_membership_is_final_no_op_for_that_course_day() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("LES MILLS BODYPUMP", date(2026, 5, 5), time(9, 0)),
            course("Hyrox Starter", date(2026, 5, 5), time(10, 30)),
        ],
        existing_states=[existing("LES MILLS BODYPUMP", date(2026, 5, 5), time(9, 0), state="waitlisted")],
    )

    assert [choice.course.name for choice in choices] == ["Hyrox Starter"]


def test_both_target_courses_can_be_selected_on_same_day_when_they_do_not_overlap() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("LES MILLS BODYPUMP", date(2026, 5, 5), time(9, 0)),
            course("Hyrox Starter", date(2026, 5, 5), time(10, 30)),
        ],
        existing_states=[],
    )

    assert [choice.course.name for choice in choices] == ["LES MILLS BODYPUMP", "Hyrox Starter"]


def test_same_course_type_resolves_to_earliest_allowed_start_time() -> None:
    choices = choose_booking_targets(
        config(),
        [
            course("Hyrox Starter", date(2026, 5, 5), time(10, 0)),
            course("Hyrox Starter", date(2026, 5, 5), time(9, 0)),
            course("Hyrox Starter", date(2026, 5, 5), time(11, 0)),
        ],
        existing_states=[],
    )

    assert [(choice.course.name, choice.course.start) for choice in choices] == [
        ("Hyrox Starter", time(9, 0))
    ]


def test_overlap_uses_visible_duration_or_default_duration_plus_buffer() -> None:
    visible_duration_choices = choose_booking_targets(
        config(),
        [course("Hyrox Starter", date(2026, 5, 5), time(10, 0), duration=None)],
        existing_states=[existing("Pilates", date(2026, 5, 5), time(9, 0), duration=45)],
    )
    default_duration_choices = choose_booking_targets(
        config(),
        [
            course("Hyrox Starter", date(2026, 5, 5), time(10, 0), duration=None),
            course("Hyrox Starter", date(2026, 5, 5), time(10, 15), duration=None),
        ],
        existing_states=[existing("Pilates", date(2026, 5, 5), time(9, 0), duration=None)],
    )

    assert [(choice.course.name, choice.course.start) for choice in visible_duration_choices] == [
        ("Hyrox Starter", time(10, 0)),
    ]
    assert [(choice.course.name, choice.course.start) for choice in default_duration_choices] == [
        ("Hyrox Starter", time(10, 15)),
    ]
