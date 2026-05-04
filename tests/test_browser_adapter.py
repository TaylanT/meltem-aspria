from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from email.message import EmailMessage
from pathlib import Path

from aspria_booker.browser_adapter import (
    ActionVerification,
    AspriaBookingPageCollectionSource,
    BrowserCourseObservation,
    BrowserExistingState,
    DryRunBrowserCollector,
    VerifiedActionRunner,
)
from aspria_booker.artifacts import ArtifactStore
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


def config() -> BookerConfig:
    return BookerConfig(
        enabled=True,
        dry_run=True,
        club=ClubConfig(name="Aspria Hannover Maschsee"),
        courses=[CourseConfig("LES MILLS BODYPUMP"), CourseConfig("Hyrox Starter")],
        time_windows={
            "tuesday": [TimeWindow("00:00", "11:00")],
            "thursday": [TimeWindow("18:00", "23:59")],
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


def live_config() -> BookerConfig:
    base = config()
    return BookerConfig(
        enabled=True,
        dry_run=False,
        club=base.club,
        courses=base.courses,
        time_windows=base.time_windows,
        release_job=base.release_job,
        hourly_job=base.hourly_job,
        matching=base.matching,
        default_duration_minutes=base.default_duration_minutes,
        buffer_minutes=base.buffer_minutes,
        retention=base.retention,
        notifications=base.notifications,
        secrets=base.secrets,
    )


@dataclass
class FakeCourseSource:
    observations: list[BrowserCourseObservation]
    existing_states: list[BrowserExistingState]
    select_dates: list[list[date]]
    clicked: bool = False
    artifact_html: str | None = None
    artifact_screenshot: bytes | None = None

    def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        self.select_dates.append(scan_dates)
        return self.observations, self.existing_states

    def click_anything(self) -> None:
        self.clicked = True

    def diagnostic_artifacts(
        self,
        *,
        observation: BrowserCourseObservation | None,
    ) -> tuple[str | None, bytes | None, dict[str, object]]:
        return self.artifact_html, self.artifact_screenshot, {"observed": observation.name if observation else None}


@dataclass
class FakeActionSource:
    observations: list[BrowserCourseObservation]
    existing_states: list[BrowserExistingState]
    selected_dates: list[list[date]]
    booking_verification: ActionVerification = ActionVerification(status="booked")
    waitlist_verification: ActionVerification = ActionVerification(status="waitlisted")
    booking_clicks: list[BrowserCourseObservation] | None = None
    waitlist_clicks: list[BrowserCourseObservation] | None = None
    cancel_clicks: int = 0
    artifact_html: str | None = None
    artifact_screenshot: bytes | None = None

    def __post_init__(self) -> None:
        self.booking_clicks = []
        self.waitlist_clicks = []

    def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        self.selected_dates.append(scan_dates)
        return self.observations, self.existing_states

    def book(self, observation: BrowserCourseObservation) -> ActionVerification:
        assert self.booking_clicks is not None
        self.booking_clicks.append(observation)
        return self.booking_verification

    def join_waitlist(self, observation: BrowserCourseObservation) -> ActionVerification:
        assert self.waitlist_clicks is not None
        self.waitlist_clicks.append(observation)
        return self.waitlist_verification

    def diagnostic_artifacts(
        self,
        *,
        observation: BrowserCourseObservation | None,
    ) -> tuple[str | None, bytes | None, dict[str, object]]:
        return self.artifact_html, self.artifact_screenshot, {"observed": observation.name if observation else None}


class CapturingMailer:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


class ExplodingSource:
    def collect(self, scan_dates: list[date]) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        raise RuntimeError("browser page crashed")

    def book(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise AssertionError("not reached")

    def join_waitlist(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise AssertionError("not reached")

    def diagnostic_artifacts(
        self,
        *,
        observation: BrowserCourseObservation | None,
    ) -> tuple[str | None, bytes | None, dict[str, object]]:
        return "<html><body>crashed page</body></html>", None, {"url": "https://example.invalid/book"}


def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore.open(tmp_path / "history.sqlite")


def test_dry_run_collection_records_observations_and_reports_intended_decisions(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="scan", run_id="run-123")
    source = FakeCourseSource(
        observations=[
            BrowserCourseObservation(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="free",
                available_action="book",
            ),
            BrowserCourseObservation(
                name="Pilates",
                day=date(2026, 5, 5),
                start=time(9, 0),
                duration_minutes=60,
                status="free",
                available_action="book",
            ),
        ],
        existing_states=[],
        select_dates=[],
    )

    result = DryRunBrowserCollector(config=config(), history=history, source=source).collect(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
    )

    assert source.select_dates == [[date(2026, 5, 5)]]
    assert source.clicked is False
    assert [(decision.course_name, decision.action_type, decision.reason) for decision in result.decisions] == [
        ("LES MILLS BODYPUMP", "booking", "free spot matched target")
    ]
    assert [action["action_type"] for action in history.actions_for_run(run_id)] == ["booking"]


def test_dry_run_collection_reports_waitlist_intent_without_clicking(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="hourly", run_id="run-456")
    source = FakeCourseSource(
        observations=[
            BrowserCourseObservation(
                name="Hyrox Starter",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=None,
                status="waitlist_possible",
                available_action="waitlist",
            )
        ],
        existing_states=[],
        select_dates=[],
    )

    result = DryRunBrowserCollector(config=config(), history=history, source=source).collect(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
    )

    assert source.clicked is False
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("waitlist", "would_join_waitlist", "full target course allows waitlist")
    ]
    assert history.actions_for_run(run_id)[0]["action_type"] == "waitlist"


def test_unclear_observations_are_recorded_as_failure_outcomes(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-789")
    source = FakeCourseSource(
        observations=[
            BrowserCourseObservation(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="unclear",
                available_action=None,
            )
        ],
        existing_states=[],
        select_dates=[],
    )

    result = DryRunBrowserCollector(config=config(), history=history, source=source).collect(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
    )

    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("failure", "unclear", "course status was unclear during collection")
    ]
    assert history.actions_for_run(run_id)[0]["action_type"] == "failure"


def test_dry_run_collector_saves_artifacts_for_unclear_observations(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="scan", run_id="run-dry-unclear")
    source = FakeCourseSource(
        observations=[
            BrowserCourseObservation(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="unclear",
                available_action=None,
            )
        ],
        existing_states=[],
        select_dates=[],
        artifact_html="<html><body>unclear course row</body></html>",
    )

    DryRunBrowserCollector(
        config=config(),
        history=history,
        source=source,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    ).collect(run_id=run_id, scan_dates=[date(2026, 5, 5)])

    artifact_dir = tmp_path / "artifacts" / run_id / "20260505T100000Z-unclear-status"
    assert (artifact_dir / "page.html").read_text(encoding="utf-8") == "<html><body>unclear course row</body></html>"
    assert (artifact_dir / "metadata.json").exists()


def test_live_runner_books_free_target_and_sends_deduped_success_notification(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-live-book")
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

    result = VerifiedActionRunner(
        config=live_config(),
        history=history,
        source=source,
        notifications=NotificationService(history=history, mailer=mailer),
    ).run(run_id=run_id, scan_dates=[date(2026, 5, 5)], dry_run=False)

    assert source.booking_clicks == [source.observations[0]]
    assert source.waitlist_clicks == []
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("booking", "success", "booking verified after click")
    ]
    actions = history.actions_for_run(run_id)
    assert [(action["action_type"], action["result"]) for action in actions] == [("booking", "success")]
    assert len(mailer.messages) == 1
    assert history.has_action_notification(actions[0]["action_id"], notification_type="booking_success") is True


def test_live_runner_joins_waitlist_when_full_target_allows_waitlist(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="hourly", run_id="run-live-waitlist")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="Hyrox Starter",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=None,
                status="waitlist_possible",
                available_action="waitlist",
            )
        ],
        existing_states=[],
        selected_dates=[],
    )

    result = VerifiedActionRunner(config=live_config(), history=history, source=source).run(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
        dry_run=False,
    )

    assert source.booking_clicks == []
    assert source.waitlist_clicks == [source.observations[0]]
    assert [(decision.action_type, decision.result) for decision in result.decisions] == [
        ("waitlist", "success")
    ]


