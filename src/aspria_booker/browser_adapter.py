from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
import re
from typing import Any, Literal, Protocol

from aspria_booker.artifacts import ArtifactPaths, ArtifactStore
from aspria_booker.config import BookerConfig, redact
from aspria_booker.email import NotificationService
from aspria_booker.history import ActionType, HistoryStore
from aspria_booker.rules import ExistingCourseState, ExistingState, ObservedCourse, choose_booking_targets
from aspria_booker.session import ASPRIA_HANNOVER_MASCHSEE_URL, booking_start_url, configured_storage_state_path


CourseStatus = Literal["booked", "waitlisted", "free", "full", "waitlist_possible", "unclear"]
AvailableAction = Literal["book", "waitlist"]


@dataclass(frozen=True)
class BrowserCourseObservation:
    name: str
    day: date
    start: time
    duration_minutes: int | None
    status: CourseStatus
    available_action: AvailableAction | None


@dataclass(frozen=True)
class BrowserExistingState:
    name: str
    day: date
    start: time
    state: ExistingState
    duration_minutes: int | None


@dataclass(frozen=True)
class DryRunDecision:
    course_name: str
    course_date: date
    start_time: time
    action_type: ActionType
    result: str
    reason: str
    observation_id: int | None


@dataclass(frozen=True)
class DryRunCollectionResult:
    observations: list[BrowserCourseObservation]
    existing_states: list[BrowserExistingState]
    decisions: list[DryRunDecision]


@dataclass(frozen=True)
class ActionVerification:
    status: CourseStatus
    reason: str = ""


@dataclass(frozen=True)
class VerifiedActionResult:
    observations: list[BrowserCourseObservation]
    existing_states: list[BrowserExistingState]
    decisions: list[DryRunDecision]


class CourseCollectionSource(Protocol):
    def collect(
        self,
        scan_dates: list[date],
    ) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        raise NotImplementedError


