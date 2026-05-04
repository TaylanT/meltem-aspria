from __future__ import annotations

from pathlib import Path


def test_readme_documents_private_server_runbook() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    required_phrases = [
        "Python 3.12",
        "uv sync",
        "uv run playwright install chromium",
        "config.yaml",
        ".env",
        "storage/aspria-state.json",
        "chmod 600",
        "aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env setup-login --headed",
        "aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env scan --dry-run",
        "aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env release --dry-run",
        "aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env hourly --dry-run",
        "aspria-booker --config /opt/aspria-booker/config.yaml --env-file /opt/aspria-booker/.env test-email",
        "aspria-booker-release.service",
        "aspria-booker-release.timer",
        "OnCalendar=*-*-* 20:55:00",
        "aspria-booker-hourly.service",
        "aspria-booker-hourly.timer",
        "OnCalendar=hourly",
        "journalctl -u aspria-booker-release.service",
        "journalctl -u aspria-booker-hourly.service",
        "storage/history.sqlite",
        "storage/artifacts",
        "expired session",
        "manual login intervention",
        "unclear page state",
        "notification behavior",
        "cancellation",
        "rebooking",
        "Captcha bypass",
        "2FA bypass",
        "English UI fallback",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in readme]
    assert missing == []
