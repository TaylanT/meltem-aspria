from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys
import time as time_module
from typing import Callable, Protocol

from aspria_booker.artifacts import ArtifactStore
from aspria_booker.browser_adapter import (
    CourseActionSource,
    PlaywrightCourseCollectionSource,
    VerifiedActionResult,
    VerifiedActionRunner,
)
from aspria_booker.config import BookerConfig, redact
from aspria_booker.email import NotificationService
from aspria_booker.history import HistoryStore
from aspria_booker.schedule import PollWindow, hourly_scan_dates, release_schedule
from aspria_booker.session import BrowserSessionManager, LoginOutcome, PlaywrightBrowserProbe


@dataclass(frozen=True)
class JobPaths:
    history_path: Path = Path("storage/history.sqlite")
    artifact_root: Path = Path("storage/artifacts")


@dataclass(frozen=True)
class JobRunResult:
    run_id: str
    command: str
    iterations: int
    actions: int
    failures: int


class Authenticator(Protocol):
    def ensure_authenticated(self, *, run_id: str | None = None) -> LoginOutcome:
        raise NotImplementedError


class JobClock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock:
    def now(self) -> datetime:
        return datetime.now()

    def sleep(self, seconds: float) -> None:
        time_module.sleep(seconds)


def run_release_job(
    *,
    config: BookerConfig,
    history: HistoryStore | None = None,
    source: CourseActionSource | None = None,
    session: Authenticator | None = None,
    notifications: NotificationService | None = None,
    paths: JobPaths = JobPaths(),
    today: date | None = None,
    clock: JobClock | None = None,
    dry_run: bool | None = None,
) -> JobRunResult:
    today = today or date.today()
    clock = clock or SystemClock()
    history = history or HistoryStore.open(paths.history_path)
    artifacts = ArtifactStore(paths.artifact_root)
    run_id = history.start_run(command="release")
    _log(f"release: opened run {run_id}")
    if not _authenticate(session or _session_manager(config, artifacts), run_id=run_id, history=history):
        return JobRunResult(run_id=run_id, command="release", iterations=0, actions=1, failures=1)

    schedule = release_schedule(config, today=today)
    scan_dates = [schedule.target_date]
    _log(f"release: target date {schedule.target_date.isoformat()}")
    _log(
        "release: "
        f"tight {schedule.tight.start} through {schedule.tight.end} "
        f"every {schedule.tight.interval_seconds}s; "
        f"slow {schedule.slow.start} through {schedule.slow.end} "
        f"every {schedule.slow.interval_seconds}s"
    )
    runner_source = source or _default_action_source(config)
    runner = VerifiedActionRunner(
        config=config,
        history=history,
        source=runner_source,
        notifications=notifications,
        artifacts=artifacts,
    )
    iterations = 0
    actions = 0
    failures = 0
    for window in (schedule.tight, schedule.slow):
        window_result = _poll_window(
            window,
            today=today,
            clock=clock,
            history=history,
            run_id=run_id,
            run_once=lambda: runner.run(
                run_id=run_id,
                scan_dates=scan_dates,
                dry_run=config.dry_run if dry_run is None else dry_run,
            ),
        )
        iterations += window_result.iterations
        actions += window_result.actions
        failures += window_result.failures
        if window_result.actions > 0 and window_result.failures == 0:
            break
    _close_source(runner_source)
    _log(f"release: completed run {run_id}; iterations={iterations} actions={actions} failures={failures}")
    return JobRunResult(run_id=run_id, command="release", iterations=iterations, actions=actions, failures=failures)


