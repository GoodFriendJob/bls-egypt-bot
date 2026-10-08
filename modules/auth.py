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
import re
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

# --------------------------------------------------------------------------- #
# CAPTCHA (DOM-based — no image recognition needed)
#
# Confirmed structure from page source:
#   * Prompt text lives in  div.col-12.box-label  -> "Please select all boxes
#     with number NNN".
#   * Clickable tiles are  div.col-4  containing an <img> wired up with
#     onclick="Select('<id>', this)".
#   * A VISIBLE tile carries an inline  style="padding: 5px; display: block;"
#     A HIDDEN tile carries  style="padding:5px;"  with no display:block.
#     Several grids are stacked; only 9 tiles are visible at a time.
#   * Clicking a tile adds class "img-selected" to its <img>.
#   * The page's own Select() handler maintains the hidden input
#     "SelectedImages". The bot therefore only ever CLICKS - it must never write
#     SelectedImages itself, and must not touch the other hidden fields
#     (Id, ReturnUrl, ResponseData, Param, __RequestVerificationToken), which
#     ride along with the native form submit.
# --------------------------------------------------------------------------- #
CAPTCHA_BOX_LABEL_SELECTOR = "div.col-12.box-label"
CAPTCHA_TILE_SELECTOR = "div.col-4"
CAPTCHA_CONTAINER_SELECTOR = "div.main-div-container"
# Strongest, most specific signal that a challenge is on screen.
CAPTCHA_PROMPT_TEXT = "please select all boxes with number"
CAPTCHA_IMG_SELECTOR = "img.captcha-img"
CAPTCHA_SELECTED_CLASS = "img-selected"
CAPTCHA_SELECTED_INPUT_SELECTOR = 'input[name="SelectedImages"]'
CAPTCHA_NUMBER_REGEX = re.compile(r"number\s+(\d+)", re.I)
CAPTCHA_SUBMIT_SELECTORS = ('button:has-text("Submit")', 'input[value="Submit"]')
CAPTCHA_MAX_ATTEMPTS = 3
CAPTCHA_CLICK_DELAY = 0.5

# One pass over the DOM: resolve every visible tile to the number its label
# states, and report which labels are prompts rather than per-tile labels.
#
# Visibility is decided by the inline "display:block" marker (whitespace
# normalised, so both "display: block" and "display:block" match) AND by an
# ancestor walk - an inline display:block inside a hidden grid still computes to
# "block", so the inline marker alone is not sufficient.
_CAPTCHA_DETECT_JS = r"""
(args) => {
  const { containerSel, tileSel, imgSel, labelSel, promptText } = args;
  const needle = (promptText || '').toLowerCase();
  const bodyText = (document.body ? (document.body.innerText || '') : '').toLowerCase();

  const containers = Array.from(document.querySelectorAll(containerSel));
  // A container only counts as a captcha signal when it actually holds captcha
  // content. "main-div-container" may well be a generic page wrapper present on
  // every page - treating its mere existence as "captcha" would make every
  // normal login look like a challenge and stall the bot.
  let containerHasCaptcha = false;
  for (const c of containers) {
    if (c.querySelector(imgSel) || c.querySelector(tileSel) || c.querySelector(labelSel)) {
      containerHasCaptcha = true;
      break;
    }
    if ((c.innerText || '').toLowerCase().includes(needle)) {
      containerHasCaptcha = true;
      break;
    }
  }

  return {
    promptTextFound: needle ? bodyText.includes(needle) : false,
    containerCount: containers.length,
    containerHasCaptcha: containerHasCaptcha,
    captchaImgCount: document.querySelectorAll(imgSel).length,
    tileCount: document.querySelectorAll(tileSel).length,
    labelCount: document.querySelectorAll(labelSel).length,
  };
}
"""

