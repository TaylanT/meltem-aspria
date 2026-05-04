from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
import re
from typing import Literal

from aspria_booker.config import BookerConfig


ExistingState = Literal["booked", "waitlisted"]


@dataclass(frozen=True)
class ObservedCourse:
    name: str
    day: date
    start: time
    duration_minutes: int | None = None


@dataclass(frozen=True)
class ExistingCourseState:
    name: str
    day: date
    start: time
    state: ExistingState
    duration_minutes: int | None = None


@dataclass(frozen=True)
class BookingTarget:
    course: ObservedCourse
    matched_config_name: str


@dataclass(frozen=True)
class _Interval:
    day: date
    start_minutes: int
    end_minutes: int


def choose_booking_targets(
    config: BookerConfig,
    observed_courses: list[ObservedCourse],
    *,
    existing_states: list[ExistingCourseState],
) -> list[BookingTarget]:
    target_names = {_normalize(course.name): course.name for course in config.courses}
    final_course_days = _final_course_days(existing_states)
    blocked_intervals = [
        _interval(state.day, state.start, state.duration_minutes, config)
        for state in existing_states
        if state.state == "booked"
    ]
    selected_intervals: list[_Interval] = []
    selected_course_days: set[tuple[str, date]] = set()
    choices: list[BookingTarget] = []

    candidates = [
        course
        for course in observed_courses
        if _normalize(course.name) in target_names
        and _is_in_allowed_window(config, course.day, course.start)
    ]
    candidates.sort(key=lambda course: (course.day, course.start, _normalize(course.name)))

    for course in candidates:
        normalized_name = _normalize(course.name)
        course_day = (normalized_name, course.day)
        if course_day in final_course_days or course_day in selected_course_days:
            continue
        interval = _interval(course.day, course.start, course.duration_minutes, config)
        if any(_overlaps(interval, blocked) for blocked in blocked_intervals):
            continue
        if any(_overlaps(interval, selected) for selected in selected_intervals):
            continue
        choices.append(BookingTarget(course=course, matched_config_name=target_names[normalized_name]))
        selected_course_days.add(course_day)
        selected_intervals.append(interval)

    return choices


def _final_course_days(existing_states: list[ExistingCourseState]) -> set[tuple[str, date]]:
    return {
        (_normalize(state.name), state.day)
        for state in existing_states
        if state.state in {"booked", "waitlisted"}
    }


def _is_in_allowed_window(config: BookerConfig, day: date, start: time) -> bool:
    weekday = day.strftime("%A").lower()
    start_minutes = _minutes(start)
    return any(
        _minutes_from_text(window.start) <= start_minutes <= _minutes_from_text(window.end)
        for window in config.time_windows.get(weekday, [])
    )


def _interval(
    day: date,
    start: time,
    duration_minutes: int | None,
    config: BookerConfig,
) -> _Interval:
    start_minutes = _minutes(start)
    duration = duration_minutes if duration_minutes is not None else config.default_duration_minutes
    return _Interval(
        day=day,
        start_minutes=start_minutes,
        end_minutes=start_minutes + duration + config.buffer_minutes,
    )


def _overlaps(left: _Interval, right: _Interval) -> bool:
    if left.day != right.day:
        return False
    return left.start_minutes < right.end_minutes and right.start_minutes < left.end_minutes


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _minutes_from_text(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()
