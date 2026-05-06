from __future__ import annotations

from pathlib import Path

import pytest

from aspria_booker.config import ConfigError, effective_dry_run, load_config


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def valid_config(*, dry_run: bool = True, notifications: bool = False) -> str:
    return f"""
enabled: true
dry_run: {str(dry_run).lower()}
club:
  name: Aspria Hannover Maschsee
  booking_url: https://example.invalid/book
courses:
  - LES MILLS BODYPUMP
  - Hyrox Starter
time_windows:
  tuesday:
    - from: "00:00"
      to: "11:00"
  thursday:
    - from: "18:00"
      to: "23:59"
release_job:
  poll_start: "20:58"
  tight_poll_until: "21:10"
  tight_interval_seconds: 5
  slow_poll_until: "22:00"
  slow_interval_seconds: 60
hourly_job:
  lookahead_days: 3
matching:
  exact_normalized: true
  fuzzy: false
default_duration_minutes: 60
buffer_minutes: 15
retention:
  history_days: 90
  artifacts_days: 14
notifications:
  enabled: {str(notifications).lower()}
  smtp:
    host: smtp.example.invalid
    port: 587
    username_env: SMTP_USERNAME
    password_env: SMTP_PASSWORD
    from_email: bot@example.invalid
    to_email: ops@example.invalid
"""


def test_loads_yaml_and_env_without_requiring_live_credentials(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, valid_config())
    env_path = tmp_path / ".env"
    env_path.write_text("ASPRIA_EMAIL=person@example.invalid\nASPRIA_PASSWORD=secret\n", encoding="utf-8")

    config = load_config(config_path, env_path=env_path)

    assert config.club.name == "Aspria Hannover Maschsee"
    assert [course.name for course in config.courses] == ["LES MILLS BODYPUMP", "Hyrox Starter"]
    assert config.time_windows["tuesday"][0].end == "11:00"
    assert config.secrets["ASPRIA_PASSWORD"] == "secret"


def test_allows_longer_tight_polling_interval(tmp_path: Path) -> None:
    config_text = valid_config().replace("tight_interval_seconds: 5", "tight_interval_seconds: 60")

    config = load_config(write_config(tmp_path, config_text))

    assert config.release_job.tight_interval_seconds == 60


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("moonday:\n    - from: \"09:00\"\n      to: \"10:00\"", "unknown weekday"),
        ("tuesday:\n    - from: \"9am\"\n      to: \"10:00\"", "invalid time"),
    ],
)
def test_rejects_invalid_time_windows(tmp_path: Path, patch: str, message: str) -> None:
    config_text = valid_config().replace(
        'tuesday:\n    - from: "00:00"\n      to: "11:00"', patch
    )

    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        (valid_config().replace("- Hyrox Starter", "- ''"), "course name"),
        (valid_config().replace("default_duration_minutes: 60", "default_duration_minutes: -1"), "duration"),
        (valid_config().replace("buffer_minutes: 15", "buffer_minutes: -1"), "buffer"),
        (
            valid_config().replace("tight_interval_seconds: 5", "tight_interval_seconds: 4"),
            "tight polling interval",
        ),
        (
            valid_config().replace('tight_poll_until: "21:10"', 'tight_poll_until: "20:57"'),
            "release polling times",
        ),
        (
            valid_config(notifications=True).replace("host: smtp.example.invalid", "host: ''"),
            "SMTP",
        ),
    ],
)
def test_rejects_invalid_config_values(tmp_path: Path, config_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, config_text))


def test_live_config_requires_aspria_credentials(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ASPRIA_EMAIL.*ASPRIA_PASSWORD"):
        load_config(write_config(tmp_path, valid_config(dry_run=False)), env_path=None)


def test_cli_dry_run_override_only_moves_in_safe_direction(tmp_path: Path) -> None:
    dry_config = load_config(write_config(tmp_path, valid_config(dry_run=True)))
    live_env = tmp_path / ".env"
    live_env.write_text("ASPRIA_EMAIL=a@example.invalid\nASPRIA_PASSWORD=p\n", encoding="utf-8")
    live_config = load_config(write_config(tmp_path, valid_config(dry_run=False)), env_path=live_env)

    assert effective_dry_run(dry_config, cli_dry_run=False) is True
    assert effective_dry_run(live_config, cli_dry_run=True) is True
