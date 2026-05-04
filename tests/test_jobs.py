from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path

from aspria_booker.browser_adapter import ActionVerification, BrowserCourseObservation, BrowserExistingState
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
from aspria_booker.email import NotificationService
from aspria_booker.history import HistoryStore
from aspria_booker.jobs import JobPaths, run_hourly_job, run_release_job
from aspria_booker.session import LoginOutcome, LoginStatus


def live_config(*, notifications: bool = False) -> BookerConfig:
    return BookerConfig(
        enabled=True,
        dry_run=False,
        club=ClubConfig(name="Aspria Hannover Maschsee", booking_url="https://example.invalid/book"),
        courses=[CourseConfig("LES MILLS BODYPUMP"), CourseConfig("Hyrox Starter")],
        time_windows={
            "tuesday": [TimeWindow("00:00", "11:00")],
            "thursday": [TimeWindow("18:00", "23:59")],
        },
        release_job=ReleaseJobConfig("20:58", "21:00", 5, "21:02", 60),
        hourly_job=HourlyJobConfig(lookahead_days=3),
        matching=MatchingConfig(exact_normalized=True, fuzzy=False),
        default_duration_minutes=60,
        buffer_minutes=15,
        retention=RetentionConfig(history_days=90, artifacts_days=14),
        notifications=NotificationConfig(
            enabled=notifications,
            smtp=SmtpConfig(to_email="ops@example.invalid"),
        ),
        secrets={"ASPRIA_EMAIL": "person@example.invalid", "ASPRIA_PASSWORD": "secret"},
    )


class FakeSession:
    def __init__(self, outcome: LoginOutcome | None = None) -> None:
        self.outcome = outcome or LoginOutcome(LoginStatus.AUTHENTICATED, "session ok")
        self.run_ids: list[str | None] = []

    def ensure_authenticated(self, *, run_id: str | None = None) -> LoginOutcome:
        self.run_ids.append(run_id)
        return self.outcome


@dataclass
class FakeActionSource:
    observations: list[BrowserCourseObservation]
    existing_states: list[BrowserExistingState]
    selected_dates: list[list[date]]
    booking_verification: ActionVerification = ActionVerification(status="booked")
    waitlist_verification: ActionVerification = ActionVerification(status="waitlisted")

    def __post_init__(self) -> None:
        self.booking_clicks: list[BrowserCourseObservation] = []
        self.waitlist_clicks: list[BrowserCourseObservation] = []

    def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        self.selected_dates.append(scan_dates)
        return self.observations, self.existing_states

    def book(self, observation: BrowserCourseObservation) -> ActionVerification:
        self.booking_clicks.append(observation)
        return self.booking_verification

    def join_waitlist(self, observation: BrowserCourseObservation) -> ActionVerification:
        self.waitlist_clicks.append(observation)
        return self.waitlist_verification

    def diagnostic_artifacts(
        self,
        *,
        observation: BrowserCourseObservation | None,
    ) -> tuple[str | None, bytes | None, dict[str, object]]:
        return "<html><body>diagnostic</body></html>", None, {"course": observation.name if observation else None}


class CapturingMailer:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


class FakeClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.instant

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.instant += timedelta(seconds=seconds)


def test_release_job_authenticates_polls_target_date_and_books_live_course(tmp_path: Path) -> None:
    config = live_config(notifications=True)
    history = HistoryStore.open(tmp_path / "history.sqlite")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="free",
                available_action="book",
            )
        ],
        existing_states=[],
        selected_dates=[],
    )
    mailer = CapturingMailer()

    result = run_release_job(
        config=config,
        history=history,
        source=source,
        session=FakeSession(),
        notifications=NotificationService(history=history, mailer=mailer),
        paths=JobPaths(history_path=tmp_path / "history.sqlite", artifact_root=tmp_path / "artifacts"),
        today=date(2026, 5, 2),
        clock=FakeClock(datetime(2026, 5, 2, 20, 58)),
    )

    assert source.selected_dates == [[date(2026, 5, 5)]]
    assert source.booking_clicks == [source.observations[0]]
    assert source.waitlist_clicks == []
    assert result.run_id in history.run_ids()
    assert [(action["action_type"], action["result"]) for action in history.actions_for_run(result.run_id)] == [
        ("booking", "success")
    ]
    assert len(mailer.messages) == 1


def test_hourly_job_scans_today_through_three_days_and_joins_waitlist(tmp_path: Path) -> None:
    config = live_config()
    history = HistoryStore.open(tmp_path / "history.sqlite")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="Hyrox Starter",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="waitlist_possible",
                available_action="waitlist",
            )
        ],
        existing_states=[],
        selected_dates=[],
    )

    result = run_hourly_job(
        config=config,
        history=history,
        source=source,
        session=FakeSession(),
        notifications=None,
        paths=JobPaths(history_path=tmp_path / "history.sqlite", artifact_root=tmp_path / "artifacts"),
        today=date(2026, 5, 4),
    )

    assert source.selected_dates == [[date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7)]]
    assert source.booking_clicks == []
    assert source.waitlist_clicks == [source.observations[0]]
    assert [(action["action_type"], action["result"]) for action in history.actions_for_run(result.run_id)] == [
        ("waitlist", "success")
    ]


def test_hourly_job_accepts_reused_storage_session(tmp_path: Path) -> None:
    config = live_config()
    history = HistoryStore.open(tmp_path / "history.sqlite")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="Hyrox Starter",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="waitlist_possible",
                available_action="waitlist",
            )
        ],
        existing_states=[],
        selected_dates=[],
    )

    result = run_hourly_job(
        config=config,
        history=history,
        source=source,
        session=FakeSession(LoginOutcome(LoginStatus.REUSED_STORAGE, "saved storage state is valid")),
        notifications=None,
        paths=JobPaths(history_path=tmp_path / "history.sqlite", artifact_root=tmp_path / "artifacts"),
        today=date(2026, 5, 4),
        dry_run=True,
    )

    assert source.selected_dates == [[date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7)]]
    assert result.actions == 1
    assert [(action["action_type"], action["result"]) for action in history.actions_for_run(result.run_id)] == [
        ("waitlist", "would_join_waitlist")
    ]


def test_job_saves_artifact_and_records_failure_when_collection_crashes(tmp_path: Path) -> None:
    class ExplodingSource(FakeActionSource):
        def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
            raise RuntimeError("crashed with ASPRIA_PASSWORD=secret")

    config = live_config()
    history = HistoryStore.open(tmp_path / "history.sqlite")

    result = run_hourly_job(
        config=config,
        history=history,
        source=ExplodingSource(observations=[], existing_states=[], selected_dates=[]),
        session=FakeSession(),
        notifications=None,
        paths=JobPaths(history_path=tmp_path / "history.sqlite", artifact_root=tmp_path / "artifacts"),
        today=date(2026, 5, 4),
    )

    actions = history.actions_for_run(result.run_id)
    assert [(action["action_type"], action["result"]) for action in actions] == [("failure", "technical_error")]
    artifact_dirs = list((tmp_path / "artifacts" / result.run_id).glob("*-technical-failure"))
    assert len(artifact_dirs) == 1
    metadata = (artifact_dirs[0] / "metadata.json").read_text(encoding="utf-8")
    assert "ASPRIA_PASSWORD=[redacted]" in metadata
    assert "ASPRIA_PASSWORD=secret" not in metadata
