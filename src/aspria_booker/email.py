from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import smtplib
from typing import Protocol

from aspria_booker.config import BookerConfig, ConfigError, SmtpConfig
from aspria_booker.history import HistoryStore


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> None:
        pass


@dataclass(frozen=True)
class CourseActionEmail:
    action_type: str
    course_name: str
    course_date: date
    start_time: str
    result: str
    run_id: str


@dataclass(frozen=True)
class FailureEmail:
    description: str
    run_id: str
    artifact_path: Path | None = None


@dataclass(frozen=True)
class ManualInterventionEmail:
    description: str
    run_id: str
    artifact_path: Path | None = None


@dataclass(frozen=True)
class NotificationPolicy:
    command: str
    dry_run: bool
    explicit_notify: bool = False

    def allows_email(self) -> bool:
        if self.command == "scan" and self.dry_run and not self.explicit_notify:
            return False
        return True


class SMTPMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_email: str,
        to_email: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._from_email = from_email
        self._to_email = to_email
        self._username = username
        self._password = password

    @classmethod
    def from_config(cls, config: BookerConfig) -> SMTPMailer:
        smtp = _require_smtp_config(config.notifications.smtp)
        username = config.secrets.get(smtp.username_env) if smtp.username_env else None
        password = config.secrets.get(smtp.password_env) if smtp.password_env else None
        return cls(
            host=smtp.host,
            port=smtp.port,
            from_email=smtp.from_email,
            to_email=smtp.to_email,
            username=username,
            password=password,
        )

    def send(self, message: EmailMessage) -> None:
        if "From" not in message:
            message["From"] = self._from_email
        if "To" not in message:
            message["To"] = self._to_email
        if self._port == 465:
            with smtplib.SMTP_SSL(self._host, self._port) as smtp:
                self._login_if_configured(smtp)
                smtp.send_message(message)
            return
        with smtplib.SMTP(self._host, self._port) as smtp:
            smtp.starttls()
            self._login_if_configured(smtp)
            smtp.send_message(message)

    def _login_if_configured(self, smtp: smtplib.SMTP) -> None:
        if self._username is not None and self._password is not None:
            smtp.login(self._username, self._password)


class NotificationService:
    def __init__(self, *, history: HistoryStore, mailer: Mailer) -> None:
        self._history = history
        self._mailer = mailer

    def send_course_action_success(self, *, action_id: int, sent_at: datetime | None = None) -> bool:
        action = self._history.action_for_notification(action_id)
        notification_type = f"{action['action_type']}_success"
        if self._history.has_action_notification(action_id, notification_type=notification_type):
            return False
        message = render_course_action_email(
            CourseActionEmail(
                action_type=str(action["action_type"]),
                course_name=str(action["course_name"]),
                course_date=date.fromisoformat(str(action["course_date"])),
                start_time=str(action["start_time"]),
                result=str(action["result"]),
                run_id=str(action["run_id"]),
            )
        )
        self._mailer.send(message)
        self._history.record_action_notification(
            action_id=action_id,
            notification_type=notification_type,
            sent_at=sent_at,
        )
        return True

    def send_known_failure(
        self,
        *,
        failure_key: str,
        description: str,
        run_id: str,
        artifact_path: Path | None = None,
        sent_at: datetime | None = None,
    ) -> bool:
        instant = sent_at or datetime.now(timezone.utc)
        notification_date = instant.astimezone(timezone.utc).date()
        if self._history.has_known_failure_notification(failure_key, notification_date):
            return False
        self._mailer.send(
            render_failure_email(
                FailureEmail(description=description, run_id=run_id, artifact_path=artifact_path)
            )
        )
        self._history.record_known_failure_notification(
            failure_key=failure_key,
            notification_date=notification_date,
            sent_at=instant,
        )
        return True

    def send_manual_intervention(
        self,
        *,
        failure_key: str,
        description: str,
        run_id: str,
        artifact_path: Path | None = None,
        sent_at: datetime | None = None,
    ) -> bool:
        instant = sent_at or datetime.now(timezone.utc)
        notification_date = instant.astimezone(timezone.utc).date()
        notification_type = "manual_intervention"
        if self._history.has_keyed_notification(
            failure_key,
            notification_type=notification_type,
            notification_date=notification_date,
        ):
            return False
        self._mailer.send(
            render_manual_intervention_email(
                ManualInterventionEmail(
                    description=description,
                    run_id=run_id,
                    artifact_path=artifact_path,
                )
            )
        )
        self._history.record_keyed_notification(
            failure_key=failure_key,
            notification_type=notification_type,
            notification_date=notification_date,
            sent_at=instant,
        )
        return True


def render_course_action_email(data: CourseActionEmail) -> EmailMessage:
    action_label = "Booking" if data.action_type == "booking" else "Waitlist"
    message = EmailMessage()
    message["Subject"] = f"Aspria Booker: {action_label} {data.result}"
    message.set_content(
        "\n".join(
            [
                f"{action_label} action result: {data.result}",
                f"Course: {data.course_name}",
                f"Date/time: {data.course_date.isoformat()} {data.start_time}",
                f"Run ID: {data.run_id}",
            ]
        )
    )
    return message


def render_failure_email(data: FailureEmail) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Aspria Booker: action needs review"
    lines = [
        "A booking run needs review.",
        f"Description: {data.description}",
        f"Run ID: {data.run_id}",
    ]
    if data.artifact_path is not None:
        lines.append(f"Artifact path: {data.artifact_path}")
    message.set_content("\n".join(lines))
    return message


def render_manual_intervention_email(data: ManualInterventionEmail) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Aspria Booker: Human action required for login"
    lines = [
        "Human action is required to restore the Aspria login session.",
        f"Description: {data.description}",
        f"Run ID: {data.run_id}",
    ]
    if data.artifact_path is not None:
        lines.append(f"Artifact path: {data.artifact_path}")
    message.set_content("\n".join(lines))
    return message


def render_test_email(config: BookerConfig) -> EmailMessage:
    smtp = _require_smtp_config(config.notifications.smtp)
    message = EmailMessage()
    message["Subject"] = "Aspria Booker: SMTP test"
    message["From"] = smtp.from_email
    message["To"] = smtp.to_email
    message.set_content("SMTP settings are valid enough to send this Aspria Booker test email.")
    return message


@dataclass(frozen=True)
class CompleteSmtpConfig:
    host: str
    port: int
    from_email: str
    to_email: str
    username_env: str | None = None
    password_env: str | None = None


def _require_smtp_config(smtp: SmtpConfig) -> CompleteSmtpConfig:
    missing = [
        name
        for name, value in [
            ("SMTP host", smtp.host),
            ("SMTP port", smtp.port),
            ("SMTP from_email", smtp.from_email),
            ("SMTP to_email", smtp.to_email),
        ]
        if value in (None, "")
    ]
    if missing:
        raise ConfigError("missing SMTP values: " + ", ".join(missing))
    assert smtp.host is not None
    assert smtp.port is not None
    assert smtp.from_email is not None
    assert smtp.to_email is not None
    return CompleteSmtpConfig(
        host=smtp.host,
        port=smtp.port,
        from_email=smtp.from_email,
        to_email=smtp.to_email,
        username_env=smtp.username_env,
        password_env=smtp.password_env,
    )
