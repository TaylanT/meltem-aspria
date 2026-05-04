from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Literal
from uuid import uuid4


ActionType = Literal["booking", "waitlist", "no_op", "failure"]


class HistoryStore:
    """SQLite-backed audit trail for runs, observations, actions, and notifications."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    @classmethod
    def open(cls, path: str | Path) -> HistoryStore:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(db_path))

    def start_run(
        self,
        *,
        command: str,
        started_at: datetime | None = None,
        run_id: str | None = None,
    ) -> str:
        stable_run_id = run_id or str(uuid4())
        self._connection.execute(
            """
            INSERT OR IGNORE INTO runs (run_id, command, started_at)
            VALUES (?, ?, ?)
            """,
            (stable_run_id, command, _datetime_text(started_at)),
        )
        self._connection.commit()
        return stable_run_id

    def record_course_observation(
        self,
        *,
        run_id: str,
        scan_date: date,
        course_name: str,
        course_date: date,
        start_time: str,
        duration_minutes: int | None,
        status: str,
        available_action: str | None,
        observed_at: datetime | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO course_observations (
                run_id,
                scan_date,
                course_name,
                course_date,
                start_time,
                duration_minutes,
                status,
                available_action,
                observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                scan_date.isoformat(),
                course_name,
                course_date.isoformat(),
                start_time,
                duration_minutes,
                status,
                available_action,
                _datetime_text(observed_at),
            ),
        )
        self._connection.commit()
        return _required_lastrowid(cursor)

    def record_action(
        self,
        *,
        run_id: str,
        observation_id: int | None,
        action_type: ActionType,
        result: str,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO actions (
                run_id,
                observation_id,
                action_type,
                result,
                reason,
                occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, observation_id, action_type, result, reason, _datetime_text(occurred_at)),
        )
        self._connection.commit()
        return _required_lastrowid(cursor)

    def actions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT
                actions.action_id,
                actions.action_type,
                actions.result,
                course_observations.course_name,
                course_observations.scan_date
            FROM actions
            LEFT JOIN course_observations
                ON course_observations.observation_id = actions.observation_id
            WHERE actions.run_id = ?
            ORDER BY actions.action_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def action_for_notification(self, action_id: int) -> dict[str, Any]:
        row = self._connection.execute(
            """
            SELECT
                actions.action_id,
                actions.run_id,
                actions.action_type,
                actions.result,
                course_observations.course_name,
                course_observations.course_date,
                course_observations.start_time
            FROM actions
            LEFT JOIN course_observations
                ON course_observations.observation_id = actions.observation_id
            WHERE actions.action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown action_id: {action_id}")
        return dict(row)

    def has_action_notification(self, action_id: int, *, notification_type: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM notifications
            WHERE action_id = ? AND notification_type = ?
            LIMIT 1
            """,
            (action_id, notification_type),
        ).fetchone()
        return row is not None

    def record_action_notification(
        self,
        *,
        action_id: int,
        notification_type: str,
        sent_at: datetime | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO notifications (
                action_id,
                notification_type,
                notification_date,
                sent_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (action_id, notification_type, _notification_date(sent_at), _datetime_text(sent_at)),
        )
        self._connection.commit()
        return _required_lastrowid(cursor)

    def has_known_failure_notification(self, failure_key: str, notification_date: date) -> bool:
        return self.has_keyed_notification(
            failure_key,
            notification_type="known_failure",
            notification_date=notification_date,
        )

    def has_keyed_notification(
        self,
        failure_key: str,
        *,
        notification_type: str,
        notification_date: date,
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM notifications
            WHERE failure_key = ?
                AND notification_type = ?
                AND notification_date = ?
            LIMIT 1
            """,
            (failure_key, notification_type, notification_date.isoformat()),
        ).fetchone()
        return row is not None

    def record_known_failure_notification(
        self,
        *,
        failure_key: str,
        notification_date: date,
        sent_at: datetime | None = None,
    ) -> int:
        return self.record_keyed_notification(
            failure_key=failure_key,
            notification_type="known_failure",
            notification_date=notification_date,
            sent_at=sent_at,
        )

    def record_keyed_notification(
        self,
        *,
        failure_key: str,
        notification_type: str,
        notification_date: date,
        sent_at: datetime | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO notifications (
                failure_key,
                notification_type,
                notification_date,
                sent_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (failure_key, notification_type, notification_date.isoformat(), _datetime_text(sent_at)),
        )
        self._connection.commit()
        return _required_lastrowid(cursor)

    def cleanup_history(self, *, before: date) -> int:
        cursor = self._connection.execute(
            "DELETE FROM runs WHERE date(started_at) < date(?)",
            (before.isoformat(),),
        )
        self._connection.execute(
            """
            DELETE FROM notifications
            WHERE action_id IS NULL AND date(notification_date) < date(?)
            """,
            (before.isoformat(),),
        )
        self._connection.commit()
        return cursor.rowcount

    def cleanup_history_retention(self, *, retention_days: int, today: date) -> int:
        return self.cleanup_history(before=today - timedelta(days=retention_days))

    def run_ids(self) -> list[str]:
        rows = self._connection.execute("SELECT run_id FROM runs ORDER BY started_at, run_id").fetchall()
        return [str(row["run_id"]) for row in rows]

    def close(self) -> None:
        self._connection.close()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                started_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS course_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                scan_date TEXT NOT NULL,
                course_name TEXT NOT NULL,
                course_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                duration_minutes INTEGER,
                status TEXT NOT NULL,
                available_action TEXT,
                observed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                observation_id INTEGER REFERENCES course_observations(observation_id) ON DELETE SET NULL,
                action_type TEXT NOT NULL CHECK (action_type IN ('booking', 'waitlist', 'no_op', 'failure')),
                result TEXT NOT NULL,
                reason TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER REFERENCES actions(action_id) ON DELETE CASCADE,
                failure_key TEXT,
                notification_type TEXT NOT NULL,
                notification_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                CHECK (action_id IS NOT NULL OR failure_key IS NOT NULL)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS notifications_action_dedupe
            ON notifications(action_id, notification_type)
            WHERE action_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS notifications_known_failure_daily_dedupe
            ON notifications(failure_key, notification_type, notification_date)
            WHERE failure_key IS NOT NULL;
            """
        )
        self._connection.commit()


def _datetime_text(value: datetime | None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat()


def _notification_date(value: datetime | None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).date().isoformat()


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not report a row id for the insert")
    return cursor.lastrowid