def run_hourly_job(
    *,
    config: BookerConfig,
    history: HistoryStore | None = None,
    source: CourseActionSource | None = None,
    session: Authenticator | None = None,
    notifications: NotificationService | None = None,
    paths: JobPaths = JobPaths(),
    today: date | None = None,
    dry_run: bool | None = None,
) -> JobRunResult:
    today = today or date.today()
    history = history or HistoryStore.open(paths.history_path)
    artifacts = ArtifactStore(paths.artifact_root)
    run_id = history.start_run(command="hourly")
    _log(f"hourly: opened run {run_id}")
    if not _authenticate(session or _session_manager(config, artifacts), run_id=run_id, history=history):
        return JobRunResult(run_id=run_id, command="hourly", iterations=0, actions=1, failures=1)

    scan_dates = hourly_scan_dates(config, today=today)
    _log(f"hourly: scan {scan_dates[0].isoformat()} through {scan_dates[-1].isoformat()}")
    runner_source = source or _default_action_source(config)
    try:
        result = VerifiedActionRunner(
            config=config,
            history=history,
            source=runner_source,
            notifications=notifications,
            artifacts=artifacts,
        ).run(
            run_id=run_id,
            scan_dates=scan_dates,
            dry_run=config.dry_run if dry_run is None else dry_run,
        )
    except Exception as error:
        _record_technical_failure(history, run_id=run_id, error=error)
        _log_error(f"hourly: failed run {run_id}: {redact(str(error))}")
        return JobRunResult(run_id=run_id, command="hourly", iterations=1, actions=1, failures=1)
    finally:
        _close_source(runner_source)
    failures = sum(1 for decision in result.decisions if decision.action_type == "failure")
    _log(f"hourly: completed run {run_id}; decisions={len(result.decisions)} failures={failures}")
    return JobRunResult(
        run_id=run_id,
        command="hourly",
        iterations=1,
        actions=len(result.decisions),
        failures=failures,
    )


@dataclass(frozen=True)
class _WindowRunResult:
    iterations: int
    actions: int
    failures: int


def _poll_window(
    window: PollWindow,
    *,
    today: date,
    clock: JobClock,
    history: HistoryStore,
    run_id: str,
    run_once: Callable[[], VerifiedActionResult],
) -> _WindowRunResult:
    start = _combine(today, window.start)
    end = _combine(today, window.end)
    now = clock.now()
    if now < start:
        clock.sleep((start - now).total_seconds())
    iterations = 0
    actions = 0
    failures = 0
    while clock.now() <= end:
        iterations += 1
        try:
            result = run_once()
        except Exception as error:
            _record_technical_failure(history, run_id=run_id, error=error)
            _log_error(f"release: poll failed: {redact(str(error))}")
            failures += 1
            actions += 1
        else:
            decisions = result.decisions
            actions += len(decisions)
            iteration_failures = sum(1 for decision in decisions if decision.action_type == "failure")
            failures += iteration_failures
            if decisions and iteration_failures == 0:
                break
        next_instant = clock.now() + timedelta(seconds=window.interval_seconds)
        if next_instant > end:
            break
        clock.sleep(window.interval_seconds)
    return _WindowRunResult(iterations=iterations, actions=actions, failures=failures)


def _authenticate(session: Authenticator, *, run_id: str, history: HistoryStore) -> bool:
    outcome = session.ensure_authenticated(run_id=run_id)
    _log(f"session: {outcome.status.value}")
    if outcome.ok:
        return True
    history.record_action(
        run_id=run_id,
        observation_id=None,
        action_type="failure",
        result="login_required",
        reason=redact(outcome.message),
    )
    _log_error(f"session: {redact(outcome.message)}")
    return False


def _record_technical_failure(history: HistoryStore, *, run_id: str, error: Exception) -> None:
    history.record_action(
        run_id=run_id,
        observation_id=None,
        action_type="failure",
        result="technical_error",
        reason=redact(f"{type(error).__name__}: {error}"),
    )


def _default_action_source(config: BookerConfig) -> CourseActionSource:
    return PlaywrightCourseCollectionSource(config=config)  # type: ignore[return-value]


def _session_manager(config: BookerConfig, artifacts: ArtifactStore) -> BrowserSessionManager:
    return BrowserSessionManager(
        club=config.club,
        secrets=config.secrets,
        probe=PlaywrightBrowserProbe(),
        artifacts=artifacts,
    )


def _close_source(source: object) -> None:
    close = getattr(source, "close", None)
    if callable(close):
        close()


def _combine(day: date, value: str) -> datetime:
    hour, minute = value.split(":", 1)
    return datetime.combine(day, time(int(hour), int(minute)))


def _log(message: str) -> None:
    print(redact(message))


def _log_error(message: str) -> None:
    print(redact(message), file=sys.stderr)
