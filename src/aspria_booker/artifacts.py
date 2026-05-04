from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Any, Literal


ArtifactTrigger = Literal[
    "unclear_status",
    "failed_action",
    "technical_failure",
    "manual_intervention",
]

_CAPTURE_TRIGGERS: set[str] = {
    "unclear_status",
    "failed_action",
    "technical_failure",
    "manual_intervention",
}
_SECRET_KEY_PARTS = ("password", "cookie", "authorization", "auth", "token", "secret")
_MARKER_FILE = ".aspria-artifact"


@dataclass(frozen=True)
class ArtifactPaths:
    directory: Path
    metadata_path: Path
    html_path: Path | None = None
    screenshot_path: Path | None = None


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def save(
        self,
        *,
        run_id: str,
        trigger: ArtifactTrigger,
        occurred_at: datetime | None = None,
        html: str | None = None,
        screenshot: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactPaths:
        instant = _utc(occurred_at)
        directory = self._root / _safe_path_part(run_id) / f"{_timestamp_slug(instant)}-{_trigger_slug(trigger)}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / _MARKER_FILE).write_text("aspria-booker artifact\n", encoding="utf-8")

        html_path = None
        if html is not None:
            html_path = directory / "page.html"
            html_path.write_text(html, encoding="utf-8")

        screenshot_path = None
        if screenshot is not None:
            screenshot_path = directory / "screenshot.png"
            screenshot_path.write_bytes(screenshot)

        metadata_path = directory / "metadata.json"
        metadata_payload = _redact_metadata(metadata or {})
        metadata_payload.update(
            {
                "run_id": run_id,
                "trigger": trigger,
                "occurred_at": instant.isoformat(),
            }
        )
        metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        return ArtifactPaths(
            directory=directory,
            metadata_path=metadata_path,
            html_path=html_path,
            screenshot_path=screenshot_path,
        )

    def cleanup_retention(self, *, retention_days: int, today: date) -> int:
        cutoff = today - timedelta(days=retention_days)
        removed = 0
        if not self._root.exists():
            return removed

        for marker in self._root.glob(f"*/*/{_MARKER_FILE}"):
            artifact_dir = marker.parent
            if _artifact_date(artifact_dir) is None:
                continue
            artifact_date = _artifact_date(artifact_dir)
            if artifact_date is not None and artifact_date < cutoff:
                shutil.rmtree(artifact_dir)
                removed += 1
        _remove_empty_run_directories(self._root)
        return removed


def should_capture_artifacts(trigger: str) -> bool:
    return trigger in _CAPTURE_TRIGGERS


def _redact_metadata(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_secret_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _redact_metadata(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_metadata(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _utc(value: datetime | None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _timestamp_slug(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _trigger_slug(trigger: str) -> str:
    return trigger.replace("_", "-")


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip(".-") or "unknown"


def _artifact_date(directory: Path) -> date | None:
    prefix = directory.name.split("-", 1)[0]
    try:
        return datetime.strptime(prefix, "%Y%m%dT%H%M%SZ").date()
    except ValueError:
        return None


def _remove_empty_run_directories(root: Path) -> None:
    for run_dir in root.iterdir():
        if run_dir.is_dir() and not any(run_dir.iterdir()):
            run_dir.rmdir()