_CAPTCHA_SCAN_JS = r"""
(args) => {
  const { tileSel, labelSel, imgSel, selectedClass } = args;

  const numFrom = (txt) => {
    const m = /number\s+(\d+)/i.exec(txt || '');
    return m ? m[1] : null;
  };

  const ancestorsVisible = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
      if (parseFloat(s.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    return true;
  };

  const inlineBlock = (el) => {
    const st = (el.getAttribute('style') || '').replace(/\s+/g, '');
    return st.includes('display:block');
  };

  const allTiles = Array.from(document.querySelectorAll(tileSel));
  const allLabels = Array.from(document.querySelectorAll(labelSel));
  const labelIndex = new Map(allLabels.map((el, i) => [el, i]));

  // Which label states this tile's number? Try, in order: a label inside the
  // tile, the nearest preceding label sibling, the sole label in the tile's row,
  // the tile's own text, then image attributes.
  const labelFor = (tile) => {
    const own = tile.querySelector(labelSel);
    if (own && numFrom(own.innerText)) {
      return { num: numFrom(own.innerText), src: 'descendant', idx: labelIndex.get(own) };
    }
    let sib = tile.previousElementSibling;
    while (sib) {
      if (sib.matches && sib.matches(labelSel) && numFrom(sib.innerText)) {
        return { num: numFrom(sib.innerText), src: 'prev-sibling', idx: labelIndex.get(sib) };
      }
      sib = sib.previousElementSibling;
    }
    const parent = tile.parentElement;
    if (parent) {
      const inRow = Array.from(parent.querySelectorAll(labelSel));
      if (inRow.length === 1 && numFrom(inRow[0].innerText)) {
        return { num: numFrom(inRow[0].innerText), src: 'row-single', idx: labelIndex.get(inRow[0]) };
      }
    }
    const selfNum = numFrom(tile.innerText);
    if (selfNum) return { num: selfNum, src: 'tile-text', idx: null };

    const img = tile.querySelector(imgSel) || tile.querySelector('img');
    if (img) {
      for (const attr of ['alt', 'title', 'data-number', 'data-value']) {
        const v = (img.getAttribute(attr) || '').trim();
        const n = numFrom(v) || (/^\d+$/.test(v) ? v : null);
        if (n) return { num: n, src: 'img-' + attr, idx: null };
      }
    }
    return { num: null, src: 'unresolved', idx: null };
  };

  const tiles = [];
  const claimed = new Set();
  allTiles.forEach((tile, domIndex) => {
    if (!inlineBlock(tile) || !ancestorsVisible(tile)) return;
    const img = tile.querySelector(imgSel) || tile.querySelector('img');
    const info = labelFor(tile);
    if (info.idx !== null && info.idx !== undefined) claimed.add(info.idx);
    tiles.push({
      domIndex: domIndex,
      id: tile.id || (img && img.id) || '',
      num: info.num,
      labelSource: info.src,
      hasImg: !!img,
      selected: img ? img.classList.contains(selectedClass) : false,
    });
  });

  // A prompt is a visible label that is not acting as some tile's own label.
  const prompts = [];
  const visibleLabels = [];
  allLabels.forEach((lab, i) => {
    if (!ancestorsVisible(lab)) return;
    const n = numFrom(lab.innerText);
    if (!n) return;
    const entry = { idx: i, num: n, text: (lab.innerText || '').trim().slice(0, 120) };
    visibleLabels.push(entry);
    if (!claimed.has(i) && !lab.closest(tileSel)) prompts.push(entry);
  });

  return {
    tiles: tiles,
    prompts: prompts,
    visibleLabels: visibleLabels,
    totalTiles: allTiles.length,
    totalLabels: allLabels.length,
  };
}
"""

