from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict

from aspria_booker.artifacts import ArtifactStore
from aspria_booker.config import BookerConfig, ClubConfig


ASPRIA_HANNOVER_MASCHSEE_URL = "https://www.aspria.com/de/hannover-maschsee"


class LoginStatus(Enum):
    AUTHENTICATED = "authenticated"
    REUSED_STORAGE = "reused_storage"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    PROTECTIVE_CHALLENGE = "protective_challenge"
    FAILED = "failed"


@dataclass(frozen=True)
class LoginOutcome:
    status: LoginStatus
    message: str
    artifact_html: str | None = None
    artifact_screenshot: bytes | None = None
    artifact_metadata: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in {LoginStatus.AUTHENTICATED, LoginStatus.REUSED_STORAGE}


class _LoginArtifact(TypedDict):
    artifact_html: str | None
    artifact_screenshot: bytes | None
    artifact_metadata: dict[str, Any]


class BrowserProbe(Protocol):
    def is_storage_state_valid(self, *, start_url: str, storage_state_path: Path) -> bool:
        raise NotImplementedError

    def relogin(
        self,
        *,
        start_url: str,
        storage_state_path: Path,
        email: str,
        password: str,
    ) -> LoginOutcome:
        raise NotImplementedError


class BrowserSessionManager:
    def __init__(
        self,
        *,
        club: ClubConfig,
        secrets: dict[str, str],
        probe: BrowserProbe,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._club = club
        self._secrets = secrets
        self._probe = probe
        self._artifacts = artifacts

    def ensure_authenticated(self, *, run_id: str | None = None) -> LoginOutcome:
        start_url = booking_start_url(self._club)
        storage_state_path = configured_storage_state_path(self._club)
        if storage_state_path.exists() and self._probe.is_storage_state_valid(
            start_url=start_url,
            storage_state_path=storage_state_path,
        ):
            return LoginOutcome(LoginStatus.REUSED_STORAGE, "saved storage state is valid")

        email = self._secrets.get("ASPRIA_EMAIL")
        password = self._secrets.get("ASPRIA_PASSWORD")
        if not email or not password:
            outcome = LoginOutcome(
                LoginStatus.MANUAL_INTERVENTION_REQUIRED,
                "headless re-login requires ASPRIA_EMAIL and ASPRIA_PASSWORD",
            )
            self._save_login_artifact(run_id=run_id, outcome=outcome, start_url=start_url)
            return outcome

        outcome = self._probe.relogin(
            start_url=start_url,
            storage_state_path=storage_state_path,
            email=email,
            password=password,
        )
        self._save_login_artifact(run_id=run_id, outcome=outcome, start_url=start_url)
        return outcome

    def _save_login_artifact(self, *, run_id: str | None, outcome: LoginOutcome, start_url: str) -> None:
        if self._artifacts is None or outcome.status is LoginStatus.AUTHENTICATED:
            return
        if outcome.status is LoginStatus.REUSED_STORAGE:
            return
        self._artifacts.save(
            run_id=run_id or "login",
            trigger="manual_intervention",
            occurred_at=datetime.now(timezone.utc),
            html=outcome.artifact_html,
            screenshot=outcome.artifact_screenshot,
            metadata={
                "status": outcome.status.value,
                "message": outcome.message,
                "start_url": start_url,
                "required_secrets": ["ASPRIA_EMAIL", "ASPRIA_PASSWORD"],
                **(outcome.artifact_metadata or {}),
            },
        )


def setup_interactive_login(config: BookerConfig, *, headed: bool) -> LoginOutcome:
    driver = PlaywrightInteractiveLogin()
    return driver.run(
        start_url=booking_start_url(config.club),
        storage_state_path=configured_storage_state_path(config.club),
        headed=headed,
    )


def booking_start_url(club: ClubConfig) -> str:
    return club.booking_url or ASPRIA_HANNOVER_MASCHSEE_URL


def configured_storage_state_path(club: ClubConfig) -> Path:
    return Path(club.storage_state_path)


class PlaywrightInteractiveLogin:
    def run(self, *, start_url: str, storage_state_path: Path, headed: bool) -> LoginOutcome:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError:
            return LoginOutcome(
                LoginStatus.FAILED,
                "Playwright is not installed; install project browser dependencies before setup-login",
            )

        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not headed)
                context = browser.new_context()
                page = context.new_page()
                page.goto(start_url)
                input(
                    "Complete login in the opened browser, navigate to the booking flow, "
                    "then press Enter here to save storage state."
                )
                context.storage_state(path=str(storage_state_path))
                browser.close()
        except (OSError, PlaywrightError) as error:
            return LoginOutcome(LoginStatus.FAILED, f"interactive login failed: {error}")

        return LoginOutcome(LoginStatus.AUTHENTICATED, f"storage state saved to {storage_state_path}")


PlaywrightFactory = Callable[[], Any]


