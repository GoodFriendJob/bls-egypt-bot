"""Availability polling loop for Cairo and Alexandria."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeout

from modules import utils
from modules.auth import LoginError
from modules.booking import Booking
from modules.state import (
    STATUS_CHECKING,
    STATUS_ERROR,
    STATUS_NO_SLOTS,
    STATUS_PAUSED,
    STATUS_SLOT_FOUND,
)

# --------------------------------------------------------------------------- #
# Visa-type verification gate (CONFIRMED)
#
# After login the portal lands on /Global/bls/visatypeverification, which holds
# a SECOND captcha behind a "Verify Selection" button. Unlike the login captcha,
# this one renders its numbers as ordinary text, so it is solvable straight from
# the DOM - no vision call needed.
#
# The grid markup is injected when the button is clicked, so it is absent from a
# page dump taken on arrival. Selectors below are therefore best-effort and the
# modal is dumped on every encounter so they can be tightened.
# --------------------------------------------------------------------------- #
VERIFY_SELECTION_TEXTS = ("verify selection", "verify")
SUBMIT_SELECTION_TEXTS = ("submit selection",)
RELOAD_IMAGES_TEXTS = ("reload images", "reload", "refresh")
CONSENT_ACCEPT_TEXTS = (
    "i agree to provide my consent",
    "i have read and understood",
    "i agree",
    "accept",
)
# A rejected selection surfaces as a native JS alert, not as page text.
INVALID_ALERT_HINTS = ("invalid", "wrong", "try again", "not correct", "failed")
VERIFY_MAX_ATTEMPTS = 5

# One pass over the modal: the prompt number plus every clickable tile's number,
# both read as text. Mirrors the login solver's contrast trick for the prompt,
# since the same decoy-stacking pattern may be reused here.
_VERIFY_SCAN_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
      if (parseFloat(s.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return false;
    const top = document.elementFromPoint(cx, cy);
    return !!top && (top === el || el.contains(top));
  };

  const parseRGB = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x.trim()));
    return p.length >= 3 ? { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 } : null;
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const c = parseRGB(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const contrast = (el) => {
    const fg = parseRGB(getComputedStyle(el).color);
    if (!fg || fg.a < 0.1) return 0;
    const a = lum(fg), b = lum(bgOf(el));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };

  const all = Array.from(document.querySelectorAll('body *'));

  // Prompt: visible text containing "number NNN", highest contrast wins.
  const prompts = [];
  for (const el of all) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    const m = /number\s+(\d+)/i.exec(t);
    if (!m) continue;
    if (!visible(el)) continue;
    prompts.push({ num: m[1], text: t.slice(0, 120), contrast: Math.round(contrast(el) * 100) / 100 });
  }
  prompts.sort((a, b) => b.contrast - a.contrast);

  // Tiles: leaf-ish visible elements whose whole text is just a number.
  const tiles = [];
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (!/^\d{2,5}$/.test(t)) continue;
    if (el.querySelector('*') && el.children.length > 1) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    tiles.push({
      num: t,
      id: el.id || '',
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 80),
      hasOnclick: !!el.getAttribute('onclick') || !!el.onclick,
      selected: /select|active|checked/i.test((el.className || '').toString()),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height },
    });
  }
  tiles.sort((a, b) => (Math.abs(a.rect.y - b.rect.y) > 15 ? a.rect.y - b.rect.y : a.rect.x - b.rect.x));

  return { prompts, tiles };
}
"""

# Slot cells on the calendar, used as a structural signal alongside page text.
SLOT_PROBES = (
    "td.available",
    "td[class*='available']",
    "button[class*='available']",
    "a[class*='available']",
    ".slot:not(.disabled)",
    "[data-slot]:not([disabled])",
)