# Candidate containers for a captcha / challenge widget, dumped verbatim when one
# is detected so the markup can be analysed without re-triggering a login.
CAPTCHA_DOM_SELECTORS = (
    # BLS's own captcha, first — these are the confirmed containers.
    "div.col-12.box-label",
    "div.col-4",
    "img.captcha-img",
    'input[name="SelectedImages"]',
    # Generic third-party challenges, in case BLS ever swaps provider.
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

        # CAPTCHA FIRST. The challenge renders instead of the account page, so
        # checking "am I logged in?" before handling it always fails and reports a
        # misleading reason. solve_captcha() is a no-op when nothing is on screen.
        logger.info("auth: checking for a CAPTCHA before the login check")
        if not await self.solve_captcha(page):
            # Unsolved (or markup we do not recognise) -> hand over to a human.
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
    # CAPTCHA
    # ------------------------------------------------------------------ #
    async def solve_captcha(self, page: Page) -> bool:
        """Solve the DOM-based BLS captcha.

        Returns True when there is no captcha, or when one was solved. Returns
        False when a captcha is present but could not be solved — the caller then
        falls back to pausing for a human.

        Per round: scan the DOM once, take the target number from the prompt,
        click the <img> inside every visible ``div.col-4`` whose label states that
        number, confirm each click registered (class ``img-selected``), then
        submit. A rejected selection reloads the grid with a new number, so every
        round re-scans from scratch. Up to ``CAPTCHA_MAX_ATTEMPTS`` rounds.
        """
        signals = await self._detect_captcha(page)
        logger.info(f"auth: captcha signals -> {self._signal_summary(signals)}")

        if not signals["present"]:
            logger.info("auth: no CAPTCHA detected")
            return True

        logger.info("auth: CAPTCHA detected, attempting to solve")
        self._log("CAPTCHA detected — solving", status="warn")
        await utils.screenshot(page, "captcha-before")
        # Always keep the markup of a real challenge: if the tile selectors turn
        # out not to match, this dump is what identifies the correct ones.
        await utils.dump_page_html(page, "captcha-detected")

        for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            scan = await self._captcha_scan(page)
            if scan is None:
                await utils.screenshot(page, f"captcha-scan-failed-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-scan-failed-attempt{attempt}")
                return False

            tiles = scan.get("tiles") or []
            target = self._captcha_target(scan)

            if not tiles:
                # A challenge is on screen but no tile matched the selectors.
                # Bail out loudly rather than silently reporting "solved".
                logger.error(
                    "auth: CAPTCHA is on screen but no tile matched "
                    f"{CAPTCHA_TILE_SELECTOR!r} with an inline display:block "
                    f"(DOM has {scan.get('totalTiles')} col-4 divs, "
                    f"{scan.get('totalLabels')} box-labels)"
                )
                logger.error(
                    "auth: the tile selectors need updating — see the DOM dump "
                    "in logs/dom/ for the real markup"
                )
                self._log("CAPTCHA tiles not found — selectors need updating", status="error")
                await utils.screenshot(page, f"captcha-no-tiles-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-no-tiles-attempt{attempt}")
                return False

            # --- attempt header: everything needed to debug from the log alone --
            logger.info("-" * 60)
            logger.info(f"CAPTCHA attempt {attempt} of {CAPTCHA_MAX_ATTEMPTS}")
            logger.info(f"  target number   : {target}")
            logger.info(
                f"  visible tiles   : {len(tiles)} "
                f"(of {scan.get('totalTiles')} col-4 divs in the DOM)"
            )
            logger.info(
                f"  visible labels  : {len(scan.get('visibleLabels') or [])} "
                f"(of {scan.get('totalLabels')} box-labels in the DOM)"
            )
            logger.info(
                "  tile numbers    : "
                + ", ".join(
                    f"{t.get('id') or '?'}={t.get('num')}" for t in tiles
                )
            )
            logger.info(
                "  label sources   : "
                + ", ".join(sorted({str(t.get("labelSource")) for t in tiles}))
            )
            self._log(
                f"captcha attempt {attempt}: target={target}, {len(tiles)} visible tiles",
                status="warn",
            )

            if target is None:
                logger.warning("auth: captcha present but no target number found")
                logger.warning(f"auth: visible labels seen: {scan.get('visibleLabels')}")
                await utils.screenshot(page, f"captcha-no-target-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-no-target-attempt{attempt}")
                return False

            unresolved = [t for t in tiles if not t.get("num")]
            if unresolved:
                # Cannot know these tiles' numbers, so cannot know whether they
                # should be clicked. Guessing risks a wrong answer and a lockout.
                logger.warning(
                    f"auth: {len(unresolved)} visible tile(s) have no readable "
                    "number — refusing to guess"
                )
                await utils.dump_page_html(page, f"captcha-unresolved-attempt{attempt}")
                return False

            matches = [t for t in tiles if t.get("num") == target]
            matched_ids = [t.get("id") or "?" for t in matches]
            logger.info(f"  matched tiles   : {len(matches)} -> {matched_ids}")

            if not matches:
                logger.warning(f"auth: no visible tile carries number {target}")
                await utils.screenshot(page, f"captcha-no-match-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-no-match-attempt{attempt}")
                return False

            clicked_ok, confirmations = await self._captcha_click_tiles(page, matches)

            logger.info("  click results   :")
            for entry in confirmations:
                logger.info(
                    f"      id={entry['id']:<14} number={entry['number']:<6} "
                    f"clicked={entry['clicked']}  "
                    f"img-selected={entry['selected_confirmed']}"
                )

            selected = await self._captcha_selected_images(page)
            logger.info(f"  SelectedImages  : {selected!r}")

            # Visual record of the selection actually made, before submitting.
            await utils.screenshot(page, f"captcha-attempt{attempt}-before-submit")

            if not clicked_ok:
                logger.error("  outcome         : ABORTED - not every tile could be clicked")
                self._log("captcha aborted — a tile could not be clicked", status="error")
                await utils.screenshot(page, f"captcha-click-failed-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-click-failed-attempt{attempt}")
                return False

            submitted = await self._click_captcha_submit(page)
            logger.info(f"  submit clicked  : {submitted}")
            if not submitted:
                logger.error("  outcome         : ABORTED - Submit button not found")
                self._log("captcha aborted — no Submit button", status="error")
                await utils.screenshot(page, f"captcha-no-submit-attempt{attempt}")
                await utils.dump_page_html(page, f"captcha-no-submit-attempt{attempt}")
                return False

            await self._settle(page)

            if not await self._captcha_present(page):
                logger.success(f"  outcome         : SOLVED on attempt {attempt}")
                logger.info("-" * 60)
                self._log(f"captcha solved (attempt {attempt})", status="ok")
                await utils.screenshot(page, "captcha-solved")
                return True

            logger.warning(
                f"  outcome         : REJECTED - captcha still present after "
                f"attempt {attempt}, grid reloaded"
            )
            logger.info("-" * 60)
            self._log(f"captcha attempt {attempt} rejected", status="warn")
            await utils.screenshot(page, f"captcha-attempt{attempt}-rejected")
            await utils.human_delay(self.config)

        logger.error(f"auth: captcha not solved after {CAPTCHA_MAX_ATTEMPTS} attempts")
        self._log(f"captcha unsolved after {CAPTCHA_MAX_ATTEMPTS} attempts", status="error")
        await utils.screenshot(page, "captcha-failed")
        await utils.dump_page_html(page, "captcha-failed")
        return False

    async def _captcha_present(self, page: Page) -> bool:
        """True while a captcha challenge is on screen."""
        return (await self._detect_captcha(page))["present"]

    async def _detect_captcha(self, page: Page) -> dict[str, Any]:
        """Decide whether a captcha is on screen, and report every signal.

        Deliberately broader than the tile scan: the page can be a challenge even
        when ``div.col-4`` tiles do not match, and in that case the bot must say
        so loudly rather than report "no captcha" and let the login check fail
        with a misleading error.

        Signals, strongest first:
          * prompt text "Please select all boxes with number" anywhere on the page
          * at least one visible tile found by the full scan
          * ``img.captcha-img`` elements present
          * ``div.main-div-container`` that *contains* captcha content

        The container is only counted when it holds captcha content, because a
        name like "main-div-container" may be a generic page wrapper — treating
        its bare presence as a captcha would misfire on every normal page.
        """
        signals: dict[str, Any] = {
            "promptTextFound": False,
            "containerCount": 0,
            "containerHasCaptcha": False,
            "captchaImgCount": 0,
            "tileCount": 0,
            "labelCount": 0,
            "visibleTiles": 0,
            "present": False,
        }

        try:
            found = await page.evaluate(
                _CAPTCHA_DETECT_JS,
                {
                    "containerSel": CAPTCHA_CONTAINER_SELECTOR,
                    "tileSel": CAPTCHA_TILE_SELECTOR,
                    "imgSel": CAPTCHA_IMG_SELECTOR,
                    "labelSel": CAPTCHA_BOX_LABEL_SELECTOR,
                    "promptText": CAPTCHA_PROMPT_TEXT,
                },
            )
            signals.update(found or {})
        except Exception as exc:
            logger.warning(f"auth: captcha detection failed: {exc}")
            return signals

        scan = await self._captcha_scan(page)
        if scan is not None:
            signals["visibleTiles"] = len(scan.get("tiles") or [])

        signals["present"] = bool(
            signals["promptTextFound"]
            or signals["visibleTiles"] > 0
            or signals["captchaImgCount"] > 0
            or signals["containerHasCaptcha"]
        )
        return signals

    @staticmethod
    def _signal_summary(signals: dict[str, Any]) -> str:
        return (
            f"prompt_text={signals['promptTextFound']} "
            f"visible_tiles={signals['visibleTiles']} "
            f"captcha_imgs={signals['captchaImgCount']} "
            f"container={signals['containerCount']}"
            f"(has_captcha={signals['containerHasCaptcha']}) "
            f"col4_total={signals['tileCount']} "
            f"box_labels={signals['labelCount']}"
        )

    async def _captcha_scan(self, page: Page) -> dict[str, Any] | None:
        """Run the single-pass DOM scan. None when it could not be evaluated."""
        try:
            return await page.evaluate(
                _CAPTCHA_SCAN_JS,
                {
                    "tileSel": CAPTCHA_TILE_SELECTOR,
                    "labelSel": CAPTCHA_BOX_LABEL_SELECTOR,
                    "imgSel": CAPTCHA_IMG_SELECTOR,
                    "selectedClass": CAPTCHA_SELECTED_CLASS,
                },
            )
        except Exception as exc:
            logger.warning(f"auth: captcha DOM scan failed: {exc}")
            return None

    def _captcha_target(self, scan: dict[str, Any]) -> str | None:
        """Pick the target number out of a scan result.

        Prompts and per-tile labels use the same ``div.col-12.box-label`` class
        and the same sentence, so "any visible box-label" can easily return a
        tile's own label instead of the prompt. Preference order:

        1. A visible label that no tile claimed as its own  -> the real prompt.
        2. If several unclaimed labels disagree, the first in document order.
        3. If every visible label is claimed by a tile, fall back to the first
           visible label and log loudly, because that reading may be wrong.
        """
        prompts = scan.get("prompts") or []
        if prompts:
            numbers = {entry["num"] for entry in prompts}
            if len(numbers) > 1:
                logger.warning(
                    f"auth: {len(numbers)} different prompt numbers visible "
                    f"({sorted(numbers)}) — using the first in document order"
                )
            logger.debug(f"auth: captcha prompt text {prompts[0]['text']!r}")
            return prompts[0]["num"]

        labels = scan.get("visibleLabels") or []
        if labels:
            logger.warning(
                "auth: every visible box-label is attached to a tile — no distinct "
                f"prompt found; falling back to the first label ({labels[0]['num']}). "
                "Verify the prompt markup if the captcha keeps failing."
            )
            return labels[0]["num"]
        return None

    async def _captcha_click_tiles(
        self, page: Page, matches: list[dict[str, Any]]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Click the <img> inside each matching tile, verifying each registered.

        Returns ``(all_clicked, confirmations)`` where each confirmation records
        the tile id, its number, whether the click was dispatched, and whether the
        ``img-selected`` class appeared afterwards.

        Tiles are addressed by their position in ``document.querySelectorAll`` so
        the random per-load ids are never used as selectors. Clicking lets the
        page's own ``Select()`` handler populate the hidden ``SelectedImages``
        field — the bot never writes that field itself.
        """
        confirmations: list[dict[str, Any]] = []

        def record(info: dict[str, Any], clicked: bool, confirmed: Any) -> None:
            confirmations.append(
                {
                    "id": str(info.get("id") or "?"),
                    "number": str(info.get("num")),
                    "clicked": clicked,
                    "selected_confirmed": confirmed,
                }
            )

        try:
            tiles = await page.query_selector_all(CAPTCHA_TILE_SELECTOR)
        except Exception as exc:
            logger.warning(f"auth: could not re-query captcha tiles: {exc}")
            for info in matches:
                record(info, False, "no-query")
            return False, confirmations

        all_ok = True
        for position, tile_info in enumerate(matches, start=1):
            index = tile_info.get("domIndex")
            if index is None or index >= len(tiles):
                logger.warning(f"auth: captcha tile index {index} is out of range")
                record(tile_info, False, "index-out-of-range")
                all_ok = False
                continue

            tile = tiles[index]
            target_el = await tile.query_selector(CAPTCHA_IMG_SELECTOR)
            if target_el is None:
                target_el = await tile.query_selector("img")
            if target_el is None:
                logger.warning(f"auth: captcha tile {index} has no <img> to click")
                record(tile_info, False, "no-img")
                all_ok = False
                continue

            try:
                await target_el.click()
                logger.debug(
                    f"auth: clicked captcha tile {position}/{len(matches)} "
                    f"(id={tile_info.get('id') or '?'}, number={tile_info.get('num')})"
                )
            except Exception as exc:
                logger.warning(f"auth: could not click captcha tile {index}: {exc}")
                record(tile_info, False, "click-error")
                all_ok = False
                continue

            await asyncio.sleep(CAPTCHA_CLICK_DELAY)

            # The page marks a chosen tile with class "img-selected"; if that did
            # not appear, the click did not register and submitting now would
            # send an incomplete answer.
            try:
                confirmed: Any = await target_el.evaluate(
                    "(el, cls) => el.classList.contains(cls)", CAPTCHA_SELECTED_CLASS
                )
            except Exception as exc:
                logger.debug(f"auth: could not read {CAPTCHA_SELECTED_CLASS}: {exc}")
                confirmed = "unknown"

            if confirmed is False:
                logger.warning(
                    f"auth: captcha tile {index} did not gain "
                    f".{CAPTCHA_SELECTED_CLASS} after the click"
                )
                all_ok = False

            record(tile_info, True, confirmed)

        return all_ok, confirmations

    async def _captcha_selected_images(self, page: Page) -> str | None:
        """Current value of the hidden SelectedImages field, for logging only."""
        try:
            field = await page.query_selector(CAPTCHA_SELECTED_INPUT_SELECTOR)
            if field is None:
                return None
            return await field.get_attribute("value") or ""
        except Exception:
            return None

    async def _click_captcha_submit(self, page: Page) -> bool:
        for selector in CAPTCHA_SUBMIT_SELECTORS:
            try:
                button = page.locator(selector).first
                await button.wait_for(state="visible", timeout=BUTTON_TIMEOUT_MS)
                await button.click(timeout=BUTTON_TIMEOUT_MS)
                logger.info(f"auth: captcha submitted via {selector}")
                return True
            except PlaywrightTimeout:
                continue
            except Exception as exc:
                logger.debug(f"auth: captcha submit via {selector} failed: {exc}")
        return await utils.click_by_text(page, ("submit",), config=self.config)

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