class PlaywrightBrowserProbe:
    def __init__(self, *, playwright_factory: PlaywrightFactory | None = None) -> None:
        self._playwright_factory = playwright_factory or _default_playwright_factory

    def is_storage_state_valid(self, *, start_url: str, storage_state_path: Path) -> bool:
        try:
            with self._playwright_factory() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(storage_state=str(storage_state_path))
                    page = context.new_page()
                    page.goto(start_url, wait_until="domcontentloaded")
                    state = _classify_login_page(page)
                    return state in {_LoginPageState.AUTHENTICATED, _LoginPageState.UNKNOWN}
                finally:
                    browser.close()
        except Exception:
            return False

    def relogin(
        self,
        *,
        start_url: str,
        storage_state_path: Path,
        email: str,
        password: str,
    ) -> LoginOutcome:
        try:
            with self._playwright_factory() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(start_url, wait_until="domcontentloaded")
                    initial_state = _classify_login_page(page)
                    if initial_state is _LoginPageState.AUTHENTICATED:
                        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(storage_state_path))
                        return LoginOutcome(LoginStatus.AUTHENTICATED, "storage state refreshed")
                    if initial_state is _LoginPageState.PROTECTIVE_CHALLENGE:
                        return LoginOutcome(
                            LoginStatus.PROTECTIVE_CHALLENGE,
                            "login stopped because a protective challenge or manual verification page is visible",
                            **_login_artifact(page, state=initial_state),
                        )
                    if initial_state is _LoginPageState.UNKNOWN:
                        return LoginOutcome(
                            LoginStatus.MANUAL_INTERVENTION_REQUIRED,
                            "login stopped because the page state was not a recognizable login form",
                            **_login_artifact(page, state=initial_state),
                        )

                    _fill_first(page, _EMAIL_SELECTORS, email)
                    _fill_first(page, _PASSWORD_SELECTORS, password)
                    _click_first(page, _SUBMIT_SELECTORS)
                    next_state = _classify_login_page(page)
                    if next_state is _LoginPageState.AUTHENTICATED:
                        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(storage_state_path))
                        return LoginOutcome(LoginStatus.AUTHENTICATED, "storage state refreshed")
                    if next_state is _LoginPageState.PROTECTIVE_CHALLENGE:
                        return LoginOutcome(
                            LoginStatus.PROTECTIVE_CHALLENGE,
                            "login stopped because a protective challenge or manual verification page appeared",
                            **_login_artifact(page, state=next_state),
                        )
                    return LoginOutcome(
                        LoginStatus.MANUAL_INTERVENTION_REQUIRED,
                        "login form did not reach an authenticated booking page",
                        **_login_artifact(page, state=next_state),
                    )
                finally:
                    browser.close()
        except Exception as error:
            return LoginOutcome(
                LoginStatus.FAILED,
                f"headless re-login failed: {_safe_error(error, secrets=(email, password))}",
            )


class _LoginPageState(Enum):
    AUTHENTICATED = "authenticated"
    LOGIN_FORM = "login_form"
    PROTECTIVE_CHALLENGE = "protective_challenge"
    UNKNOWN = "unknown"


_AUTHENTICATED_TEXTS = (
    "meine buchungen",
    "abmelden",
    "freie plätze",
    "freie plaetze",
    "warteliste",
)
_LOGIN_TEXTS = (
    "anmelden",
    "einloggen",
    "login",
)
_PROTECTIVE_TEXTS = (
    "captcha",
    "2fa",
    "two-factor",
    "two factor",
    "sicherheitscode",
    "verifizierung",
    "ungewohnliche aktivitat",
    "ungewöhnliche aktivität",
    "suspicious activity",
    "automation",
    "automatisierung",
)
_EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input[autocomplete='email']",
)
_PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
)
_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Anmelden')",
    "button:has-text('Einloggen')",
)


def _default_playwright_factory() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _classify_login_page(page: Any) -> _LoginPageState:
    text = _normalized_page_text(page)
    if _matched_markers(text, _PROTECTIVE_TEXTS):
        return _LoginPageState.PROTECTIVE_CHALLENGE
    if _has_any_locator(page, _PASSWORD_SELECTORS) or _matched_markers(text, _LOGIN_TEXTS):
        return _LoginPageState.LOGIN_FORM
    if _matched_markers(text, _AUTHENTICATED_TEXTS):
        return _LoginPageState.AUTHENTICATED
    return _LoginPageState.UNKNOWN


def _login_artifact(page: Any, *, state: _LoginPageState) -> _LoginArtifact:
    return {
        "artifact_html": _safe_page_content(page),
        "artifact_screenshot": _safe_page_screenshot(page),
        "artifact_metadata": _page_metadata(page, state=state),
    }


def _page_metadata(page: Any, *, state: _LoginPageState) -> dict[str, object]:
    text = _normalized_page_text(page)
    title = ""
    try:
        title = str(page.title())
    except Exception:
        title = ""
    return {
        "page_url": str(getattr(page, "url", "")),
        "page_title": title,
        "login_state": state.value,
        "matched_protective_markers": _matched_markers(text, _PROTECTIVE_TEXTS),
        "matched_login_markers": _matched_markers(text, _LOGIN_TEXTS),
        "matched_signed_in_markers": _matched_markers(text, _AUTHENTICATED_TEXTS),
    }


def _matched_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in text]


def _safe_page_content(page: Any) -> str | None:
    try:
        return str(page.content())
    except Exception:
        return None


def _safe_page_screenshot(page: Any) -> bytes | None:
    try:
        return bytes(page.screenshot(full_page=True))
    except Exception:
        return None


def _normalized_page_text(page: Any) -> str:
    content = _visible_page_text(page)
    url = str(getattr(page, "url", ""))
    return f"{url}\n{content}".lower()


def _visible_page_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1000))
    except Exception:
        try:
            return str(page.content())
        except Exception:
            return ""


def _has_any_locator(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> None:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.fill(value)
            return
    raise RuntimeError("expected login field was not found")


def _click_first(page: Any, selectors: tuple[str, ...]) -> None:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.click()
            return
    raise RuntimeError("expected login submit control was not found")


def _safe_error(error: Exception, *, secrets: tuple[str, ...] = ()) -> str:
    message = str(error)
    for value in secrets:
        if value:
            message = message.replace(value, "[redacted]")
    for key in ("password", "cookie", "authorization", "token", "secret"):
        message = message.replace(key, "[redacted]")
    return message