class Monitor:
    """Polls each configured location and hands detected slots to Booking."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        state: Any,
        auth: Any,
        notifier: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.auth = auth
        self.notifier = notifier
        self.booking = Booking(config, state=state, notifier=notifier)

        self.locations = [str(loc).lower() for loc in (config.get("bls", {}) or {}).get("locations", [])]
        self.poll_interval = float(config.get("poll_interval", 90))
        retry = config.get("retry", {}) or {}
        self.max_attempts = int(retry.get("max_attempts", 3))
        self.page_error_wait = float(retry.get("page_error_wait", 30))
        self.network_error_wait = float(retry.get("network_error_wait", 60))

        self._consecutive_errors = 0
        self._last_pause_reason: str | None = None

        # Native JS dialogs raised by the verification captcha.
        self._dialogs: list[str] = []
        self._dialog_hooked: set[int] = set()

        # The appointment URL is still a guess; the live portal answers it with
        # ERR_HTTP_RESPONSE_CODE_FAILURE. Flip this to True (or set
        # bls.paths.appointment_confirmed) once the real route is known.
        bls_cfg = config.get("bls", {}) or {}
        self.appointment_path_confirmed = bool(
            (bls_cfg.get("paths") or {}).get("appointment_confirmed", False)
        )

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Poll until a stop is requested."""
        logger.info(f"monitor: watching {self.locations} every {self.poll_interval}s")
        while not self.state.stop_requested:
            if not self.state.running:
                await self._idle()
                continue
            if self.state.paused:
                await self._await_manual_resume()
                continue

            try:
                await self.run_once()
                self._consecutive_errors = 0
            except LoginError as exc:
                # ensure_logged_in() already alerted and paused the bot.
                logger.error(f"monitor: authentication blocked: {exc}")
                await utils.interruptible_sleep(self.page_error_wait, self.state)
                continue
            except Exception as exc:
                await self._handle_cycle_error(exc)
                continue

            if self.state.stop_requested:
                break
            await utils.interruptible_sleep(self.poll_interval, self.state)

        logger.info("monitor: loop ended")

    async def run_once(self) -> None:
        """One full pass: log in, clear the verification gate, check each location."""
        page = await self.auth.ensure_logged_in()
        self.state.mark_cycle()

        if not await self.ensure_visa_type_verified(page):
            logger.error("monitor: visa-type verification not cleared — skipping cycle")
            self.state.log_event("bot", "visa-type verification failed", status="error")
            return

        # TODO(next step): the appointment flow itself is not built yet. Survey
        # whatever page the verification lands on so the real booking route can
        # be written from observed markup instead of guessed URLs.
        await self._survey_page(page, "post-verification")

        if not self.appointment_path_confirmed:
            # Navigating to the placeholder URL just produces
            # ERR_HTTP_RESPONSE_CODE_FAILURE three times per location and fires
            # a Telegram error alert for each. Stop here until the real route is
            # read off the survey above.
            logger.warning(
                "monitor: appointment route not confirmed yet — stopping after the "
                "survey. Set bls.paths.appointment (and clear "
                "appointment_path_confirmed) once the real URL is known."
            )
            self.state.log_event(
                "bot", "logged in and verified; appointment route still unknown", status="warn"
            )
            return

        for location in self.locations:
            if self.state.stop_requested or not self.state.running:
                return
            await self._check_location(page, location)
            await utils.human_delay(self.config)

    # ------------------------------------------------------------------ #
    # Visa-type verification gate
    # ------------------------------------------------------------------ #
    async def ensure_visa_type_verified(self, page: Any) -> bool:
        """Clear the post-login verification page.

        Flow: open /Global/bls/visatypeverification -> dismiss consent modals ->
        click "Verify Selection" -> solve the text-based captcha in the modal ->
        "Submit Selection" -> submit the form.
        """
        # MUST be installed before anything can trigger an alert: with no handler
        # Playwright auto-dismisses dialogs, so an "Invalid selection" alert
        # would vanish silently and every retry would look identical.
        self._install_dialog_handler(page)

        url = self.auth.path("dashboard")
        logger.info(f"monitor: opening visa-type verification at {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self._settle(page)
        except Exception as exc:
            logger.error(f"monitor: could not open the verification page: {exc}")
            return False

        await self._accept_consents(page)

        state = await self._gate_state(page)
        if state.get("submit") or state.get("verified"):
            logger.info("monitor: verification already satisfied")
            return await self._submit_verified_form(page)

        # STEP 1 — open the captcha popup.
        if not await utils.click_by_text(page, VERIFY_SELECTION_TEXTS, config=self.config):
            logger.warning("monitor: no 'Verify Selection' button found")
            await utils.screenshot(page, "verify-no-button")
            await utils.dump_page_html(page, "verify-no-button")
            return False
        logger.info("monitor: clicked 'Verify Selection' — waiting for the popup")
        await self._settle(page)
        await utils.human_delay(self.config)

        for attempt in range(1, VERIFY_MAX_ATTEMPTS + 1):
            logger.info(f"monitor: verification attempt {attempt}/{VERIFY_MAX_ATTEMPTS}")

            # The grid is injected on click, so capture it every attempt — these
            # artifacts are what the selectors get tightened against.
            await utils.screenshot(page, f"verify-modal-attempt{attempt}")
            await utils.dump_page_html(page, f"verify-modal-attempt{attempt}")

            # STEP 2 — solve and submit the selection.
            self._dialogs.clear()
            submitted = await self._solve_text_captcha(page, attempt)

            # STEP 3 — a rejected answer arrives as a JS alert, so give the
            # dialog handler a moment to fire before judging the outcome.
            await asyncio.sleep(1.0)
            alerts = list(self._dialogs)
            if alerts:
                logger.info(f"  dialog(s)       : {alerts}")

            rejected = any(
                any(h in msg.lower() for h in INVALID_ALERT_HINTS) for msg in alerts
            )

            if submitted and not rejected:
                await self._settle(page)
                state = await self._gate_state(page)
                if state.get("submit") or state.get("verified"):
                    logger.success("monitor: verification captcha passed")
                    self.state.log_event("bot", "visa-type verification cleared", status="ok")
                    return await self._submit_verified_form(page)
                logger.warning("monitor: submitted but the gate is still closed")
            elif rejected:
                logger.warning(f"  outcome         : REJECTED — {alerts}")

            if attempt < VERIFY_MAX_ATTEMPTS:
                await self._reload_captcha_images(page)
                await utils.human_delay(self.config)

        logger.error("monitor: verification not cleared after all attempts")
        await utils.screenshot(page, "verify-failed")
        await utils.dump_page_html(page, "verify-failed")
        await self.notifier.manual_required(
            "Could not clear the visa-type verification page automatically. "
            "Complete it in the browser, then send /resume."
        )
        self.state.set_manual_pause("visa-type verification")
        await self.notifier.wait_for_resume()
        self.state.clear_manual_pause()

        state = await self._gate_state(page)
        if state.get("submit") or state.get("verified"):
            return await self._submit_verified_form(page)
        return False

    async def _solve_text_captcha(self, page: Any, attempt: int) -> bool:
        """Solve the modal captcha by reading tile numbers straight from the DOM."""
        try:
            scan = await page.evaluate(_VERIFY_SCAN_JS)
        except Exception as exc:
            logger.warning(f"monitor: verification scan failed: {exc}")
            return False

        prompts = scan.get("prompts") or []
        tiles = scan.get("tiles") or []
        if not prompts:
            logger.warning("monitor: no 'number NNN' prompt visible in the modal")
            return False

        target = prompts[0]["num"]
        logger.info(
            f"  verify target   : {target} (contrast={prompts[0]['contrast']}, "
            f"{len(prompts)} prompt candidate(s))"
        )
        logger.info(
            "  verify tiles    : "
            + ", ".join(f"{i}:{t['num']}" for i, t in enumerate(tiles, 1))
        )

        matches = [t for t in tiles if t["num"] == target]
        logger.info(f"  verify matches  : {len(matches)} tile(s) showing {target}")
        if not matches:
            logger.warning(f"monitor: no tile shows {target}")
            return False

        # Click by geometry — ids here are not guaranteed stable or present.
        for index, tile in enumerate(matches, 1):
            cx = tile["rect"]["x"] + tile["rect"]["w"] / 2
            cy = tile["rect"]["y"] + tile["rect"]["h"] / 2
            try:
                await page.mouse.click(cx, cy)
                logger.debug(f"  clicked verify tile {index}/{len(matches)} ({tile['num']})")
            except Exception as exc:
                logger.warning(f"monitor: could not click verify tile {tile['num']}: {exc}")
                return False
            await asyncio.sleep(0.5)

        await utils.screenshot(page, f"verify-selected-attempt{attempt}")

        if not await utils.click_by_text(page, SUBMIT_SELECTION_TEXTS, config=self.config):
            logger.warning("monitor: no 'Submit Selection' button found")
            return False
        logger.info("  verify submit   : clicked")
        await self._settle(page)
        return True

    async def _accept_consents(self, page: Any) -> None:
        """Dismiss the biometric / data-protection consent modals if shown."""
        for text in CONSENT_ACCEPT_TEXTS:
            if await utils.click_by_text(page, (text,), config=self.config):
                logger.info(f"monitor: accepted consent — {text!r}")
                await self._settle(page)

    def _install_dialog_handler(self, page: Any) -> None:
        """Capture and accept native JS dialogs.

        Playwright auto-dismisses dialogs when nothing is listening, so an
        "Invalid selection" alert would disappear without trace and a failed
        attempt would be indistinguishable from a successful one. Registering a
        handler both accepts the dialog and records its message.
        """
        if id(page) in self._dialog_hooked:
            return

        def _on_dialog(dialog: Any) -> None:
            try:
                message = dialog.message or ""
            except Exception:
                message = ""
            self._dialogs.append(message)
            logger.info(f"monitor: JS dialog accepted — {message!r}")
            asyncio.ensure_future(self._accept_dialog(dialog))

        page.on("dialog", _on_dialog)
        self._dialog_hooked.add(id(page))
        logger.debug("monitor: dialog handler installed")

    @staticmethod
    async def _accept_dialog(dialog: Any) -> None:
        try:
            await dialog.accept()
        except Exception as exc:
            logger.debug(f"monitor: could not accept dialog: {exc}")

    async def _gate_state(self, page: Any) -> dict[str, Any]:
        """Visibility of the Verified / Submit buttons that follow a pass."""
        try:
            state = await page.evaluate(
                """
                () => {
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                           s.visibility !== 'hidden';
                  };
                  const byText = (re) => Array.from(
                      document.querySelectorAll('button, input[type=submit]'))
                    .filter(vis)
                    .filter((b) => re.test((b.innerText || b.value || '').trim()));
                  return {
                    verified: vis(document.getElementById('btnVerified'))
                              || byText(/^verified$/i).length > 0,
                    submit: vis(document.getElementById('btnSubmit'))
                            || byText(/^submit$/i).length > 0,
                    modalOpen: Array.from(
                        document.querySelectorAll('.modal, [role="dialog"]'))
                      .some(vis),
                    url: location.href,
                  };
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: gate probe failed: {exc}")
            return {}
        logger.info(f"  gate state      : {state}")
        return state

    async def _submit_verified_form(self, page: Any) -> bool:
        """Click the Submit that appears once verification has passed."""
        clicked = await utils.click_by_text(page, ("submit",), config=self.config)
        if not clicked:
            try:
                await page.evaluate(
                    "() => { const b = document.getElementById('btnSubmit');"
                    " if (b) { b.style.display=''; b.click(); } }"
                )
                clicked = True
                logger.info("monitor: submitted via #btnSubmit")
            except Exception as exc:
                logger.warning(f"monitor: could not submit the form: {exc}")
        else:
            logger.info("monitor: submitted the verification form")
        await self._settle(page)
        logger.info(f"monitor: URL after verification submit — {page.url}")
        return True

    async def _reload_captcha_images(self, page: Any) -> None:
        """Ask for a fresh grid after a rejected selection."""
        if await utils.click_by_text(page, RELOAD_IMAGES_TEXTS, config=self.config):
            logger.info("monitor: requested a fresh verification grid")
            await self._settle(page)
            return
        # No reload control — reopening the popup has the same effect.
        if await utils.click_by_text(page, VERIFY_SELECTION_TEXTS, config=self.config):
            logger.info("monitor: reopened the verification popup for a fresh grid")
            await self._settle(page)

    async def _survey_page(self, page: Any, label: str) -> None:
        """Log what is on the current page so the next step can be built."""
        try:
            info = await page.evaluate(
                """
                () => {
                  const txt = (el) => (el.innerText || el.value || '').trim().slice(0, 60);
                  const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                           s.visibility !== 'hidden';
                  };
                  const pick = (sel) => Array.from(document.querySelectorAll(sel))
                    .filter(vis).slice(0, 25).map((el) => ({
                      t: txt(el), id: el.id || '', name: el.name || '',
                      href: el.getAttribute('href') || '',
                    }));
                  return {
                    url: location.href,
                    title: document.title,
                    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4'))
                      .filter(vis).slice(0, 10).map(txt),
                    buttons: pick('button, input[type=submit], input[type=button]'),
                    links: pick('a[href]'),
                    selects: Array.from(document.querySelectorAll('select'))
                      .filter(vis).map((s) => ({
                        id: s.id, name: s.name,
                        options: Array.from(s.options).slice(0, 15).map((o) => o.text.trim()),
                      })),
                    inputs: pick('input:not([type=hidden])'),
                  };
                }
                """
            )
        except Exception as exc:
            logger.warning(f"monitor: page survey failed: {exc}")
            return

        logger.info("=" * 60)
        logger.info(f"PAGE SURVEY ({label})")
        logger.info(f"  url      : {info.get('url')}")
        logger.info(f"  title    : {info.get('title')}")
        logger.info(f"  headings : {info.get('headings')}")
        logger.info(f"  selects  : {info.get('selects')}")
        logger.info(f"  buttons  : {[b['t'] for b in info.get('buttons', []) if b['t']]}")
        logger.info(f"  inputs   : {[(i['name'] or i['id']) for i in info.get('inputs', [])]}")
        logger.info(f"  links    : {[l['href'] for l in info.get('links', []) if l['href']][:20]}")
        logger.info("=" * 60)
        await utils.screenshot(page, label)
        await utils.dump_page_html(page, label)

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            logger.debug("monitor: networkidle timed out")
        await utils.human_delay(self.config, scale=0.5)

    async def close(self) -> None:
        await self.auth.stop()

    # ------------------------------------------------------------------ #
    # Per-location check
    # ------------------------------------------------------------------ #
    async def _check_location(self, page: Any, location: str) -> None:
        self.state.set_location_status(location, STATUS_CHECKING, "checking availability")

        try:
            available, detail = await utils.with_retries(
                lambda: self._probe_location(page, location),
                attempts=self.max_attempts,
                delay=5,
                description=f"availability check for {location}",
            )
        except PlaywrightTimeout as exc:
            await self._location_error(page, location, f"timeout: {exc}", self.page_error_wait)
            return
        except Exception as exc:
            await self._location_error(page, location, str(exc), self.page_error_wait)
            return

        if not available:
            self.state.set_location_status(location, STATUS_NO_SLOTS, detail, checked=True)
            logger.info(f"monitor[{location}]: no slots ({detail})")
            self.state.log_event(location, f"no slots — {detail}", status="info")
            return

        # Slot found.
        self.state.set_location_status(location, STATUS_SLOT_FOUND, detail, checked=True)
        self.state.log_event(location, f"SLOT FOUND — {detail}", status="ok")
        logger.success(f"monitor[{location}]: slot found! {detail}")
        await self.notifier.slot_found(location, detail)

        await self.booking.book(page, location, slot_hint=detail)

    async def _probe_location(self, page: Any, location: str) -> tuple[bool, str]:
        """Navigate to the appointment page and decide whether slots exist.

        TODO(unconfirmed — needs live portal inspection): per CLAUDE.md the
        appointment page structure is TBD. Detection currently combines
        configured text phrases with generic "available" cell probes. Once the
        real markup is captured, replace this with an exact check (and record the
        availability endpoint if the portal fetches slots over XHR — polling that
        directly would be far more reliable than scraping the calendar).
        """
        url = self.auth.path("appointment")
        logger.debug(f"monitor[{location}]: navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            logger.debug(f"monitor[{location}]: networkidle timed out")
        await utils.human_delay(self.config, scale=0.5)

        # An unexpected redirect usually means the session died.
        current = (page.url or "").lower()
        if any(hint in current for hint in ("login", "signin")):
            raise RuntimeError("redirected to the login page — session expired")

        detection = self.config.get("detection", {}) or {}
        text = await utils.page_text(page)

        unavailable = utils.contains_any(text, detection.get("unavailable_phrases", []))
        if unavailable:
            return False, f"matched {unavailable!r}"

        for probe in SLOT_PROBES:
            cells = await utils.visible_elements(page, probe)
            if cells:
                return True, f"{len(cells)} slot cell(s) via {probe}"

        positive = utils.contains_any(text, detection.get("available_phrases", []))
        if positive:
            return True, f"matched {positive!r}"

        # Neither signal fired — treat as no slots but keep the evidence.
        await utils.screenshot(page, f"availability-unclear-{location}")
        return False, "no availability signal on the page"

    # ------------------------------------------------------------------ #
    # Error handling
    # ------------------------------------------------------------------ #
    async def _location_error(
        self, page: Any, location: str, message: str, wait: float
    ) -> None:
        logger.error(f"monitor[{location}]: {message}")
        self.state.set_location_status(location, STATUS_ERROR, message, checked=True)
        self.state.log_event(location, f"error: {message}", status="error")
        shot = await utils.screenshot(page, f"monitor-error-{location}")
        await self.notifier.error(
            f"{location.title()} check failed: {message}",
            retry_info=f"retrying in {int(wait)}s",
        )
        if shot:
            await self.notifier.send_photo(shot, f"Monitor error — {location.title()}")
        await utils.interruptible_sleep(wait, self.state)

    async def _handle_cycle_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        message = f"{type(exc).__name__}: {exc}"
        logger.exception(f"monitor: cycle failed ({self._consecutive_errors}): {message}")
        self.state.log_event("bot", f"cycle error: {message}", status="error")

        is_network = any(
            hint in message.lower()
            for hint in ("net::", "econn", "dns", "timeout", "socket", "connection")
        )
        wait = self.network_error_wait if is_network else self.page_error_wait

        await self.notifier.error(message, retry_info=f"retrying in {int(wait)}s")

        # Repeated failures usually mean the session is gone — force a re-login.
        if self._consecutive_errors >= self.max_attempts:
            logger.warning("monitor: too many consecutive errors — forcing a fresh login")
            self._consecutive_errors = 0
            try:
                await self.auth.refresh_session()
            except Exception as refresh_exc:
                logger.error(f"monitor: forced re-login failed: {refresh_exc}")

        await utils.interruptible_sleep(wait, self.state)

    # ------------------------------------------------------------------ #
    # Idle / paused states
    # ------------------------------------------------------------------ #
    async def _idle(self) -> None:
        """Monitoring is switched off (dashboard Stop) — wait to be restarted."""
        for location in self.locations:
            self.state.set_location_status(location, STATUS_PAUSED, "monitoring stopped")
        await utils.interruptible_sleep(2, self.state, step=0.5)

    async def _await_manual_resume(self) -> None:
        """Hold until the user sends /resume (or presses Start on the dashboard).

        Must always consume real time before returning. When Telegram is not
        configured ``wait_for_resume`` returns False instantly, and the caller
        re-enters this method immediately — without the sleep below that becomes
        a 100% CPU busy-loop that also floods the log.
        """
        reason = self.state.pause_reason or "manual step"
        if reason != self._last_pause_reason:
            logger.info(f"monitor: paused — {reason}")
            self._last_pause_reason = reason

        resumed = await self.notifier.wait_for_resume(timeout=60)
        if resumed or not self.state.paused:
            self._last_pause_reason = None
            return

        logger.debug("monitor: still waiting for /resume")
        # Guarantees forward progress even when wait_for_resume cannot block.
        await utils.interruptible_sleep(15, self.state)
