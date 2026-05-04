from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from aspria_booker.history import ActionType, HistoryStore


def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore.open(tmp_path / "history.sqlite")


def test_runs_observations_and_actions_are_recorded_with_stable_ids(tmp_path: Path) -> None:
    history = store(tmp_path)
    started_at = datetime(2026, 5, 3, 18, 55, tzinfo=timezone.utc)

    run_id = history.start_run(command="hourly", started_at=started_at)
    same_run_id = history.start_run(command="hourly", started_at=started_at, run_id=run_id)
    observation_id = history.record_course_observation(
        run_id=run_id,
        scan_date=date(2026, 5, 6),
        course_name="LES MILLS BODYPUMP",
        course_date=date(2026, 5, 6),
        start_time="21:00",
        duration_minutes=60,
        status="free",
        available_action="book",
    )
    action_id = history.record_action(
        run_id=run_id,
        observation_id=observation_id,
        action_type="booking",
        result="success",
        reason="free spot matched target",
        occurred_at=started_at,
    )

    assert same_run_id == run_id
    assert history.actions_for_run(run_id) == [
        {
            "action_id": action_id,
            "action_type": "booking",
            "result": "success",
            "course_name": "LES MILLS BODYPUMP",
            "scan_date": "2026-05-06",
        }
    ]


def test_action_notification_deduplication_can_be_checked_before_sending(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release")
    action_id = history.record_action(
        run_id=run_id,
        observation_id=None,
        action_type="waitlist",
        result="success",
        reason="full course allowed waitlist",
    )

    assert history.has_action_notification(action_id, notification_type="waitlist_success") is False

    history.record_action_notification(
        action_id=action_id,
        notification_type="waitlist_success",
        sent_at=datetime(2026, 5, 3, 19, 1, tzinfo=timezone.utc),
    )

    assert history.has_action_notification(action_id, notification_type="waitlist_success") is True
    assert history.has_action_notification(action_id, notification_type="booking_success") is False


def test_booking_waitlist_no_op_and_failure_actions_can_be_recorded(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="scan")

    action_types: tuple[ActionType, ...] = ("booking", "waitlist", "no_op", "failure")
    for action_type in action_types:
        history.record_action(
            run_id=run_id,
            observation_id=None,
            action_type=action_type,
            result="recorded",
            reason=f"{action_type} outcome",
        )

    assert [action["action_type"] for action in history.actions_for_run(run_id)] == [
        "booking",
        "waitlist",
        "no_op",
        "failure",
    ]


def test_known_failure_notifications_are_deduplicated_per_day(tmp_path: Path) -> None:
    history = store(tmp_path)

    assert history.has_known_failure_notification("login-intervention", date(2026, 5, 3)) is False

    history.record_known_failure_notification(
        failure_key="login-intervention",
        notification_date=date(2026, 5, 3),
        sent_at=datetime(2026, 5, 3, 19, 0, tzinfo=timezone.utc),
    )

    assert history.has_known_failure_notification("login-intervention", date(2026, 5, 3)) is True
    assert history.has_known_failure_notification("login-intervention", date(2026, 5, 4)) is False


def test_history_retention_deletes_records_before_cutoff(tmp_path: Path) -> None:
    history = store(tmp_path)
    old_run = history.start_run(
        command="hourly",
        started_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        run_id="old-run",
    )
    kept_run = history.start_run(
        command="hourly",
        started_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        run_id="kept-run",
    )
    old_action = history.record_action(run_id=old_run, observation_id=None, action_type="failure", result="failure")
    history.record_action_notification(action_id=old_action, notification_type="known_failure")

    deleted = history.cleanup_history_retention(retention_days=90, today=date(2026, 5, 3))

    assert deleted == 1
    assert history.run_ids() == [kept_run]
    assert history.has_action_notification(old_action, notification_type="known_failure") is False