def test_live_runner_respects_enabled_and_dry_run_action_gates(tmp_path: Path) -> None:
    disabled_config = live_config()
    disabled_config = BookerConfig(
        enabled=False,
        dry_run=disabled_config.dry_run,
        club=disabled_config.club,
        courses=disabled_config.courses,
        time_windows=disabled_config.time_windows,
        release_job=disabled_config.release_job,
        hourly_job=disabled_config.hourly_job,
        matching=disabled_config.matching,
        default_duration_minutes=disabled_config.default_duration_minutes,
        buffer_minutes=disabled_config.buffer_minutes,
        retention=disabled_config.retention,
        notifications=disabled_config.notifications,
        secrets=disabled_config.secrets,
    )
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-gated")
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

    result = VerifiedActionRunner(config=disabled_config, history=history, source=source).run(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
        dry_run=False,
    )

    assert source.booking_clicks == []
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("booking", "would_book", "live action disabled by safety gates")
    ]


def test_live_runner_records_no_op_for_existing_final_states_without_clicks(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="hourly", run_id="run-existing")
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
        existing_states=[
            BrowserExistingState(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(9, 0),
                state="booked",
                duration_minutes=60,
            )
        ],
        selected_dates=[],
    )

    result = VerifiedActionRunner(config=live_config(), history=history, source=source).run(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
        dry_run=False,
    )

    assert source.booking_clicks == []
    assert source.waitlist_clicks == []
    assert source.cancel_clicks == 0
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("no_op", "already_booked", "existing booking is final")
    ]
    assert [(action["action_type"], action["result"]) for action in history.actions_for_run(run_id)] == [
        ("no_op", "already_booked")
    ]


