from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aspria_booker.artifacts import ArtifactStore
from aspria_booker.config import ClubConfig
from aspria_booker.session import (
    BrowserProbe,
    BrowserSessionManager,
    LoginOutcome,
    LoginStatus,
    PlaywrightBrowserProbe,
)


class FakeProbe(BrowserProbe):
    def __init__(
        self,
        *,
        storage_valid: bool = False,
        login_outcome: LoginOutcome | None = None,
    ) -> None:
        self.storage_valid = storage_valid
        self.login_outcome = login_outcome or LoginOutcome(LoginStatus.AUTHENTICATED, "logged in")
        self.checked_paths: list[Path] = []
        self.login_calls: list[tuple[str, Path, str, str]] = []

    def is_storage_state_valid(self, *, start_url: str, storage_state_path: Path) -> bool:
        self.checked_paths.append(storage_state_path)
        return self.storage_valid

    def relogin(
        self,
        *,
        start_url: str,
        storage_state_path: Path,
        email: str,
        password: str,
    ) -> LoginOutcome:
        self.login_calls.append((start_url, storage_state_path, email, password))
        return self.login_outcome


def test_session_reuses_existing_valid_storage_state(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    storage_path.write_text("{}", encoding="utf-8")
    probe = FakeProbe(storage_valid=True)
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url="https://example.invalid/book",
            storage_state_path=str(storage_path),
        ),
        secrets={"ASPRIA_EMAIL": "person@example.invalid", "ASPRIA_PASSWORD": "secret"},
        probe=probe,
    )

    outcome = manager.ensure_authenticated()

    assert outcome.status is LoginStatus.REUSED_STORAGE
    assert probe.checked_paths == [storage_path]
    assert probe.login_calls == []


def test_session_refreshes_expired_storage_with_credentials(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    storage_path.write_text("{}", encoding="utf-8")
    probe = FakeProbe(storage_valid=False)
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url="https://example.invalid/book",
            storage_state_path=str(storage_path),
        ),
        secrets={"ASPRIA_EMAIL": "person@example.invalid", "ASPRIA_PASSWORD": "secret"},
        probe=probe,
    )

    outcome = manager.ensure_authenticated()

    assert outcome.status is LoginStatus.AUTHENTICATED
    assert probe.login_calls == [
        ("https://example.invalid/book", storage_path, "person@example.invalid", "secret")
    ]


def test_session_stops_when_manual_intervention_is_required(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    probe = FakeProbe(
        storage_valid=False,
        login_outcome=LoginOutcome(LoginStatus.MANUAL_INTERVENTION_REQUIRED, "2FA required"),
    )
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url=None,
            storage_state_path=str(storage_path),
        ),
        secrets={"ASPRIA_EMAIL": "person@example.invalid", "ASPRIA_PASSWORD": "secret"},
        probe=probe,
    )

    outcome = manager.ensure_authenticated()

    assert outcome.status is LoginStatus.MANUAL_INTERVENTION_REQUIRED
    assert "2FA" in outcome.message
    assert probe.login_calls[0][0] == "https://www.aspria.com/de/hannover-maschsee"


def test_session_requires_credentials_before_headless_relogin(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url="https://example.invalid/book",
            storage_state_path=str(tmp_path / "state.json"),
        ),
        secrets={},
        probe=FakeProbe(storage_valid=False),
    )

    outcome = manager.ensure_authenticated()

    assert outcome.status is LoginStatus.MANUAL_INTERVENTION_REQUIRED
    assert "ASPRIA_EMAIL" in outcome.message


def test_session_saves_manual_intervention_artifact_with_run_id(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url="https://example.invalid/book",
            storage_state_path=str(tmp_path / "state.json"),
        ),
        secrets={},
        probe=FakeProbe(storage_valid=False),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )

    outcome = manager.ensure_authenticated(run_id="run-login")

    assert outcome.status is LoginStatus.MANUAL_INTERVENTION_REQUIRED
    artifact_dirs = list((tmp_path / "artifacts" / "run-login").glob("*-manual-intervention"))
    assert len(artifact_dirs) == 1
    assert "ASPRIA_PASSWORD" in (artifact_dirs[0] / "metadata.json").read_text(encoding="utf-8")


