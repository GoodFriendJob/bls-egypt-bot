"""Login + session persistence for the BLS Spain Egypt portal.

Selector policy — read before touching anything here
----------------------------------------------------
Confirmed via DevTools on the live portal (CLAUDE.md → Portal Research Findings):

* The login form renders 10 ``input[type="text"]`` fields. Nine are honeypots.
* Every id and name is regenerated on each page load.
* **The real field's position also randomises on each load** — Field 0 on one
  load, Field 3 on the next. ``.first`` and any fixed index are therefore wrong.
* The only stable discriminator: each input sits in a ``div.mb-3`` wrapper, and
  only the real field's wrapper has computed ``display: block``. Honeypot
  wrappers are ``display: none``.

So every field in this module is located with ``utils.find_real_input()``, which
implements exactly that check. Nothing here may select an input by index, id,
name, or generated class.

Buttons are a different story — ``#btnVerify`` is a confirmed stable id and is
used directly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from modules import utils

SESSION_FILE = Path("session/cookies.json")

# The real email box is an input[type="text"] whose div.mb-3 wrapper is display:block.
EMAIL_INPUT_TYPE = "text"

# TODO(unconfirmed — needs live portal inspection): the password page has not been
# inspected. CLAUDE.md expects the same honeypot pattern as the email step, so the
# same div.mb-3 / display:block discriminator should apply — confirm before
# trusting it, and answer:
#   * Is the real box input[type="password"] or input[type="text"]?
#   * Are decoy password fields present, and do they also sit in div.mb-3?
#   * Is the wrapper still div.mb-3 on this page, or a different class?
#   * Does the step-1 email field persist, or is it replaced?
#   * Is there a distinct submit button id (step 1 uses #btnVerify)?
# Until then both input types are probed through find_real_input(), password first.
PASSWORD_INPUT_TYPES = ("password", "text")

VERIFY_BUTTON_SELECTOR = "#btnVerify"  # CONFIRMED: stable id
VERIFY_BUTTON_TEXTS = ("verify", "continue", "next", "submit", "proceed", "تحقق", "متابعة")
LOGIN_BUTTON_TEXTS = ("login", "log in", "sign in", "submit", "دخول", "تسجيل")
COOKIE_ACCEPT_TEXTS = ("accept", "agree", "got it", "ok", "موافق")
LOGGED_IN_MARKERS = ("logout", "log out", "sign out", "my account", "تسجيل الخروج")

# CONFIRMED: the login form POSTs here. The hidden fields
# (__RequestVerificationToken, ResponseData, ReturnUrl, Id) ride along with the
# native form submit — the bot must never set them itself.
LOGIN_SUBMIT_PATH = "/Global/account/LoginSubmit"

BUTTON_TIMEOUT_MS = 5000

# Candidate containers for a captcha / challenge widget, dumped verbatim when one
# is detected so the markup can be analysed without re-triggering a login.
CAPTCHA_DOM_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='captcha']",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[class*='captcha']",
    "[id*='captcha']",
    "[class*='Captcha']",
    "[id*='Captcha']",
    "[class*='puzzle']",
    "[class*='slider-verify']",
    "[class*='challenge']",
    "[id*='challenge']",
    "[class*='verify']",
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
]


class LoginError(RuntimeError):
    """Raised when the bot cannot establish an authenticated session."""


class PortalUnreachableError(LoginError):
    """The portal served an HTTP error page instead of the app.

    Most commonly a 403 from the AWS load balancer, which BLS returns to IPs
    outside Egypt and to datacenter/hosting ASNs. Retrying the login is pointless
    in that state — there is no form on the page to fill — so this is raised
    immediately rather than being left to surface as a confusing
    "could not locate the real email field" further down the flow.
    """


class BLSAuth:
    """Owns the Playwright lifecycle and keeps an authenticated page available."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        state: Any = None,
        notifier: Any = None,
    ) -> None:
        self.config = config
        self.state = state
        self.notifier = notifier

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._logged_in = False

        self.session_file = Path(SESSION_FILE)

    # ------------------------------------------------------------------ #
    # Browser lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch Chromium, build a stealthed context, restore saved cookies."""
        if self._browser is not None:
            return

        browser_cfg = self.config.get("browser", {}) or {}
        proxy_cfg = self.config.get("proxy", {}) or {}

        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": bool(self.config.get("headless", True)),
            "args": LAUNCH_ARGS,
        }
        if proxy_cfg.get("enabled") and proxy_cfg.get("server"):
            proxy: dict[str, Any] = {"server": proxy_cfg["server"]}
            if proxy_cfg.get("username"):
                proxy["username"] = proxy_cfg["username"]
                proxy["password"] = proxy_cfg.get("password", "")
            launch_kwargs["proxy"] = proxy
            logger.info(f"auth: using proxy {proxy_cfg['server']}")

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {
            "locale": browser_cfg.get("locale", "en-US"),
            "timezone_id": browser_cfg.get("timezone", "Africa/Cairo"),
            "viewport": {"width": 1366, "height": 768},
            "ignore_https_errors": True,
        }
        if browser_cfg.get("user_agent"):
            context_kwargs["user_agent"] = browser_cfg["user_agent"]

        if self._has_saved_session():
            context_kwargs["storage_state"] = str(self.session_file)
            logger.info(f"auth: restoring session from {self.session_file}")

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_navigation_timeout(
            int(browser_cfg.get("navigation_timeout", 60000))
        )
        self._context.set_default_timeout(int(browser_cfg.get("action_timeout", 20000)))

        if await utils.apply_stealth(self._context):
            logger.debug("auth: stealth applied to context")

        self._page = await self._context.new_page()
        await utils.apply_stealth(self._page)
        self._log("browser launched", status="ok")

    async def stop(self) -> None:
        """Close everything, saving the session first on a best-effort basis."""
        if self._context is not None and self._logged_in:
            await self._save_session()
        for closer, label in ((self._context, "context"), (self._browser, "browser")):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:
                logger.debug(f"auth: closing {label} failed: {exc}")
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug(f"auth: stopping playwright failed: {exc}")
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        logger.info("auth: browser closed")

    @property
    def page(self) -> Page:
        if self._page is None:
            raise LoginError("browser is not started — call start() first")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise LoginError("browser is not started — call start() first")
        return self._context

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def ensure_logged_in(self, *, force: bool = False) -> Page:
        """Return an authenticated page, logging in or re-logging in as needed.

        Restored cookies are tried first; a login only happens when the session
        is missing or expired. Safe to call before every monitoring cycle.
        """
        async with self._lock:
            await self.start()
            page = self.page

            try:
                if not force and await self._is_logged_in(page):
                    self._logged_in = True
                    logger.debug("auth: existing session is still valid")
                    return page
            except PortalUnreachableError:
                # Blocked at the edge — there is no form to fill. Pause instead of
                # burning login attempts against an error page.
                self._logged_in = False
                if self.state is not None:
                    self.state.set_manual_pause("portal blocked (HTTP 403) — needs an Egyptian IP")
                raise

            attempts = max(1, int((self.config.get("retry", {}) or {}).get("login_attempts", 2)))
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                logger.info(f"auth: logging in (attempt {attempt}/{attempts})")
                try:
                    if await self._perform_login(page):
                        self._logged_in = True
                        await self._save_session()
                        self._log("login successful", status="ok")
                        return page
                    last_error = LoginError("login did not reach an authenticated page")
                except PortalUnreachableError:
                    self._logged_in = False
                    if self.state is not None:
                        self.state.set_manual_pause(
                            "portal blocked (HTTP 403) — needs an Egyptian IP"
                        )
                    raise
                except PlaywrightTimeout as exc:
                    last_error = exc
                    logger.warning(f"auth: timeout during login: {exc}")
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"auth: login attempt failed: {exc}")

                await utils.screenshot(page, f"login-failed-attempt{attempt}")
                if attempt < attempts:
                    await asyncio.sleep(5 * attempt)

            self._logged_in = False
            message = f"Login failed after {attempts} attempt(s): {last_error}"
            self._log(message, status="error")
            await self._alert_error(message, retry_info="monitoring paused until /resume")
            if self.state is not None:
                self.state.set_manual_pause("login failed — check credentials")
            raise LoginError(message)

    async def refresh_session(self) -> Page:
        """Force a fresh login, discarding any stored cookies."""
        self._clear_saved_session()
        return await self.ensure_logged_in(force=True)

    # ------------------------------------------------------------------ #
    # Login flow
    # ------------------------------------------------------------------ #
    async def _perform_login(self, page: Page) -> bool:
        """Two-step login: email + Verify, then password + submit."""
        await self._goto(page, self._path("login"))
        await utils.human_delay(self.config)
        await self._dismiss_cookie_banner(page)

        # --- Step 1: email -------------------------------------------------
        email = (self.config.get("bls", {}) or {}).get("email", "")
        if not email:
            raise LoginError("bls.email is empty in config.yaml")

        if not await self._fill_email(page, email):
            await utils.screenshot(page, "login-no-email-field")
            raise LoginError("could not locate the real email field among the honeypots")
        self._log("email submitted", status="ok")

        await utils.human_delay(self.config)
        await self._click_verify(page)
        await self._settle(page)
        await self._check_for_captcha(page)

        # --- Step 2: password ---------------------------------------------
        password = (self.config.get("bls", {}) or {}).get("password", "")
        if not password:
            raise LoginError("bls.password is empty in config.yaml")

        if not await self._fill_password(page, password):
            await utils.screenshot(page, "login-no-password-field")
            raise LoginError("password field did not appear after the email step")
        self._log("password submitted", status="ok")

        await utils.human_delay(self.config)
        await self._submit_login(page)
        await self._settle(page)
        await self._check_for_captcha(page)

        # TODO(unconfirmed): the live portal may insert an email/SMS OTP step
        # right here, before the account page loads. If that is observed, call
        # modules.otp.handle_otp(page, ...) at this point and only then evaluate
        # _is_logged_in(). Until it is confirmed, an OTP prompt simply makes the
        # login-detection check fail and the caller retries.
        return await self._is_logged_in(page)

    async def _fill_email(self, page: Page, email: str) -> bool:
        """Fill the one real email field.

        Located by ``find_real_input`` — the real input is the one whose
        ``div.mb-3`` wrapper computes to ``display: block``. Position, id and name
        all randomise per load, so none of them may be used.
        """
        element = await utils.find_real_input(page, EMAIL_INPUT_TYPE)

        if element is None:
            # No wrapper reported display:block. Either the markup changed or the
            # form had not finished rendering — retry once after a short settle.
            logger.warning("auth: no real email field on the first pass — retrying")
            await utils.human_delay(self.config)
            element = await utils.find_real_input(page, EMAIL_INPUT_TYPE)

        if element is None:
            # Last resort: computed-visibility scan (display/opacity/size/offscreen).
            logger.warning(
                "auth: div.mb-3 discriminator found nothing — falling back to the "
                "visibility scan; the login markup may have changed"
            )
            element = await utils.find_visible_input(
                page,
                kinds=("text", "email"),
                keywords=("email", "mail", "user", "بريد"),
            )

        if element is None:
            return False

        await utils.type_like_human(element, email)
        logger.info("auth: email filled")
        return True

    async def _fill_password(self, page: Page, password: str) -> bool:
        """Fill the password field on the second login step.

        TODO(unconfirmed — needs live portal inspection): see
        ``PASSWORD_INPUT_TYPES`` above. This assumes the email step's honeypot
        pattern repeats, so the same ``div.mb-3`` / ``display:block`` check is
        used. Once the real markup is captured, narrow this to the single
        confirmed input type and drop the loop.
        """
        for input_type in PASSWORD_INPUT_TYPES:
            element = await utils.find_real_input(page, input_type)
            if element is None:
                continue
            if input_type == "text":
                # Safety net: a text input only receives the password if it is
                # also genuinely visible, so a mislabelled honeypot can never
                # capture the credential.
                if not await utils.is_really_visible(element):
                    logger.warning(
                        "auth: candidate text field on the password step is not "
                        "genuinely visible — skipping it (honeypot guard)"
                    )
                    continue
            await utils.type_like_human(element, password)
            logger.info(f"auth: password filled (real input[type={input_type!r}])")
            return True

        logger.warning(
            "auth: div.mb-3 discriminator found no password field — "
            "falling back to the visibility scan"
        )
        element = await utils.find_visible_input(
            page, kinds=("password", "text"), keywords=("password", "pass", "مرور")
        )
        if element is None:
            return False
        await utils.type_like_human(element, password)
        logger.info("auth: password filled via visibility fallback")
        return True

    async def _click_verify(self, page: Page) -> bool:
        """Click the step-1 Verify button.

        CONFIRMED: ``#btnVerify``. Button ids are stable on this portal — only the
        *input fields* randomise — so targeting it directly is correct. Falls back
        to a text match, then to Enter, if the markup ever changes.
        """
        try:
            button = page.locator(VERIFY_BUTTON_SELECTOR)
            await button.wait_for(state="visible", timeout=BUTTON_TIMEOUT_MS)
            await button.click(timeout=BUTTON_TIMEOUT_MS)
            logger.info(f"auth: clicked Verify via {VERIFY_BUTTON_SELECTOR}")
            return True
        except PlaywrightTimeout:
            logger.warning(
                f"auth: {VERIFY_BUTTON_SELECTOR} not clickable — falling back to text match"
            )
        except Exception as exc:
            logger.warning(f"auth: {VERIFY_BUTTON_SELECTOR} click failed ({exc}) — falling back")

        if await utils.click_by_text(page, VERIFY_BUTTON_TEXTS, config=self.config):
            return True

        logger.debug("auth: no Verify button found — pressing Enter")
        await page.keyboard.press("Enter")
        return False

    async def _submit_login(self, page: Page) -> bool:
        """Submit the password step and watch the confirmed LoginSubmit POST.

        Observing the POST status is what distinguishes rejected credentials from
        the page silently re-rendering.
        """

        async def _click() -> None:
            if not await utils.click_by_text(page, LOGIN_BUTTON_TEXTS, config=self.config):
                logger.debug("auth: no Login button found — pressing Enter")
                await page.keyboard.press("Enter")

        try:
            async with page.expect_response(
                lambda response: LOGIN_SUBMIT_PATH.lower() in response.url.lower(),
                timeout=20000,
            ) as info:
                await _click()
            response = await info.value
            logger.info(f"auth: {LOGIN_SUBMIT_PATH} responded {response.status}")
            if response.status >= 400:
                self._log(f"login POST returned HTTP {response.status}", status="error")
            return response.status < 400
        except PlaywrightTimeout:
            # Not fatal: some flows submit via XHR to another path, or the page was
            # already navigating. _is_logged_in() is the real verdict.
            logger.debug(f"auth: no {LOGIN_SUBMIT_PATH} response observed")
            return False
        except Exception as exc:
            logger.debug(f"auth: login submit observation failed: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # Session detection + persistence
    # ------------------------------------------------------------------ #
    async def _is_logged_in(self, page: Page) -> bool:
        """Heuristic session check: load the account page and read the result.

        TODO(unconfirmed — needs live portal inspection): the post-login landing
        page has not been seen yet, so ``bls.paths.dashboard`` is a guess and this
        check leans on generic "logout"/"my account" text. Once logged in, capture
        the real account URL and a stable element that only exists in an
        authenticated session, then replace the phrase heuristics below.
        """
        try:
            await self._goto(page, self._path("dashboard"))
        except PortalUnreachableError:
            raise  # the portal is blocked — do not fall through to a login attempt
        except Exception as exc:
            logger.debug(f"auth: dashboard navigation failed: {exc}")
            return False

        await utils.human_delay(self.config, scale=0.5)
        url = (page.url or "").lower()
        text = await utils.page_text(page)

        if utils.contains_any(text, LOGGED_IN_MARKERS):
            return True

        # Still on a login screen, or bounced back to one.
        if any(hint in url for hint in ("login", "signin", "account/login")):
            return False
        if await utils.first_visible(page, 'input[type="password"]') is not None:
            return False
        if utils.contains_any(text, ("sign in", "log in", "تسجيل الدخول")):
            return False

        # Unexpected shape — capture it rather than guessing.
        logger.debug(f"auth: inconclusive session check at {page.url}")
        await utils.screenshot(page, "session-check-inconclusive")
        return False

    def _has_saved_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"auth: unreadable session file ({exc}) — ignoring it")
            return False
        return bool(data.get("cookies") or data.get("origins"))

    async def _save_session(self) -> None:
        if self._context is None:
            return
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(self.session_file))
            logger.info(f"auth: session saved to {self.session_file}")
        except Exception as exc:
            logger.warning(f"auth: could not save session: {exc}")

    def _clear_saved_session(self) -> None:
        try:
            if self.session_file.exists():
                self.session_file.unlink()
                logger.info("auth: stored session discarded")
        except OSError as exc:
            logger.warning(f"auth: could not delete session file: {exc}")

    # ------------------------------------------------------------------ #
    # Page helpers
    # ------------------------------------------------------------------ #
    def path(self, key: str) -> str:
        """Public accessor for a configured portal URL (used by monitor.py)."""
        return self._path(key)

    def _path(self, key: str) -> str:
        """Absolute URL for a configured portal path.

        TODO(unconfirmed): only the login path is verified. ``dashboard`` and
        ``appointment`` in ``bls.paths`` are placeholders — confirm both after a
        successful login and update config.example.yaml accordingly.
        """
        bls = self.config.get("bls", {}) or {}
        base = (bls.get("url") or "").rstrip("/")
        rel = ((bls.get("paths") or {}).get(key) or "/").lstrip("/")
        return f"{base}/{rel}" if rel else f"{base}/"

    async def _goto(self, page: Page, url: str) -> None:
        """Navigate, and fail loudly if the portal served an error page.

        Without this check an HTTP 403 just renders as a bare error document with
        no inputs, and the failure surfaces much later as a misleading
        "could not locate the real email field among the honeypots".
        """
        logger.debug(f"auth: navigating to {url}")
        response = await page.goto(url, wait_until="domcontentloaded")

        if response is not None and response.status >= 400:
            server = ""
            try:
                headers = await response.all_headers()
                server = headers.get("server", "")
            except Exception:
                pass

            shot = await utils.screenshot(page, f"http-{response.status}")
            message = f"portal returned HTTP {response.status} for {url}"
            if server:
                message += f" (server: {server})"
            if response.status == 403:
                message += (
                    " — BLS blocks requests from outside Egypt and from "
                    "datacenter/hosting IP ranges. Run from an Egyptian IP "
                    "(Egyptian VPS, or set proxy.enabled in config.yaml)."
                )

            logger.error(f"auth: {message}")
            self._log(message, status="error")
            await self._alert_error(message, retry_info="no retry — the IP must change first")
            if shot and self.notifier is not None:
                try:
                    await self.notifier.send_photo(shot, f"HTTP {response.status} from the portal")
                except Exception:
                    pass
            raise PortalUnreachableError(message)

        await self._settle(page)

    async def _settle(self, page: Page) -> None:
        """Wait for the network to go quiet, then add a human-ish pause."""
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            logger.debug("auth: networkidle timed out — continuing")
        await utils.human_delay(self.config, scale=0.5)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        if await utils.click_by_text(page, COOKIE_ACCEPT_TEXTS, config=self.config):
            logger.debug("auth: cookie banner dismissed")
            await utils.human_delay(self.config, scale=0.5)

    async def _check_for_captcha(self, page: Page) -> None:
        """Pause for a human when BLS throws a captcha at the login flow."""
        phrases = (self.config.get("detection", {}) or {}).get("captcha_phrases", [])
        hit = utils.contains_any(await utils.page_text(page), phrases)
        if not hit:
            return

        # Capture everything about the challenge before anything else touches the
        # page: screenshot, full DOM, and the widget markup in isolation.
        shot = await utils.screenshot(page, "login-captcha")
        full_dom = await utils.dump_page_html(page, "login-captcha")
        widget = await utils.dump_widget_dom(page, CAPTCHA_DOM_SELECTORS, "login-captcha")

        logger.warning(f"auth: captcha detected ({hit!r}) — manual step required")
        logger.warning(f"auth: captcha url={page.url}")
        logger.warning(
            f"auth: captcha artefacts — screenshot={shot} full_dom={full_dom} "
            f"widget_dom={widget.get('file')} matches={len(widget.get('matches', []))} "
            f"iframes={widget.get('iframes')}"
        )
        self._log(f"captcha detected: {hit}", status="warn")
        if self.state is not None:
            self.state.set_manual_pause("captcha on the login page")
        if self.notifier is not None:
            await self.notifier.manual_required(
                f"Captcha on the login page (matched {hit!r}). "
                "Solve it in the browser window, then send /resume.",
                screenshot=shot,
            )
            await self.notifier.wait_for_resume()
        if self.state is not None:
            self.state.clear_manual_pause()

    # ------------------------------------------------------------------ #
    # Logging / alert plumbing
    # ------------------------------------------------------------------ #
    def _log(self, message: str, *, status: str = "info") -> None:
        logger.info(f"auth: {message}")
        if self.state is not None:
            self.state.log_event("auth", message, status=status)

    async def _alert_error(self, message: str, *, retry_info: str = "") -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.error(message, retry_info=retry_info)
        except Exception as exc:
            logger.debug(f"auth: could not send error alert: {exc}")