def test_live_runner_records_visible_final_states_without_clicks(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="hourly", run_id="run-visible-final")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="Hyrox Starter",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=None,
                status="waitlisted",
                available_action=None,
            )
        ],
        existing_states=[],
        selected_dates=[],
    )

    result = VerifiedActionRunner(config=live_config(), history=history, source=source).run(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
        dry_run=False,
    )

    assert source.booking_clicks == []
    assert source.waitlist_clicks == []
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("no_op", "already_waitlisted", "visible waitlist membership is final")
    ]
    assert [(action["action_type"], action["result"]) for action in history.actions_for_run(run_id)] == [
        ("no_op", "already_waitlisted")
    ]


def test_live_runner_records_and_notifies_full_course_without_waitlist(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-full")
    source = FakeActionSource(
        observations=[
            BrowserCourseObservation(
                name="LES MILLS BODYPUMP",
                day=date(2026, 5, 5),
                start=time(10, 0),
                duration_minutes=60,
                status="full",
                available_action=None,
            )
        ],
        existing_states=[],
        selected_dates=[],
    )
    mailer = CapturingMailer()

    result = VerifiedActionRunner(
        config=live_config(),
        history=history,
        source=source,
        notifications=NotificationService(history=history, mailer=mailer),
    ).run(run_id=run_id, scan_dates=[date(2026, 5, 5)], dry_run=False)

    assert source.booking_clicks == []
    assert source.waitlist_clicks == []
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("failure", "full_no_waitlist", "target course is full and waitlist is not available")
    ]
    assert len(mailer.messages) == 1
    assert "needs review" in mailer.messages[0]["Subject"]


def test_live_runner_records_unclear_verification_as_failure(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-verify-unclear")
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
        booking_verification=ActionVerification(status="unclear", reason="button disappeared without booked text"),
    )

    result = VerifiedActionRunner(config=live_config(), history=history, source=source).run(
        run_id=run_id,
        scan_dates=[date(2026, 5, 5)],
        dry_run=False,
    )

    assert source.booking_clicks == [source.observations[0]]
    assert [(decision.action_type, decision.result, decision.reason) for decision in result.decisions] == [
        ("failure", "verification_unclear", "button disappeared without booked text")
    ]


def test_live_runner_saves_failed_action_artifacts_and_includes_path_in_failure_email(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-artifact-failure")
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
        booking_verification=ActionVerification(status="unclear", reason="button disappeared without booked text"),
        artifact_html="<html><body>after click</body></html>",
        artifact_screenshot=b"png bytes",
    )
    mailer = CapturingMailer()

    VerifiedActionRunner(
        config=live_config(),
        history=history,
        source=source,
        notifications=NotificationService(history=history, mailer=mailer),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    ).run(run_id=run_id, scan_dates=[date(2026, 5, 5)], dry_run=False)

    artifact_dir = tmp_path / "artifacts" / run_id / "20260505T100000Z-failed-action"
    assert (artifact_dir / "page.html").read_text(encoding="utf-8") == "<html><body>after click</body></html>"
    assert (artifact_dir / "screenshot.png").read_bytes() == b"png bytes"
    assert str(artifact_dir) in mailer.messages[0].get_content()


def test_live_runner_saves_technical_failure_artifact_when_collection_raises(tmp_path: Path) -> None:
    history = store(tmp_path)
    run_id = history.start_run(command="release", run_id="run-technical-failure")

    try:
        VerifiedActionRunner(
            config=live_config(),
            history=history,
            source=ExplodingSource(),
            artifacts=ArtifactStore(tmp_path / "artifacts"),
        ).run(run_id=run_id, scan_dates=[date(2026, 5, 5)], dry_run=False)
    except RuntimeError as error:
        assert str(error) == "browser page crashed"
    else:
        raise AssertionError("expected collection failure")

    artifact_dirs = list((tmp_path / "artifacts" / run_id).glob("*-technical-failure"))
    assert len(artifact_dirs) == 1
    assert (artifact_dirs[0] / "page.html").read_text(encoding="utf-8") == "<html><body>crashed page</body></html>"