def test_session_saves_login_page_diagnostics_for_protective_challenge(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        club=ClubConfig(
            name="Aspria Hannover Maschsee",
            booking_url="https://example.invalid/book",
            storage_state_path=str(tmp_path / "state.json"),
        ),
        secrets={"ASPRIA_EMAIL": "person@example.invalid", "ASPRIA_PASSWORD": "server-password-secret"},
        probe=FakeProbe(
            storage_valid=False,
            login_outcome=LoginOutcome(
                LoginStatus.PROTECTIVE_CHALLENGE,
                "challenge visible",
                artifact_html="<main>Captcha erforderlich</main>",
                artifact_screenshot=b"fake-png",
                artifact_metadata={
                    "page_url": "https://example.invalid/challenge",
                    "matched_protective_markers": ["captcha"],
                },
            ),
        ),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )

    outcome = manager.ensure_authenticated(run_id="run-login")

    assert outcome.status is LoginStatus.PROTECTIVE_CHALLENGE
    artifact_dirs = list((tmp_path / "artifacts" / "run-login").glob("*-manual-intervention"))
    assert len(artifact_dirs) == 1
    assert (artifact_dirs[0] / "page.html").read_text(encoding="utf-8") == "<main>Captcha erforderlich</main>"
    assert (artifact_dirs[0] / "screenshot.png").read_bytes() == b"fake-png"
    metadata = json.loads((artifact_dirs[0] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["page_url"] == "https://example.invalid/challenge"
    assert metadata["matched_protective_markers"] == ["captcha"]


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return self._page.count(self._selector)

    @property
    def first(self) -> "FakeLocator":
        return self

    def fill(self, value: str) -> None:
        if self._page.fill_error is not None:
            raise self._page.fill_error
        self._page.filled.append((self._selector, value))

    def click(self) -> None:
        self._page.clicked.append(self._selector)
        if self._page.after_click_html is not None:
            self._page.html = self._page.after_click_html
            self._page.visible_text = self._page.after_click_html
        if self._page.after_click_url is not None:
            self._page.url = self._page.after_click_url

    def inner_text(self, *, timeout: int | None = None) -> str:
        if self._selector == "body":
            return self._page.visible_text
        return ""


class FakePage:
    def __init__(
        self,
        *,
        html: str,
        url: str = "https://example.invalid/book",
        after_click_html: str | None = None,
        after_click_url: str | None = None,
        fill_error: Exception | None = None,
        visible_text: str | None = None,
    ) -> None:
        self.html = html
        self.visible_text = visible_text if visible_text is not None else html
        self.url = url
        self.after_click_html = after_click_html
        self.after_click_url = after_click_url
        self.fill_error = fill_error
        self.visited: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    def goto(self, url: str, *, wait_until: str | None = None) -> None:
        self.visited.append(url)

    def title(self) -> str:
        return "Fake Login Page"

    def content(self) -> str:
        return self.html

    def screenshot(self, *, full_page: bool) -> bytes:
        return b"fake-screenshot"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def count(self, selector: str) -> int:
        if selector == "input[type='email']":
            return self.html.count("type='email'")
        if selector == "input[type='password']":
            return self.html.count("type='password'")
        if selector == "button[type='submit']":
            return self.html.count("type='submit'")
        return 0


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.storage_state_paths: list[str] = []

    def new_page(self) -> FakePage:
        return self.page

    def storage_state(self, *, path: str | Path) -> dict[str, Any]:
        self.storage_state_paths.append(str(path))
        return {}


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.context_kwargs: list[dict[str, object]] = []
        self.closed = False

    def new_context(self, **kwargs: object) -> FakeContext:
        self.context_kwargs.append(kwargs)
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_kwargs: list[dict[str, object]] = []

    def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_kwargs.append(kwargs)
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)


class FakePlaywrightFactory:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def __call__(self) -> "FakePlaywrightFactory":
        return self

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


def test_playwright_probe_reuses_storage_when_booking_page_is_authenticated(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(html="<main>Meine Buchungen Kurs buchen</main>")
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    valid = probe.is_storage_state_valid(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
    )

    assert valid is True
    assert browser.context_kwargs == [{"storage_state": str(storage_path)}]
    assert page.visited == ["https://example.invalid/book"]
    assert browser.closed is True


def test_playwright_probe_treats_login_form_as_expired_storage(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(html="<form><input type='email'><input type='password'>Anmelden</form>")
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    valid = probe.is_storage_state_valid(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
    )

    assert valid is False


def test_playwright_probe_accepts_public_start_page_when_storage_exists(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(
        html="<script>captcha bot</script><main>Aspria Hannover Maschsee Kurs Buchen</main>",
        visible_text="Aspria Hannover Maschsee Kurs Buchen",
    )
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    valid = probe.is_storage_state_valid(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
    )

    assert valid is True


def test_playwright_probe_does_not_treat_marketing_angebote_as_bot_challenge(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(
        html="<main>Maßgeschneiderte Mitgliedschaftsangebote</main>",
        visible_text="Maßgeschneiderte Mitgliedschaftsangebote",
    )
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    valid = probe.is_storage_state_valid(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
    )

    assert valid is True


def test_playwright_probe_headless_relogin_saves_refreshed_storage_state(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(
        html="<form><input type='email'><input type='password'><button type='submit'>Anmelden</button></form>",
        after_click_html="<main>Meine Buchungen Kurs buchen</main>",
        after_click_url="https://example.invalid/book",
    )
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    outcome = probe.relogin(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
        email="person@example.invalid",
        password="server-password-secret",
    )

    assert outcome.status is LoginStatus.AUTHENTICATED
    assert context.storage_state_paths == [str(storage_path)]
    assert ("input[type='email']", "person@example.invalid") in page.filled
    assert ("input[type='password']", "server-password-secret") in page.filled
    assert "server-password-secret" not in outcome.message


def test_playwright_probe_stops_on_protective_challenge_without_saving_state(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(html="<h1>Captcha erforderlich</h1><p>2FA Code eingeben</p>")
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    outcome = probe.relogin(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
        email="person@example.invalid",
        password="server-password-secret",
    )

    assert outcome.status is LoginStatus.PROTECTIVE_CHALLENGE
    assert context.storage_state_paths == []
    assert page.filled == []
    assert "server-password-secret" not in outcome.message


def test_playwright_probe_redacts_credentials_from_failure_messages(tmp_path: Path) -> None:
    storage_path = tmp_path / "state.json"
    page = FakePage(
        html="<form><input type='email'><input type='password'><button type='submit'>Anmelden</button></form>",
        fill_error=RuntimeError("cannot fill server-password-secret for person@example.invalid"),
    )
    context = FakeContext(page)
    browser = FakeBrowser(context)
    probe = PlaywrightBrowserProbe(playwright_factory=FakePlaywrightFactory(FakePlaywright(browser)))

    outcome = probe.relogin(
        start_url="https://example.invalid/book",
        storage_state_path=storage_path,
        email="person@example.invalid",
        password="server-password-secret",
    )

    assert outcome.status is LoginStatus.FAILED
    assert "server-password-secret" not in outcome.message
    assert "person@example.invalid" not in outcome.message
