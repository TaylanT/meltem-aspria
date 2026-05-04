from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from aspria_booker.artifacts import ArtifactStore, ArtifactTrigger, should_capture_artifacts


def test_error_artifact_paths_include_run_id_kind_and_safe_timestamp(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    artifact = store.save(
        run_id="run-123",
        trigger="unclear_status",
        occurred_at=datetime(2026, 5, 3, 19, 4, 5, tzinfo=timezone.utc),
        html="<html><body>Status unclear</body></html>",
        screenshot=b"png bytes",
        metadata={"course": "LES MILLS BODYPUMP"},
    )

    assert artifact.directory == tmp_path / "artifacts" / "run-123" / "20260503T190405Z-unclear-status"
    assert artifact.html_path == artifact.directory / "page.html"
    assert artifact.screenshot_path == artifact.directory / "screenshot.png"
    assert artifact.metadata_path == artifact.directory / "metadata.json"
    assert artifact.html_path is not None
    assert artifact.screenshot_path is not None
    assert "Status unclear" in artifact.html_path.read_text(encoding="utf-8")
    assert artifact.screenshot_path.read_bytes() == b"png bytes"


def test_artifact_metadata_redacts_secret_values_recursively(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    artifact = store.save(
        run_id="run-secret",
        trigger="technical_failure",
        metadata={
            "url": "https://example.invalid/book",
            "ASPRIA_PASSWORD": "server-password",
            "headers": {"Authorization": "Bearer token", "Accept": "text/html"},
            "cookies": [{"name": "session", "value": "cookie-value"}],
        },
    )

    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert metadata["url"] == "https://example.invalid/book"
    assert metadata["ASPRIA_PASSWORD"] == "[redacted]"
    assert metadata["headers"]["Authorization"] == "[redacted]"
    assert metadata["headers"]["Accept"] == "text/html"
    assert metadata["cookies"] == "[redacted]"
    assert "server-password" not in artifact.metadata_path.read_text(encoding="utf-8")
    assert "cookie-value" not in artifact.metadata_path.read_text(encoding="utf-8")


def test_capture_policy_only_allows_review_worthy_states() -> None:
    allowed: tuple[ArtifactTrigger, ...] = (
        "unclear_status",
        "failed_action",
        "technical_failure",
        "manual_intervention",
    )

    for trigger in allowed:
        assert should_capture_artifacts(trigger) is True

    assert should_capture_artifacts("success") is False
    assert should_capture_artifacts("dry_run_no_op") is False


def test_retention_cleanup_only_removes_old_marked_artifact_directories(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    old = store.save(
        run_id="old-run",
        trigger="technical_failure",
        occurred_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        html="old",
    )
    kept = store.save(
        run_id="kept-run",
        trigger="technical_failure",
        occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        html="kept",
    )
    unrelated = root / "manual-notes"
    unrelated.mkdir(parents=True)
    (unrelated / "page.html").write_text("do not delete", encoding="utf-8")

    removed = store.cleanup_retention(retention_days=14, today=date(2026, 5, 3))

    assert removed == 1
    assert not old.directory.exists()
    assert kept.directory.exists()
    assert (unrelated / "page.html").exists()
