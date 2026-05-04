from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or unsafe."""


WEEKDAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}
SECRET_KEYS = ("PASSWORD", "COOKIE", "AUTH", "TOKEN", "SECRET")


@dataclass(frozen=True)
class ClubConfig:
    name: str
    booking_url: str | None = None
    storage_state_path: str = "storage/aspria-state.json"


@dataclass(frozen=True)
class CourseConfig:
    name: str


@dataclass(frozen=True)
class TimeWindow:
    start: str
    end: str


@dataclass(frozen=True)
class ReleaseJobConfig:
    poll_start: str
    tight_poll_until: str
    tight_interval_seconds: int
    slow_poll_until: str
    slow_interval_seconds: int


@dataclass(frozen=True)
class HourlyJobConfig:
    lookahead_days: int


@dataclass(frozen=True)
class MatchingConfig:
    exact_normalized: bool
    fuzzy: bool


@dataclass(frozen=True)
class RetentionConfig:
    history_days: int
    artifacts_days: int


@dataclass(frozen=True)
class SmtpConfig:
    host: str | None = None
    port: int | None = None
    username_env: str | None = None
    password_env: str | None = None
    from_email: str | None = None
    to_email: str | None = None


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool
    smtp: SmtpConfig


@dataclass(frozen=True)
class BookerConfig:
    enabled: bool
    dry_run: bool
    club: ClubConfig
    courses: list[CourseConfig]
    time_windows: dict[str, list[TimeWindow]]
    release_job: ReleaseJobConfig
    hourly_job: HourlyJobConfig
    matching: MatchingConfig
    default_duration_minutes: int
    buffer_minutes: int
    retention: RetentionConfig
    notifications: NotificationConfig
    secrets: dict[str, str]


def load_config(
    path: str | Path,
    *,
    env_path: str | Path | None = ".env",
    require_live_credentials: bool = True,
) -> BookerConfig:
    raw = _parse_yaml_subset(Path(path))
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")
    secrets = _load_env(Path(env_path)) if env_path is not None and Path(env_path).exists() else {}
    config = _build_config(raw, secrets)
    _validate(config, require_live_credentials=require_live_credentials)
    return config


def effective_dry_run(config: BookerConfig, *, cli_dry_run: bool) -> bool:
    return config.dry_run or cli_dry_run


def redact(value: str) -> str:
    redacted = value
    for key in SECRET_KEYS:
        redacted = re.sub(rf"({key}[A-Z0-9_]*=)[^\s]+", r"\1[redacted]", redacted, flags=re.I)
    return redacted


def _build_config(raw: dict[str, Any], secrets: dict[str, str]) -> BookerConfig:
    club = _mapping(raw, "club")
    release_job = _mapping(raw, "release_job")
    hourly_job = _mapping(raw, "hourly_job")
    matching = _mapping(raw, "matching")
    retention = _mapping(raw, "retention")
    notifications = _mapping(raw, "notifications")
    smtp = notifications.get("smtp", {})
    if smtp is None:
        smtp = {}
    if not isinstance(smtp, dict):
        raise ConfigError("SMTP config must be a mapping")

    return BookerConfig(
        enabled=_bool(raw, "enabled"),
        dry_run=_bool(raw, "dry_run"),
        club=ClubConfig(
            name=_str(club, "name"),
            booking_url=_optional_str(club, "booking_url"),
            storage_state_path=_optional_str(club, "storage_state_path") or "storage/aspria-state.json",
        ),
        courses=_courses(raw.get("courses")),
        time_windows=_time_windows(raw.get("time_windows")),
        release_job=ReleaseJobConfig(
            poll_start=_str(release_job, "poll_start"),
            tight_poll_until=_str(release_job, "tight_poll_until"),
            tight_interval_seconds=_int(release_job, "tight_interval_seconds"),
            slow_poll_until=_str(release_job, "slow_poll_until"),
            slow_interval_seconds=_int(release_job, "slow_interval_seconds"),
        ),
        hourly_job=HourlyJobConfig(lookahead_days=_int(hourly_job, "lookahead_days")),
        matching=MatchingConfig(
            exact_normalized=_bool(matching, "exact_normalized"),
            fuzzy=_bool(matching, "fuzzy"),
        ),
        default_duration_minutes=_int(raw, "default_duration_minutes"),
        buffer_minutes=_int(raw, "buffer_minutes"),
        retention=RetentionConfig(
            history_days=_int(retention, "history_days"),
            artifacts_days=_int(retention, "artifacts_days"),
        ),
        notifications=NotificationConfig(
            enabled=_bool(notifications, "enabled"),
            smtp=SmtpConfig(
                host=_optional_str(smtp, "host"),
                port=_optional_int(smtp, "port"),
                username_env=_optional_str(smtp, "username_env"),
                password_env=_optional_str(smtp, "password_env"),
                from_email=_optional_str(smtp, "from_email"),
                to_email=_optional_str(smtp, "to_email"),
            ),
        ),
        secrets=secrets,
    )


def _validate(config: BookerConfig, *, require_live_credentials: bool) -> None:
    for value in [
        config.release_job.poll_start,
        config.release_job.tight_poll_until,
        config.release_job.slow_poll_until,
    ]:
        _validate_time(value)
    for windows in config.time_windows.values():
        for window in windows:
            _validate_time(window.start)
            _validate_time(window.end)
    if config.default_duration_minutes < 0:
        raise ConfigError("default duration must not be negative")
    if config.buffer_minutes < 0:
        raise ConfigError("buffer must not be negative")
    release_start = _minutes_from_text(config.release_job.poll_start)
    tight_stop = _minutes_from_text(config.release_job.tight_poll_until)
    slow_stop = _minutes_from_text(config.release_job.slow_poll_until)
    if not release_start <= tight_stop <= slow_stop:
        raise ConfigError("release polling times must be ordered from start through slow stop")
    if not 5 <= config.release_job.tight_interval_seconds <= 10:
        raise ConfigError("tight polling interval must be between 5 and 10 seconds")
    if config.release_job.slow_interval_seconds < 1:
        raise ConfigError("poll intervals must not be negative")
    if config.hourly_job.lookahead_days < 0:
        raise ConfigError("hourly lookahead days must not be negative")
    if config.notifications.enabled:
        smtp = config.notifications.smtp
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
        if smtp.password_env and not config.secrets.get(smtp.password_env):
            missing.append(f"SMTP secret {smtp.password_env}")
        if missing:
            raise ConfigError("missing SMTP values: " + ", ".join(missing))
    if (
        require_live_credentials
        and not config.dry_run
        and (not config.secrets.get("ASPRIA_EMAIL") or not config.secrets.get("ASPRIA_PASSWORD"))
    ):
        raise ConfigError("dry_run: false requires ASPRIA_EMAIL and ASPRIA_PASSWORD")


def _validate_time(value: str) -> None:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise ConfigError(f"invalid time format: {value!r}")


def _minutes_from_text(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _courses(value: Any) -> list[CourseConfig]:
    if not isinstance(value, list) or not value:
        raise ConfigError("courses must be a non-empty list")
    courses = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("course name must not be empty")
        courses.append(CourseConfig(name=name.strip()))
    return courses


def _time_windows(value: Any) -> dict[str, list[TimeWindow]]:
    if not isinstance(value, dict):
        raise ConfigError("time_windows must be a mapping")
    windows: dict[str, list[TimeWindow]] = {}
    for weekday, entries in value.items():
        if weekday not in WEEKDAYS:
            raise ConfigError(f"unknown weekday: {weekday}")
        if not isinstance(entries, list):
            raise ConfigError(f"time windows for {weekday} must be a list")
        windows[weekday] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"time window for {weekday} must be a mapping")
            windows[weekday].append(TimeWindow(start=_str(entry, "from"), end=_str(entry, "to")))
    return windows


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value.strip())
    return values


def _parse_yaml_subset(path: Path) -> Any:
    lines = [
        (len(line) - len(line.lstrip(" ")), line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    parsed, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise ConfigError(f"could not parse YAML near: {lines[index][1]}")
    return parsed


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    current_indent, text = lines[index]
    if current_indent != indent:
        raise ConfigError(f"unexpected indentation near: {text}")
    if text.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ConfigError(f"unexpected indentation near: {text}")
        if text.startswith("- "):
            break
        key, separator, remainder = text.partition(":")
        if not separator:
            raise ConfigError(f"expected key/value pair near: {text}")
        key = key.strip()
        if remainder.strip():
            result[key] = _scalar(remainder.strip())
            index += 1
        else:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                result[key], index = _parse_block(lines, index, lines[index][0])
            else:
                result[key] = None
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ConfigError(f"unexpected indentation near: {text}")
        if not text.startswith("- "):
            break
        item = text[2:].strip()
        if not item:
            index += 1
            value, index = _parse_block(lines, index, lines[index][0])
            result.append(value)
        elif ":" in item and not _is_quoted(item):
            key, _, remainder = item.partition(":")
            mapping: dict[str, Any] = {key.strip(): _scalar(remainder.strip()) if remainder.strip() else None}
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                nested, index = _parse_mapping(lines, index, lines[index][0])
                mapping.update(nested)
            result.append(mapping)
        else:
            result.append(_scalar(item))
            index += 1
    return result, index


def _scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if _is_quoted(value):
        return _unquote(value)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}


def _unquote(value: str) -> str:
    if _is_quoted(value):
        return value[1:-1]
    return value