class FakePageLocator:
    def __init__(self, page: "FakeBookingPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return self._page.count(self._selector)

    @property
    def first(self) -> "FakePageLocator":
        return self

    def click(self) -> None:
        self._page.clicked.append(self._selector)
        self._page.apply_click(self._selector)


class FakeBookingPage:
    def __init__(self, pages: dict[str, str], *, url: str = "about:blank") -> None:
        self.pages = pages
        self.url = url
        self.visited: list[str] = []
        self.clicked: list[str] = []

    def goto(self, url: str, **_: object) -> None:
        self.visited.append(url)
        self.url = url

    def content(self) -> str:
        return self.pages.get(self.url, "")

    def locator(self, selector: str) -> FakePageLocator:
        return FakePageLocator(self, selector)

    def count(self, selector: str) -> int:
        if selector == "a:has-text('Kurs Buchen')" and "Kurs Buchen" in self.content():
            return 1
        if selector == "button:has-text('05.05.2026')" and "<button>05.05.2026</button>" in self.content():
            return 1
        return 0

    def apply_click(self, selector: str) -> None:
        if selector == "a:has-text('Kurs Buchen')":
            self.url = "https://example.invalid/kursbuchung"
        if selector == "button:has-text('05.05.2026')":
            self.url = "https://example.invalid/kursbuchung?date=2026-05-05"


def test_aspria_page_source_falls_back_selects_date_and_collects_german_course_states() -> None:
    page = FakeBookingPage(
        {
            "https://example.invalid/discovered": "<main>Startseite</main>",
            "https://www.aspria.com/de/hannover-maschsee": "<a>Kurs Buchen</a>",
            "https://example.invalid/kursbuchung": "<button>05.05.2026</button>",
            "https://example.invalid/kursbuchung?date=2026-05-05": """
                <main>
                  <article>LES MILLS BODYPUMP 05.05.2026 10:00 60 Min. Freie Plaetze Buchen</article>
                  <article>Hyrox Starter 05.05.2026 18:30 Warteliste moeglich Warteliste</article>
                  <section>Meine Buchungen LES MILLS BODYPUMP 05.05.2026 10:00 Gebucht</section>
                  <section>Meine Warteliste Hyrox Starter 05.05.2026 18:30 Warteliste</section>
                </main>
            """,
        }
    )

    observations, existing_states = AspriaBookingPageCollectionSource(
        page=page,
        booking_url="https://example.invalid/discovered",
    ).collect([date(2026, 5, 5)])

    assert page.visited == [
        "https://example.invalid/discovered",
        "https://www.aspria.com/de/hannover-maschsee",
    ]
    assert page.clicked == ["a:has-text('Kurs Buchen')", "button:has-text('05.05.2026')"]
    assert observations == [
        BrowserCourseObservation(
            name="LES MILLS BODYPUMP",
            day=date(2026, 5, 5),
            start=time(10, 0),
            duration_minutes=60,
            status="free",
            available_action="book",
        ),
        BrowserCourseObservation(
            name="Hyrox Starter",
            day=date(2026, 5, 5),
            start=time(18, 30),
            duration_minutes=None,
            status="waitlist_possible",
            available_action="waitlist",
        ),
    ]
    assert existing_states == [
        BrowserExistingState(
            name="LES MILLS BODYPUMP",
            day=date(2026, 5, 5),
            start=time(10, 0),
            state="booked",
            duration_minutes=None,
        ),
        BrowserExistingState(
            name="Hyrox Starter",
            day=date(2026, 5, 5),
            start=time(18, 30),
            state="waitlisted",
            duration_minutes=None,
        ),
    ]


def test_aspria_page_source_does_not_use_english_fallback_status_texts() -> None:
    page = FakeBookingPage(
        {
            "https://example.invalid/kursbuchung": """
                <article>LES MILLS BODYPUMP 05.05.2026 10:00 60 Min. Available Book</article>
            """,
        }
    )

    observations, _ = AspriaBookingPageCollectionSource(
        page=page,
        booking_url="https://example.invalid/kursbuchung",
    ).collect([date(2026, 5, 5)])

    assert observations == [
        BrowserCourseObservation(
            name="LES MILLS BODYPUMP",
            day=date(2026, 5, 5),
            start=time(10, 0),
            duration_minutes=60,
            status="unclear",
            available_action=None,
        )
    ]
