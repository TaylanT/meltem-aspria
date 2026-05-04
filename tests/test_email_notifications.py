from __future__ import annotations

from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from aspria_booker.email import (
    CourseActionEmail,
    FailureEmail,
    ManualInterventionEmail,
    NotificationPolicy,
    NotificationService,
    render_course_action_email,
    render_failure_email,
    render_manual_intervention_email,
)
from aspria_booker.history import HistoryStore


class CapturingMailer:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore.open(tmp_path / "history.sqlite")


def test_booking_success_email_includes_course_time_result_and_run_id() -> None:
    message = render_course_action_email(
        CourseActionEmail(
            action_type="booking",
            course_name="LES MILLS BODYPUMP",
            course_date=date(2026, 5, 6),
            start_time="21:00",
            result="success",
            run_id="run-123",
        )
    )

    assert "Booking success" in message["Subject"]
    body = message.get_content()
    assert "LES MILLS BODYPUMP" in body
    assert "2026-05-06 21:00" in body
    assert "success" in body
    assert "run-123" in body


def test_waitlist_success_email_includes_course_time_result_and_run_id() -> None:
    message = render_course_action_email(
        CourseActionEmail(
            action_type="waitlist",
            course_name="Hyrox Starter",
            course_date=date(2026, 5, 7),
            start_time="18:30",
            result="success",
            run_id="run-456",
        )
    )

    assert "Waitlist success" in message["Subject"]
    body = message.get_content()
    assert "Hyrox Starter" in body
    assert "2026-05-07 18:30" in body
    assert "success" in body
    assert "run-456" in body


def test_failure_email_includes_description_run_id_and_artifact_path_without_attachment() -> None:
    message = render_failure_email(
        FailureEmail(
            description="Booking status was unclear after refresh.",
            run_id="run-failure",
            artifact_path=Path("artifacts/run-failure/status.html"),
        )
    )

    body = message.get_content()
    assert "Booking status was unclear after refresh." in body
    assert "run-failure" in body
    assert "artifacts/run-failure/status.html" in body
    assert not message.is_multipart()


def test_manual_intervention_email_identifies_required_human_login() -> None:
    message = render_manual_intervention_email(
        ManualInterventionEmail(
            description="Login showed 2FA challenge.",
            run_id="run-login",
            artifact_path=Path("artifacts/run-login/login.png"),
        )
    )

    body = message.get_content()
    assert "Human action required" in message["Subject"]
    assert "Human action is required" in body
    assert "2FA challenge" in body
    assert "run-login" in body
    assert "bypass" not in body.lower()


def test_successful_action_notifications_are_sent_once_per_action(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-action")
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
    )
    mailer = CapturingMailer()
    service = NotificationService(history=history, mailer=mailer)

    assert service.send_course_action_success(action_id=action_id) is True
    assert service.send_course_action_success(action_id=action_id) is False

    assert len(mailer.messages) == 1
    assert history.has_action_notification(action_id, notification_type="booking_success") is True


def test_known_failure_notifications_are_sent_once_per_day(tmp_path: Path) -> None:
    history = store(tmp_path)
    mailer = CapturingMailer()
    service = NotificationService(history=history, mailer=mailer)
    sent_at = datetime(2026, 5, 3, 19, 0, tzinfo=timezone.utc)

    assert service.send_known_failure(
        failure_key="unclear-status:bodypump",
        description="Status remained unclear.",
        run_id="run-failure",
        artifact_path=Path("artifacts/run-failure/status.png"),
        sent_at=sent_at,
    ) is True
    assert service.send_known_failure(
        failure_key="unclear-status:bodypump",
        description="Status remained unclear.",
        run_id="run-later",
        sent_at=sent_at,
    ) is False

    assert len(mailer.messages) == 1
    assert history.has_known_failure_notification("unclear-status:bodypump", date(2026, 5, 3)) is True


def test_manual_intervention_notifications_are_sent_once_per_day(tmp_path: Path) -> None:
    history = store(tmp_path)
    mailer = CapturingMailer()
    service = NotificationService(history=history, mailer=mailer)
    sent_at = datetime(2026, 5, 3, 19, 30, tzinfo=timezone.utc)

    assert service.send_manual_intervention(
        failure_key="login:manual-intervention",
        description="Login showed 2FA challenge.",
        run_id="run-login",
        artifact_path=Path("artifacts/run-login/login.png"),
        sent_at=sent_at,
    ) is True
    assert service.send_manual_intervention(
        failure_key="login:manual-intervention",
        description="Login still needs review.",
        run_id="run-login-later",
        sent_at=sent_at,
    ) is False

    assert len(mailer.messages) == 1
    assert "Human action required" in mailer.messages[0]["Subject"]
    assert history.has_keyed_notification(
        "login:manual-intervention",
        notification_type="manual_intervention",
        notification_date=date(2026, 5, 3),
    ) is True


def test_dry_run_scan_notifications_require_explicit_notify() -> None:
    assert NotificationPolicy(command="scan", dry_run=True, explicit_notify=False).allows_email() is False
    assert NotificationPolicy(command="scan", dry_run=True, explicit_notify=True).allows_email() is True
    assert NotificationPolicy(command="release", dry_run=True, explicit_notify=False).allows_email() is True