class CourseActionSource(CourseCollectionSource, Protocol):
    def book(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise NotImplementedError

    def join_waitlist(self, observation: BrowserCourseObservation) -> ActionVerification:
        raise NotImplementedError


class AspriaBookingPageCollectionSource:
    def __init__(
        self,
        *,
        page: Any,
        booking_url: str | None,
        fallback_url: str = ASPRIA_HANNOVER_MASCHSEE_URL,
    ) -> None:
        self._page = page
        self._booking_url = booking_url
        self._fallback_url = fallback_url

    def collect(
        self,
        scan_dates: list[date],
    ) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        observations: list[BrowserCourseObservation] = []
        existing_states: list[BrowserExistingState] = []
        self._open_booking_page()
        for scan_date in scan_dates:
            self._select_scan_date(scan_date)
            page_text_blocks = _extract_visible_text_blocks(str(self._page.content()))
            observations.extend(_course_observations_from_blocks(page_text_blocks, scan_date))
            existing_states.extend(_existing_states_from_blocks(page_text_blocks, scan_date))
        return observations, existing_states

    def _open_booking_page(self) -> None:
        if self._booking_url is not None:
            self._page.goto(self._booking_url, wait_until="networkidle")
            if _looks_like_booking_page(str(self._page.content())):
                return
        self._page.goto(self._fallback_url, wait_until="networkidle")
        _click_first_available(self._page, ("a:has-text('Kurs Buchen')", "button:has-text('Kurs Buchen')"))

    def _select_scan_date(self, scan_date: date) -> None:
        date_texts = (
            scan_date.strftime("%d.%m.%Y"),
            scan_date.strftime("%-d.%-m.%Y"),
            scan_date.isoformat(),
        )
        selectors = tuple(
            selector
            for text in date_texts
            for selector in (f"button:has-text('{text}')", f"a:has-text('{text}')")
        )
        _click_first_available(self._page, selectors)


class PlaywrightCourseCollectionSource:
    def __init__(
        self,
        *,
        config: BookerConfig,
        playwright_factory: Any | None = None,
    ) -> None:
        self._config = config
        self._playwright_factory = playwright_factory or _default_playwright_factory

    def collect(
        self,
        scan_dates: list[date],
    ) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
        with self._playwright_factory() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context_kwargs: dict[str, object] = {}
                storage_state_path = configured_storage_state_path(self._config.club)
                if storage_state_path.exists():
                    context_kwargs["storage_state"] = str(storage_state_path)
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                return AspriaBookingPageCollectionSource(
                    page=page,
                    booking_url=booking_start_url(self._config.club),
                ).collect(scan_dates)
            finally:
                browser.close()


class DryRunBrowserCollector:
    def __init__(
        self,
        *,
        config: BookerConfig,
        history: HistoryStore,
        source: CourseCollectionSource,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._config = config
        self._history = history
        self._source = source
        self._artifacts = artifacts

    def collect(self, *, run_id: str, scan_dates: list[date]) -> DryRunCollectionResult:
        observations, existing_states = _collect_with_technical_artifact(
            self._source,
            self._artifacts,
            run_id=run_id,
            scan_dates=scan_dates,
        )
        observation_ids = {
            _observation_key(observation): self._history.record_course_observation(
                run_id=run_id,
                scan_date=observation.day,
                course_name=observation.name,
                course_date=observation.day,
                start_time=_time_text(observation.start),
                duration_minutes=observation.duration_minutes,
                status=observation.status,
                available_action=observation.available_action,
            )
            for observation in observations
        }
        targets = choose_booking_targets(
            self._config,
            [
                ObservedCourse(
                    name=observation.name,
                    day=observation.day,
                    start=observation.start,
                    duration_minutes=observation.duration_minutes,
                )
                for observation in observations
                if observation.status in {"free", "waitlist_possible"}
            ],
            existing_states=[
                ExistingCourseState(
                    name=state.name,
                    day=state.day,
                    start=state.start,
                    state=state.state,
                    duration_minutes=state.duration_minutes,
                )
                for state in existing_states
            ],
        )
        observations_by_key = {_observation_key(observation): observation for observation in observations}
        unclear_decisions = [
            self._unclear_decision(
                observation,
                observation_ids.get(_observation_key(observation)),
                run_id=run_id,
            )
            for observation in observations
            if observation.status == "unclear"
        ]
        target_decisions = [
            _decision_for_target(
                target.course,
                observations_by_key[_observed_course_key(target.course)],
                observation_ids.get(_observed_course_key(target.course)),
            )
            for target in targets
        ]
        decisions = unclear_decisions + target_decisions
        for decision in decisions:
            self._history.record_action(
                run_id=run_id,
                observation_id=decision.observation_id,
                action_type=decision.action_type,
                result=decision.result,
                reason=decision.reason,
            )
        return DryRunCollectionResult(
            observations=observations,
            existing_states=existing_states,
            decisions=decisions,
        )

    def _unclear_decision(
        self,
        observation: BrowserCourseObservation,
        observation_id: int | None,
        *,
        run_id: str,
    ) -> DryRunDecision:
        _save_artifact(
            self._artifacts,
            source=self._source,
            run_id=run_id,
            trigger="unclear_status",
            observation=observation,
            metadata={"result": "unclear", "reason": "course status was unclear during collection"},
        )
        return _unclear_decision(observation, observation_id)


class VerifiedActionRunner:
    def __init__(
        self,
        *,
        config: BookerConfig,
        history: HistoryStore,
        source: CourseActionSource,
        notifications: NotificationService | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._config = config
        self._history = history
        self._source = source
        self._notifications = notifications
        self._artifacts = artifacts

    def run(self, *, run_id: str, scan_dates: list[date], dry_run: bool) -> VerifiedActionResult:
        observations, existing_states = _collect_with_technical_artifact(
            self._source,
            self._artifacts,
            run_id=run_id,
            scan_dates=scan_dates,
        )
        observation_ids = _record_observations(self._history, run_id, observations)
        decisions = self._decide_and_act(
            run_id=run_id,
            observations=observations,
            existing_states=existing_states,
            observation_ids=observation_ids,
            live_allowed=self._config.enabled and not self._config.dry_run and not dry_run,
        )
        return VerifiedActionResult(
            observations=observations,
            existing_states=existing_states,
            decisions=decisions,
        )

    def _decide_and_act(
        self,
        *,
        run_id: str,
        observations: list[BrowserCourseObservation],
        existing_states: list[BrowserExistingState],
        observation_ids: dict[tuple[str, date, time], int],
        live_allowed: bool,
    ) -> list[DryRunDecision]:
        decisions: list[DryRunDecision] = []
        observations_by_key = {_observation_key(observation): observation for observation in observations}

        existing_no_ops = _existing_final_no_ops(
            self._config,
            observations,
            existing_states,
            observation_ids,
        )
        decisions.extend(existing_no_ops)

        targets = choose_booking_targets(
            self._config,
            [
                ObservedCourse(
                    name=observation.name,
                    day=observation.day,
                    start=observation.start,
                    duration_minutes=observation.duration_minutes,
                )
                for observation in observations
            ],
            existing_states=[
                ExistingCourseState(
                    name=state.name,
                    day=state.day,
                    start=state.start,
                    state=state.state,
                    duration_minutes=state.duration_minutes,
                )
                for state in existing_states
            ],
        )

        for target in targets:
            observation = observations_by_key[_observed_course_key(target.course)]
            observation_id = observation_ids.get(_observation_key(observation))
            if observation.status == "booked":
                decision = _decision(
                    observation,
                    observation_id,
                    action_type="no_op",
                    result="already_booked",
                    reason="visible booking is final",
                )
                _record_decision(self._history, run_id, decision)
                decisions.append(decision)
                continue
            if observation.status == "waitlisted":
                decision = _decision(
                    observation,
                    observation_id,
                    action_type="no_op",
                    result="already_waitlisted",
                    reason="visible waitlist membership is final",
                )
                _record_decision(self._history, run_id, decision)
                decisions.append(decision)
                continue
            if observation.status == "unclear":
                decisions.append(
                    _record_and_notify_failure(
                        self._history,
                        self._notifications,
                        run_id=run_id,
                        observation=observation,
                        observation_id=observation_id,
                        result="unclear",
                        reason="course status was unclear during collection",
                        artifacts=self._artifacts,
                        source=self._source,
                    )
                )
                continue
            if observation.status == "full" and observation.available_action != "waitlist":
                decisions.append(
                    _record_and_notify_failure(
                        self._history,
                        self._notifications,
                        run_id=run_id,
                        observation=observation,
                        observation_id=observation_id,
                        result="full_no_waitlist",
                        reason="target course is full and waitlist is not available",
                        artifacts=self._artifacts,
                        source=self._source,
                    )
                )
                continue
            if not live_allowed:
                decision = _dry_safety_decision(observation, observation_id)
                self._history.record_action(
                    run_id=run_id,
                    observation_id=decision.observation_id,
                    action_type=decision.action_type,
                    result=decision.result,
                    reason=decision.reason,
                )
                decisions.append(decision)
                continue
            decisions.append(self._execute_live_action(run_id, observation, observation_id))

        for decision in existing_no_ops:
            _record_decision(self._history, run_id, decision)
        return decisions

    def _execute_live_action(
        self,
        run_id: str,
        observation: BrowserCourseObservation,
        observation_id: int | None,
    ) -> DryRunDecision:
        if observation.available_action == "waitlist":
            verification = self._source.join_waitlist(observation)
            if verification.status == "waitlisted":
                return _record_success(
                    self._history,
                    self._notifications,
                    run_id=run_id,
                    observation=observation,
                    observation_id=observation_id,
                    action_type="waitlist",
                    result="success",
                    reason="waitlist verified after click",
                )
            return _record_and_notify_failure(
                self._history,
                self._notifications,
                run_id=run_id,
                observation=observation,
                observation_id=observation_id,
                result="verification_unclear",
                reason=verification.reason or f"waitlist verification returned {verification.status}",
                artifacts=self._artifacts,
                source=self._source,
            )

        verification = self._source.book(observation)
        if verification.status == "booked":
            return _record_success(
                self._history,
                self._notifications,
                run_id=run_id,
                observation=observation,
                observation_id=observation_id,
                action_type="booking",
                result="success",
                reason="booking verified after click",
            )
        return _record_and_notify_failure(
            self._history,
            self._notifications,
            run_id=run_id,
            observation=observation,
            observation_id=observation_id,
            result="verification_unclear",
            reason=verification.reason or f"booking verification returned {verification.status}",
            artifacts=self._artifacts,
            source=self._source,
        )


def _decision_for_target(
    course: ObservedCourse,
    observation: BrowserCourseObservation,
    observation_id: int | None,
) -> DryRunDecision:
    if observation.available_action == "waitlist":
        return DryRunDecision(
            course_name=course.name,
            course_date=course.day,
            start_time=course.start,
            action_type="waitlist",
            result="would_join_waitlist",
            reason="full target course allows waitlist",
            observation_id=observation_id,
        )
    return DryRunDecision(
        course_name=course.name,
        course_date=course.day,
        start_time=course.start,
        action_type="booking",
        result="would_book",
        reason="free spot matched target",
        observation_id=observation_id,
    )


def _record_decision(history: HistoryStore, run_id: str, decision: DryRunDecision) -> int:
    return history.record_action(
        run_id=run_id,
        observation_id=decision.observation_id,
        action_type=decision.action_type,
        result=decision.result,
        reason=decision.reason,
    )


def _record_observations(
    history: HistoryStore,
    run_id: str,
    observations: list[BrowserCourseObservation],
) -> dict[tuple[str, date, time], int]:
    return {
        _observation_key(observation): history.record_course_observation(
            run_id=run_id,
            scan_date=observation.day,
            course_name=observation.name,
            course_date=observation.day,
            start_time=_time_text(observation.start),
            duration_minutes=observation.duration_minutes,
            status=observation.status,
            available_action=observation.available_action,
        )
        for observation in observations
    }


def _existing_final_no_ops(
    config: BookerConfig,
    observations: list[BrowserCourseObservation],
    existing_states: list[BrowserExistingState],
    observation_ids: dict[tuple[str, date, time], int],
) -> list[DryRunDecision]:
    final_days = {(_normalize(state.name), state.day): state.state for state in existing_states}
    decisions: list[DryRunDecision] = []
    seen: set[tuple[str, date]] = set()
    for observation in observations:
        normalized_day = (_normalize(observation.name), observation.day)
        if normalized_day in seen or normalized_day not in final_days:
            continue
        if not _is_configured_course(config, observation.name):
            continue
        seen.add(normalized_day)
        state = final_days[normalized_day]
        result = "already_booked" if state == "booked" else "already_waitlisted"
        reason = "existing booking is final" if state == "booked" else "existing waitlist membership is final"
        decisions.append(
            _decision(
                observation,
                observation_ids.get(_observation_key(observation)),
                action_type="no_op",
                result=result,
                reason=reason,
            )
        )
    return decisions


def _record_success(
    history: HistoryStore,
    notifications: NotificationService | None,
    *,
    run_id: str,
    observation: BrowserCourseObservation,
    observation_id: int | None,
    action_type: Literal["booking", "waitlist"],
    result: str,
    reason: str,
) -> DryRunDecision:
    action_id = history.record_action(
        run_id=run_id,
        observation_id=observation_id,
        action_type=action_type,
        result=result,
        reason=reason,
    )
    if notifications is not None:
        notifications.send_course_action_success(action_id=action_id)
    return _decision(
        observation,
        observation_id,
        action_type=action_type,
        result=result,
        reason=reason,
    )


def _record_and_notify_failure(
    history: HistoryStore,
    notifications: NotificationService | None,
    *,
    run_id: str,
    observation: BrowserCourseObservation,
    observation_id: int | None,
    result: str,
    reason: str,
    artifacts: ArtifactStore | None = None,
    source: object | None = None,
) -> DryRunDecision:
    artifact = _save_artifact(
        artifacts,
        source=source,
        run_id=run_id,
        trigger="failed_action" if result != "unclear" else "unclear_status",
        observation=observation,
        metadata={"result": result, "reason": reason},
    )
    history.record_action(
        run_id=run_id,
        observation_id=observation_id,
        action_type="failure",
        result=result,
        reason=reason,
    )
    if notifications is not None:
        notifications.send_known_failure(
            failure_key=f"{result}:{_normalize(observation.name)}:{observation.day.isoformat()}",
            description=reason,
            run_id=run_id,
            artifact_path=artifact.directory if artifact is not None else None,
        )
    return _decision(
        observation,
        observation_id,
        action_type="failure",
        result=result,
        reason=reason,
    )


def _save_artifact(
    artifacts: ArtifactStore | None,
    *,
    source: object | None,
    run_id: str,
    trigger: Literal["unclear_status", "failed_action", "technical_failure", "manual_intervention"],
    observation: BrowserCourseObservation | None,
    metadata: dict[str, object],
) -> ArtifactPaths | None:
    if artifacts is None:
        return None
    html, screenshot, source_metadata = _diagnostic_artifacts(source, observation=observation)
    payload: dict[str, object] = {
        **metadata,
        **source_metadata,
    }
    if observation is not None:
        payload.update(
            {
                "course_name": observation.name,
                "course_date": observation.day.isoformat(),
                "start_time": _time_text(observation.start),
                "status": observation.status,
                "available_action": observation.available_action,
            }
        )
    return artifacts.save(
        run_id=run_id,
        trigger=trigger,
        occurred_at=_artifact_occurred_at(observation),
        html=html,
        screenshot=screenshot,
        metadata=payload,
    )


def _collect_with_technical_artifact(
    source: CourseCollectionSource,
    artifacts: ArtifactStore | None,
    *,
    run_id: str,
    scan_dates: list[date],
) -> tuple[list[BrowserCourseObservation], list[BrowserExistingState]]:
    try:
        return source.collect(scan_dates)
    except Exception as error:
        _save_artifact(
            artifacts,
            source=source,
            run_id=run_id,
            trigger="technical_failure",
            observation=None,
            metadata={
                "error_type": type(error).__name__,
                "error": redact(str(error)),
                "scan_dates": [scan_date.isoformat() for scan_date in scan_dates],
            },
        )
        raise


def _diagnostic_artifacts(
    source: object | None,
    *,
    observation: BrowserCourseObservation | None,
) -> tuple[str | None, bytes | None, dict[str, object]]:
    if source is None or not hasattr(source, "diagnostic_artifacts"):
        return None, None, {}
    result = getattr(source, "diagnostic_artifacts")(observation=observation)
    html, screenshot, metadata = result
    return html, screenshot, dict(metadata)


def _artifact_occurred_at(observation: BrowserCourseObservation | None) -> datetime | None:
    if observation is None:
        return None
    return datetime.combine(observation.day, observation.start, tzinfo=timezone.utc)


def _dry_safety_decision(
    observation: BrowserCourseObservation,
    observation_id: int | None,
) -> DryRunDecision:
    intended = _decision_for_target(
        ObservedCourse(
            name=observation.name,
            day=observation.day,
            start=observation.start,
            duration_minutes=observation.duration_minutes,
        ),
        observation,
        observation_id,
    )
    return _decision(
        observation,
        observation_id,
        action_type=intended.action_type,
        result=intended.result,
        reason="live action disabled by safety gates",
    )


def _decision(
    observation: BrowserCourseObservation,
    observation_id: int | None,
    *,
    action_type: ActionType,
    result: str,
    reason: str,
) -> DryRunDecision:
    return DryRunDecision(
        course_name=observation.name,
        course_date=observation.day,
        start_time=observation.start,
        action_type=action_type,
        result=result,
        reason=reason,
        observation_id=observation_id,
    )


def _unclear_decision(
    observation: BrowserCourseObservation,
    observation_id: int | None,
) -> DryRunDecision:
    return DryRunDecision(
        course_name=observation.name,
        course_date=observation.day,
        start_time=observation.start,
        action_type="failure",
        result="unclear",
        reason="course status was unclear during collection",
        observation_id=observation_id,
    )


def _observation_key(observation: BrowserCourseObservation) -> tuple[str, date, time]:
    return (observation.name, observation.day, observation.start)


def _observed_course_key(course: ObservedCourse) -> tuple[str, date, time]:
    return (course.name, course.day, course.start)


def _time_text(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _is_configured_course(config: BookerConfig, name: str) -> bool:
    return _normalize(name) in {_normalize(course.name) for course in config.courses}


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


class _VisibleTextBlockParser(HTMLParser):
    _BLOCK_TAGS = {"article", "li", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._capture_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            if self._capture_depth == 0:
                self._parts = []
            self._capture_depth += 1
        if self._capture_depth > 0 and tag in {"br", "p", "div"}:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS and self._capture_depth > 0:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                block = _normalize_visible_text(" ".join(self._parts))
                if block:
                    self.blocks.append(block)

    def handle_data(self, data: str) -> None:
        if self._capture_depth > 0:
            self._parts.append(data)


def _extract_visible_text_blocks(html: str) -> list[str]:
    parser = _VisibleTextBlockParser()
    parser.feed(html)
    if parser.blocks:
        return parser.blocks
    return [_normalize_visible_text(html)]


def _course_observations_from_blocks(blocks: list[str], scan_date: date) -> list[BrowserCourseObservation]:
    observations: list[BrowserCourseObservation] = []
    for block in blocks:
        if _is_existing_state_block(block):
            continue
        parsed = _parse_course_block(block, scan_date)
        if parsed is not None:
            observations.append(parsed)
    return observations


def _existing_states_from_blocks(blocks: list[str], scan_date: date) -> list[BrowserExistingState]:
    states: list[BrowserExistingState] = []
    for block in blocks:
        existing_state = _existing_state_from_block(block)
        if existing_state is None:
            continue
        parsed = _parse_course_identity(block, scan_date)
        if parsed is None:
            continue
        name, start = parsed
        states.append(
            BrowserExistingState(
                name=name,
                day=scan_date,
                start=start,
                state=existing_state,
                duration_minutes=None,
            )
        )
    return states


def _parse_course_block(block: str, scan_date: date) -> BrowserCourseObservation | None:
    parsed = _parse_course_identity(block, scan_date)
    if parsed is None:
        return None
    name, start = parsed
    duration_minutes = _duration_minutes(block)
    status, available_action = _status_and_action_from_german_text(block)
    return BrowserCourseObservation(
        name=name,
        day=scan_date,
        start=start,
        duration_minutes=duration_minutes,
        status=status,
        available_action=available_action,
    )


def _parse_course_identity(block: str, scan_date: date) -> tuple[str, time] | None:
    date_pattern = re.escape(scan_date.strftime("%d.%m.%Y"))
    match = re.search(rf"(?P<name>.*?)\s+{date_pattern}\s+(?P<hour>[0-2]\d):(?P<minute>[0-5]\d)", block)
    if match is None:
        return None
    name = _strip_existing_prefix(match.group("name"))
    if not name:
        return None
    return name, time(int(match.group("hour")), int(match.group("minute")))


def _strip_existing_prefix(value: str) -> str:
    return re.sub(r"^(Meine Buchungen|Meine Warteliste)\s+", "", value, flags=re.I).strip()


def _duration_minutes(block: str) -> int | None:
    match = re.search(r"\b(?P<minutes>\d{1,3})\s*Min\.?\b", block, flags=re.I)
    if match is None:
        return None
    return int(match.group("minutes"))


def _status_and_action_from_german_text(block: str) -> tuple[CourseStatus, AvailableAction | None]:
    normalized = _normalize_german(block)
    if "gebucht" in normalized:
        return "booked", None
    if "warteliste moeglich" in normalized or "warteliste moglich" in normalized:
        return "waitlist_possible", "waitlist"
    if "warteliste" in normalized:
        return "waitlisted", None
    if "freie plaetze" in normalized or "freier platz" in normalized:
        return "free", "book"
    if "ausgebucht" in normalized or "keine plaetze" in normalized:
        return "full", None
    return "unclear", None


def _existing_state_from_block(block: str) -> ExistingState | None:
    normalized = _normalize_german(block)
    if not normalized.startswith("meine "):
        return None
    if "meine buchungen" in normalized or "gebucht" in normalized:
        return "booked"
    if "meine warteliste" in normalized or "warteliste" in normalized:
        return "waitlisted"
    return None


def _is_existing_state_block(block: str) -> bool:
    return _existing_state_from_block(block) is not None


def _looks_like_booking_page(html: str) -> bool:
    normalized = _normalize_german(html)
    return any(
        marker in normalized
        for marker in ("kurs buchen", "meine buchungen", "freie plaetze", "warteliste")
    ) or re.search(r"\b\d{2}\.\d{2}\.\d{4}\s+[0-2]\d:[0-5]\d\b", normalized) is not None


def _click_first_available(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def _normalize_visible_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _normalize_german(value: str) -> str:
    normalized = _normalize_visible_text(value).casefold()
    return (
        normalized.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _default_playwright_factory() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()
