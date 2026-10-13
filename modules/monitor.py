"""Availability polling loop for Cairo and Alexandria."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeout

from modules import otp, utils
from modules.auth import LoginError, PortalUnreachableError
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
# CONFIRMED: the challenge is a Kendo UI window wrapping an IFRAME:
#   <iframe class="k-content-frame" src="/Global/NewCaptcha/GenerateCaptcha">
# So the grid is neither in the parent document nor in a window.open() popup —
# it is a separate frame, and every query and click must target that frame.
CAPTCHA_FRAME_HINT = "generatecaptcha"
CAPTCHA_FRAME_TIMEOUT = 30.0

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
INVALID_ALERT_HINTS = ("invalid", "wrong", "not correct", "failed")
# These mean the CHALLENGE itself is dead, not that the answer was wrong.
# Retrying only produces more 403s against an expired token — the journey has
# to restart so a fresh challenge is issued.
EXPIRED_ALERT_HINTS = ("session has been expired", "session expired", "expired")
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
      el: el,
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

  // Tag each tile so Python can grab the exact element by selector and click it
  // directly. Clicking by page coordinates is wrong inside an iframe: frame
  // coordinates and page coordinates are different spaces.
  document.querySelectorAll('[data-bot-tile]').forEach(
    (el) => el.removeAttribute('data-bot-tile'));
  tiles.forEach((t, i) => { t.el.setAttribute('data-bot-tile', String(i + 1)); });

  return { prompts, tiles: tiles.map((t) => ({
    num: t.num, id: t.id, tag: t.tag, cls: t.cls,
    hasOnclick: t.hasOnclick, selected: t.selected, rect: t.rect,
  })) };
}
"""

# Where in the journey a URL puts us. Ordered: the first match wins, so the more
# specific verification path is tested before the generic /bls/ one.
PAGE_STAGES = (
    ("/account/login", "login"),
    ("/newcaptcha/logincaptcha", "login-captcha"),
    ("/newcaptcha/generatecaptcha", "verify-captcha"),
    ("/bls/visatypeverification", "verification"),
    ("/bls/visatype", "appointment-form"),
    ("/blsappointment/", "appointment"),
)


def classify_page(url: str) -> str:
    """Name the stage a URL belongs to, for navigation tracking."""
    low = (url or "").lower()
    if low.startswith("chrome-error") or "chromewebdata" in low:
        return "browser-error"
    for fragment, stage in PAGE_STAGES:
        if fragment in low:
            return stage
    return "other"


def _frame_gone(frame: Any) -> bool:
    """True once the captcha frame has been detached (the success signal).

    A Frame has is_detached(); only a Page has is_closed(). Mixing them up
    raises AttributeError mid-flow, so the check lives here.
    """
    if frame is None:
        return True
    try:
        if hasattr(frame, "is_detached"):
            return bool(frame.is_detached())
        if hasattr(frame, "is_closed"):
            return bool(frame.is_closed())
    except Exception:
        return True
    return False


# --------------------------------------------------------------------------- #
# "Book New Appointment" form  (/Global/bls/visatype)  — CONFIRMED markup
#
# Two traps here:
#
# 1. The controls are Kendo UI DropDownLists, NOT <select> elements. The real
#    field is a hidden <input data-role="dropdownlist"> wrapped in a
#    <span class="k-widget k-dropdown">, so querying "select" finds nothing and
#    setting .value on the input does nothing either. Values must go through the
#    widget: $(el).data('kendoDropDownList').value(id) then trigger('change').
#
# 2. Every field is duplicated as decoys — AppointmentCategoryId1/2/3/5,
#    Location2/3/5, VisaType2/5, VisaSubType2/5, AppointmentFor1/2/3 — hidden by
#    the same random-class obfuscation as the login honeypots. Only one of each
#    is genuinely visible, so each must be picked by visibility, never by id.
#
# The dropdowns cascade: onLocationChangeN() refilters the Visa Type source and
# onVisaTypeChangeN() refilters Visa Sub Type, so 'change' must actually fire
# and the next field must be given time to repopulate.
# --------------------------------------------------------------------------- #
APPOINTMENT_FIELD_ORDER = (
    ("Appointment Category", "appointment_category"),
    ("Location", "location"),
    ("Visa Type", "visa_type"),
    ("Visa Sub Type", "visa_sub_type"),
)

_KENDO_SET_JS = r"""
(args) => {
  const { label, wanted } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
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

  const target = norm(label);
  const inputs = Array.from(document.querySelectorAll('input[data-role="dropdownlist"]'));
  const visible = [];
  for (const inp of inputs) {
    const lab = inp.id ? document.querySelector('label[for="' + inp.id + '"]') : null;
    if (!lab) continue;
    if (!norm(lab.innerText).startsWith(target)) continue;
    // The <input> itself is display:none by design, so judge the Kendo wrapper.
    // The decoys are hidden on their enclosing div.mb-3 (that is where the
    // random-class CSS applies display:none), so check that container too.
    const widget = inp.closest('span.k-widget') || inp.parentElement;
    if (!vis(widget)) continue;
    const box = inp.closest('div.mb-3');
    if (box && !vis(box)) continue;
    visible.push(inp);
  }
  if (!visible.length) {
    return { ok: false, reason: 'no visible widget for this label',
             candidates: inputs.length };
  }

  const inp = visible[0];
  if (!window.jQuery) return { ok: false, reason: 'jQuery missing', id: inp.id };
  const dd = window.jQuery('#' + inp.id).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'kendo widget not initialised', id: inp.id };

  const data = dd.dataSource.data() || [];
  const textField = (dd.options && dd.options.dataTextField) || 'Name';
  const valueField = (dd.options && dd.options.dataValueField) || 'Id';
  const texts = [];
  for (let i = 0; i < data.length; i++) texts.push(String(data[i][textField] || ''));

  const isPlaceholder = (t) => !t || /^-+\s*select/i.test(t);
  let idx = -1;
  if (wanted) {
    idx = texts.findIndex((t) => norm(t) === norm(wanted));
    if (idx < 0) idx = texts.findIndex((t) => norm(t).includes(norm(wanted)));
  }
  if (idx < 0) idx = texts.findIndex((t) => !isPlaceholder(t));
  if (idx < 0) {
    return { ok: false, reason: 'no selectable option', id: inp.id,
             options: texts, count: texts.length };
  }

  dd.value(data[idx][valueField]);
  // value() alone does not fire change, and the cascade depends on it.
  dd.trigger('change');
  return { ok: true, id: inp.id, chosen: texts[idx], options: texts,
           count: texts.length, visibleWidgets: visible.length };
}
"""

_RADIO_SET_JS = r"""
(wanted) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden') return false;
      n = n.parentElement;
    }
    return true;
  };
  const target = norm(wanted);
  for (const r of document.querySelectorAll('input[type="radio"]')) {
    if (!vis(r)) continue;
    const own = norm(r.value);
    const lab = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
    const text = norm((lab && lab.innerText) || '');
    if (own === target || text === target || text.startsWith(target)) {
      r.click();
      return { ok: true, name: r.name, id: r.id, value: r.value };
    }
  }
  return { ok: false };
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
        # After the portal explicitly says there are no slots, polling every 90s
        # is pointless and is exactly the behaviour that got the IP throttled.
        # Slots are released in batches, so wait much longer before retrying.
        self.no_slots_wait_minutes = float(config.get("no_slots_wait_minutes", 5))
        # Each combination costs a verification captcha; cap the per-cycle fan-out.
        self.max_combinations = int(config.get("max_combinations_per_cycle", 12))
        retry = config.get("retry", {}) or {}
        self.max_attempts = int(retry.get("max_attempts", 3))
        self.page_error_wait = float(retry.get("page_error_wait", 30))
        self.network_error_wait = float(retry.get("network_error_wait", 60))

        self._consecutive_errors = 0
        self._last_pause_reason: str | None = None

        # Unattended recovery state.
        self.block_cooldown_minutes = float(retry.get("block_cooldown_minutes", 60))
        # Consecutive login failures before this stops being treated as
        # transient and becomes a critical, manager-facing pause.
        self.critical_login_failures = int(retry.get("critical_login_failures", 8))
        self._login_failures = 0
        self._block_alerted = False
        self._last_form_result: str | None = None
        self._option_unavailable: str | None = None
        self._last_visatype_status: int | None = None
        self._form_hooked: set[int] = set()
        # Live page-stage tracking, fed by the navigation watcher.
        self._nav_hooked: set[int] = set()
        self._page_stage: str | None = None
        self._needs_restart = False
        self._needs_reverify = False
        # Rotates through bls.locations so consecutive rounds ask about
        # different centres rather than hammering the same one.
        self._location_index = 0

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

            # Honour a recorded block before touching the portal at all.
            remaining = utils.block_cooldown_remaining(self.block_cooldown_minutes)
            if remaining > 0:
                logger.warning(
                    f"monitor: portal blocked — waiting {remaining:.0f} more "
                    "minutes before the next login attempt"
                )
                for location in self.locations:
                    self.state.set_location_status(
                        location, STATUS_PAUSED, f"blocked — {remaining:.0f} min left"
                    )
                await utils.interruptible_sleep(remaining * 60, self.state)
                continue

            try:
                await self.run_once()
                self._consecutive_errors = 0
                self._login_failures = 0
            except PortalUnreachableError as exc:
                await self._handle_portal_block(exc)
                continue
            except LoginError as exc:
                await self._handle_login_failure(exc)
                continue
            except Exception as exc:
                await self._handle_cycle_error(exc)
                continue

            if self.state.stop_requested:
                break

            # Pace the next cycle by what the portal actually told us.
            if self._last_form_result == "no_slots":
                wait = self.no_slots_wait_minutes * 60
                logger.info(
                    f"monitor: no slots — next full check in "
                    f"{self.no_slots_wait_minutes:.0f} min"
                )
                for location in self.locations:
                    self.state.set_location_status(
                        location, STATUS_NO_SLOTS,
                        f"no slots — rechecking in {self.no_slots_wait_minutes:.0f} min",
                        checked=True,
                    )
            else:
                wait = self.poll_interval
            self._last_form_result = None
            await utils.interruptible_sleep(wait, self.state)

        logger.info("monitor: loop ended")

    async def run_once(self) -> None:
        """One full pass: log in, clear the verification gate, check each location."""
        page = await self.auth.ensure_logged_in()
        self.state.mark_cycle()

        self.install_navigation_watch(page)

        if not await self.ensure_visa_type_verified(page):
            logger.error("monitor: visa-type verification not cleared — skipping cycle")
            self.state.log_event("bot", "visa-type verification failed", status="error")
            return

        # Verification lands on /Global/bls/visatype?data=... — the appointment
        # form. Its URL carries a one-time token, so it can only be reached by
        # clicking through; navigating to a fixed URL will never work.
        # CONFIRMED: dismissing "No Appointments Available" makes the server
        # redirect to /Global/bls/VisaTypeVerification. So after a no-slots
        # answer the portal has already put us back at the gate — re-solve it
        # and ask about the NEXT location, rather than ending the cycle and
        # waiting. Only once every location has answered "no slots" is the long
        # wait justified.
        combos = self.build_combinations()
        logger.info(f"monitor: sweeping {len(combos)} option combination(s) this cycle")
        checked: list[str] = []
        for index, combo in enumerate(combos):
            if self.state.stop_requested or not self.state.running:
                return
            location = combo["location_key"]
            logger.info(
                f"monitor: [{index + 1}/{len(combos)}] {self.describe_combo(combo)}"
            )

            if index > 0:
                # Back at the gate after the previous location's modal; a fresh
                # challenge is required before the form can be reached again.
                # Back at the gate after the previous answer; a fresh
                # challenge is needed before the form is reachable again.
                self._needs_reverify = False
                if not await self.ensure_visa_type_verified(page):
                    logger.error("monitor: could not re-clear the gate — ending cycle")
                    return

            self._last_form_result = None
            if not await self.complete_visatype_form(page, combo):
                logger.error(f"monitor: appointment form failed for '{location}'")
                self.state.log_event(
                    "bot", f"appointment form failed ({location})", status="error"
                )
                await self._survey_page(page, f"visatype-incomplete-{location}")
                return

            if self._last_form_result == "option_unavailable":
                logger.info("monitor: combination not offered — moving to the next")
                continue

            checked.append(self.describe_combo(combo))
            if self._last_form_result != "no_slots":
                # Slots, a validation problem or an unknown modal — stop the
                # sweep so the outcome is handled rather than overwritten.
                logger.info(
                    f"monitor: {self.describe_combo(combo)} returned "
                    f"{self._last_form_result!r} — not continuing the sweep"
                )
                return

            logger.info(f"monitor: no slots for {self.describe_combo(combo)}")
            self.state.set_location_status(
                location, STATUS_NO_SLOTS, "no slots", checked=True
            )

        logger.info(
            f"monitor: no slots for any of the {len(checked)} combination(s) "
            "— cycle complete"
        )
        self._last_form_result = "no_slots"
        return
        # NOTE: the old _check_location() sweep used to run here. It navigated
        # to the guessed bls.paths.appointment URL, which does not exist — the
        # appointment form is only reachable by clicking through with a
        # one-time token. Availability is now answered by the form submit above.

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

        # DO NOT re-navigate when already here. The login flow lands on this page
        # as the result of a POST; re-requesting it with a plain GET is rejected
        # and the portal bounces to /Global/Account/LogIn, destroying the session
        # that was just established.
        url = self.auth.path("dashboard")
        current = (page.url or "").lower()
        if "visatypeverification" in current:
            logger.info(f"monitor: already on the verification page — {page.url}")
        else:
            logger.info(f"monitor: navigating to verification at {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await self._settle(page)
            except Exception as exc:
                logger.error(f"monitor: could not open the verification page: {exc}")
                return False

            if "/account/login" in (page.url or "").lower():
                logger.error(
                    "monitor: navigating to the verification page bounced to login "
                    f"— session lost ({page.url})"
                )
                return False

        await self._accept_consents(page)

        state = await self._gate_state(page)
        if state.get("submit") or state.get("verified"):
            logger.info("monitor: verification already satisfied")
            return await self._submit_verified_form(page)

        # STEP 1 — open the captcha popup. It may be a real window.open() popup
        # (it has its own title bar), in which case the grid is not in this
        # page's DOM at all and every later step must target the popup instead.
        main_page = page
        opened = await self._open_verification_popup(page)
        if opened is None:
            logger.warning("monitor: could not open the verification challenge")
            await utils.screenshot(page, "verify-no-button")
            await utils.dump_page_html(page, "verify-no-button")
            return False

        # CONFIRMED: the grid is served inside the GenerateCaptcha iframe, so
        # the solving context is that frame — not the parent page, and not a
        # popup (iframes are not pages, which is why expect_page found nothing).
        work = await self._wait_for_captcha_frame(opened)
        if work is None:
            logger.error(
                "monitor: the verification iframe never appeared — the challenge "
                "cannot be read"
            )
            await utils.screenshot(opened, "verify-no-frame")
            await utils.dump_page_html(opened, "verify-no-frame")
            await self._probe_captcha_endpoint(opened)
            return False
        await utils.human_delay(self.config)

        for attempt in range(1, VERIFY_MAX_ATTEMPTS + 1):
            logger.info(f"monitor: verification attempt {attempt}/{VERIFY_MAX_ATTEMPTS}")
            if self._restart_requested():
                logger.warning("monitor: dropped to login mid-verification — aborting")
                return False

            if _frame_gone(work):
                # The frame is torn down on success; the outcome is on the parent.
                logger.info("monitor: verification frame closed")
                state = await self._gate_state(main_page)
                if state.get("submit") or state.get("verified"):
                    logger.success("monitor: verification captcha passed")
                    self.state.log_event(
                        "bot", "visa-type verification cleared", status="ok"
                    )
                    return await self._submit_verified_form(main_page)
                logger.warning("monitor: popup closed but the gate is still shut")
                return False

            # The grid is injected on click, so capture it every attempt — these
            # artifacts are what the selectors get tightened against.
            # Screenshot from the page (a Frame cannot screenshot), but dump the
            # FRAME's html — that is where the grid markup actually lives.
            await utils.screenshot(main_page, f"verify-modal-attempt{attempt}")
            await utils.dump_page_html(work, f"verify-frame-attempt{attempt}")

            # STEP 2 — solve and submit the selection, inside the frame.
            #
            # CONFIRMED from the frame dump: this challenge is structurally
            # IDENTICAL to the login captcha — 54 div.col-4 tiles, img.captcha-img
            # holding base64 data: URIs, Select() onclick, a SelectedImages
            # hidden input. The digits are IMAGES, not DOM text, which is why the
            # text-based scan reported zero tiles. Reuse the vision solver,
            # pointed at the frame.
            self._dialogs.clear()
            # refresh_first: this grid was generated when the page loaded,
            # seconds before "Verify Selection" was even clicked. Submitting
            # against it raced a ~45s server-side token and lost (403 "Your
            # session has been expired") despite a correct reading.
            submitted = await self.auth.solve_captcha(
                main_page,
                ctx=work,
                offset=await self._frame_offset(work),
                refresh_first=True,
            )

            # STEP 3 — a rejected answer arrives as a JS alert, so give the
            # dialog handler a moment to fire before judging the outcome.
            await asyncio.sleep(1.0)
            alerts = list(self._dialogs)
            if alerts:
                logger.info(f"  dialog(s)       : {alerts}")

            expired = any(
                any(h in msg.lower() for h in EXPIRED_ALERT_HINTS) for msg in alerts
            )
            if expired:
                # Not a wrong answer — the token died before the submit landed.
                # A fresh grid is two seconds away, so pull one and try again
                # rather than abandoning the whole cycle. Retrying against the
                # SAME dead token is what produced a run of 403s previously;
                # _reload_captcha mints a new one and resets the age clock.
                logger.warning(
                    f"monitor: the challenge expired before submission ({alerts})"
                )
                await utils.screenshot(main_page, f"verify-expired-attempt{attempt}")
                if attempt < VERIFY_MAX_ATTEMPTS and not _frame_gone(work):
                    self.state.log_event(
                        "bot", "verification token expired — retrying", status="warn"
                    )
                    await self.auth.refresh_captcha(work)
                    continue
                self.state.log_event(
                    "bot", "verification challenge expired — restarting", status="warn"
                )
                return False

            rejected = any(
                any(h in msg.lower() for h in INVALID_ALERT_HINTS) for msg in alerts
            )

            if submitted and not rejected:
                if not _frame_gone(work):
                    await asyncio.sleep(1.0)
                # Verified / Submit appear on the PARENT page, never the popup.
                state = await self._gate_state(main_page)
                if state.get("submit") or state.get("verified"):
                    logger.success("monitor: verification captcha passed")
                    self.state.log_event(
                        "bot", "visa-type verification cleared", status="ok"
                    )
                    return await self._submit_verified_form(main_page)
                logger.warning("monitor: submitted but the gate is still closed")
            elif rejected:
                logger.warning(f"  outcome         : REJECTED — {alerts}")

            if attempt < VERIFY_MAX_ATTEMPTS and not _frame_gone(work):
                # Through auth so the token age clock is reset with the grid.
                if not await self.auth.refresh_captcha(work):
                    await self._reload_captcha_images(work)
                await utils.human_delay(self.config)

        logger.error("monitor: verification not cleared after all attempts")
        await utils.screenshot(main_page, "verify-failed")
        await utils.dump_page_html(main_page, "verify-failed")
        # Report, do not block. Waiting for a human /resume here would freeze an
        # unattended bot indefinitely; the caller retries on the next cycle.
        await self.notifier.error(
            "Could not clear the visa-type verification page automatically.",
            retry_info="retrying automatically on the next cycle",
        )

        state = await self._gate_state(main_page)
        if state.get("submit") or state.get("verified"):
            return await self._submit_verified_form(main_page)
        return False

    async def _solve_text_captcha(self, ctx: Any, attempt: int) -> bool:
        """Solve the captcha by reading tile numbers from the DOM.

        ``ctx`` is a Page or a Frame — the verification grid lives inside the
        /Global/NewCaptcha/GenerateCaptcha iframe, so this normally runs against
        that frame.
        """
        try:
            scan = await ctx.evaluate(_VERIFY_SCAN_JS)
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

        wanted = [i for i, t in enumerate(tiles, 1) if t["num"] == target]
        logger.info(f"  verify matches  : {len(wanted)} tile(s) showing {target} -> {wanted}")
        if not wanted:
            logger.warning(f"monitor: no tile shows {target}")
            return False

        # Click the tagged elements directly. Coordinate clicks would be wrong
        # here: inside an iframe, frame coordinates are a different space from
        # page coordinates, so page.mouse would land somewhere else entirely.
        for position in wanted:
            try:
                element = await ctx.query_selector(f'[data-bot-tile="{position}"]')
                if element is None:
                    logger.warning(f"monitor: tile {position} vanished before the click")
                    return False
                await element.click()
                logger.debug(f"  clicked verify tile at position {position}")
            except Exception as exc:
                logger.warning(f"monitor: could not click verify tile {position}: {exc}")
                return False
            await asyncio.sleep(0.5)

        if not await utils.click_by_text(ctx, SUBMIT_SELECTION_TEXTS, config=self.config):
            logger.warning("monitor: no 'Submit Selection' button found in the frame")
            return False
        logger.info("  verify submit   : clicked")
        await asyncio.sleep(1.5)
        return True

    async def _accept_consents(self, page: Any) -> None:
        """Dismiss the biometric / data-protection consent modals if shown."""
        for text in CONSENT_ACCEPT_TEXTS:
            if await utils.click_by_text(page, (text,), config=self.config):
                logger.info(f"monitor: accepted consent — {text!r}")
                await self._settle(page)

    async def _wait_for_captcha_frame(
        self, page: Any, timeout: float = CAPTCHA_FRAME_TIMEOUT
    ) -> Any | None:
        """Wait for the GenerateCaptcha iframe and for its grid to render.

        The Kendo loading mask sits over the window until the frame's content
        arrives, which is the spinner that appeared to hang: the frame existed
        but was still empty when the parent document was scanned.
        """
        waited = 0.0
        step = 0.5
        frame = None
        while waited < timeout:
            for candidate in page.frames:
                if CAPTCHA_FRAME_HINT in (candidate.url or "").lower():
                    frame = candidate
                    break
            if frame is not None:
                try:
                    ready = await frame.evaluate(
                        """
                        () => {
                          const txt = (document.body && document.body.innerText) || '';
                          const hasPrompt = /number\\s+\\d+/i.test(txt);
                          // The tiles are IMAGES, not text, so count the images.
                          // Counting numeric text here always returned 0 and made
                          // the frame look like it never finished rendering.
                          const imgs = document.querySelectorAll('img.captcha-img');
                          const cells = document.querySelectorAll('div.col-4');
                          return { hasPrompt, tiles: imgs.length || cells.length,
                                   imgs: imgs.length, cells: cells.length,
                                   chars: txt.trim().length };
                        }
                        """
                    )
                except Exception:
                    ready = None
                if ready and ready.get("hasPrompt") and ready.get("tiles", 0) >= 4:
                    logger.info(
                        f"monitor: captcha frame ready — {ready['tiles']} number "
                        f"element(s), url={frame.url}"
                    )
                    return frame
                if waited and int(waited) % 5 == 0:
                    logger.debug(f"monitor: captcha frame still loading — {ready}")
            await asyncio.sleep(step)
            waited += step

        if frame is None:
            logger.warning(
                f"monitor: no frame matching {CAPTCHA_FRAME_HINT!r} after "
                f"{timeout:.0f}s; frames seen: {[f.url for f in page.frames]}"
            )
        else:
            logger.warning(
                f"monitor: captcha frame never finished rendering ({frame.url})"
            )
        return frame

    async def complete_visatype_form(self, page: Any, combo: dict[str, Any]) -> bool:
        """Fill and submit /Global/bls/visatype.

        Order matters and is fixed: Appointment Category -> Appointment For ->
        Location -> Visa Type -> Visa Sub Type. Each dropdown's options are
        filtered from the previous choice, so setting them out of order writes
        into a list that is about to be replaced.
        """
        if "/bls/visatype" not in (page.url or "").lower():
            logger.warning(f"monitor: not on the appointment form ({page.url})")
            return False

        self._install_form_listener(page)
        self.install_navigation_watch(page)

        bls = self.config.get("bls", {}) or {}
        # Values come from the combination being swept, not straight from
        # config, because each field may be a list of options to try.
        wanted = {
            "appointment_category": combo.get("appointment_category", ""),
            "location": combo.get("location", ""),
            "visa_type": combo.get("visa_type", ""),
            "visa_sub_type": combo.get("visa_sub_type", ""),
        }

        self._option_unavailable = None
        # Fields configured with more than one value; only those are strict.
        swept: set[str] = combo.get("_swept") or set()
        logger.info("=" * 60)
        logger.info("APPOINTMENT FORM")

        # 1. Appointment Category
        if not await self._set_kendo(
            page,
            "Appointment Category",
            wanted["appointment_category"],
            strict="appointment_category" in swept,
        ):
            return await self._form_failed(page, "appointment-category")

        # 2. Appointment For (radio) — before the cascading dropdowns, because
        #    choosing Family reveals a "Number Of Members" field.
        appointment_for = str(combo.get("appointment_for", "Individual"))
        try:
            result = await page.evaluate(_RADIO_SET_JS, appointment_for)
        except Exception as exc:
            logger.warning(f"monitor: radio set failed: {exc}")
            result = {"ok": False}
        if result.get("ok"):
            logger.info(
                f"  Appointment For       : {result.get('value')!r} "
                f"(name={result.get('name')})"
            )
        else:
            logger.warning(f"  Appointment For       : NOT SET ({appointment_for!r})")
        await asyncio.sleep(1.0)

        # Choosing Family raises a confirmation modal and reveals an extra
        # "Number Of Members" dropdown. Both must be handled or the form cannot
        # be submitted.
        await self._handle_form_modals(page)
        if appointment_for.strip().lower().startswith("family"):
            members = str(bls.get("family_members", "") or "")
            if not await self._set_kendo(page, "Number Of Members", members):
                return await self._form_failed(page, "number-of-members")
        await self._settle(page)

        # 3-5. The cascading dropdowns, in order.
        for label, key in (
            ("Location", "location"),
            ("Visa Type", "visa_type"),
            ("Visa Sub Type", "visa_sub_type"),
        ):
            if not await self._set_kendo(
                page, label, wanted[key], strict=key in swept
            ):
                return await self._form_failed(page, label.lower().replace(" ", "-"))

        if self._option_unavailable:
            logger.info(
                f"monitor: skipping this combination — {self._option_unavailable} "
                "is not offered by the portal"
            )
            self._last_form_result = "option_unavailable"
            return True

        await utils.screenshot(page, "visatype-form-filled")

        if self._restart_requested():
            logger.warning("monitor: dropped to login before submit — aborting")
            return False

        # Re-check every field right before submitting. The cascades can clear a
        # field that was set earlier — a live run showed Location, Visa Type and
        # Visa Sub Type populated while Appointment Category had fallen back to
        # --Select--, which the portal rejects with "Select Appointment Category".
        unset = await self._unfilled_fields(page)
        if unset:
            logger.warning(f"  re-filling cleared fields: {unset}")
            for label in unset:
                key = {
                    "Appointment Category": "appointment_category",
                    "Location": "location",
                    "Visa Type": "visa_type",
                    "Visa Sub Type": "visa_sub_type",
                }.get(label)
                value = wanted.get(key, "") if key else ""
                await self._set_kendo(page, label, value)
            still = await self._unfilled_fields(page)
            if still:
                logger.error(f"  fields still empty after re-fill: {still}")
                return await self._form_failed(page, "fields-empty")

        self._last_visatype_status = None
        if not await utils.click_by_text(page, ("submit",), config=self.config):
            logger.warning("monitor: no Submit on the appointment form")
            return await self._form_failed(page, "no-submit")

        logger.info("monitor: appointment form submitted")
        await self._settle(page)
        logger.info(f"monitor: URL after submit — {page.url}")

        # The POST can fail outright — an expired session or a stale token comes
        # back as 4xx, or the portal simply bounces to the login page. Neither
        # is recoverable here: the whole journey has to start again.
        status = self._last_visatype_status
        url_now = (page.url or "").lower()
        bounced = "/account/login" in url_now
        if (status is not None and status >= 400) or bounced:
            logger.error(
                f"monitor: appointment submit failed "
                f"(HTTP {status}, url={page.url}) — session looks dead"
            )
            await utils.screenshot(page, "visatype-submit-failed")
            await utils.dump_page_html(page, "visatype-submit-failed")
            await self._restart_journey(page)
            return False
        logger.info("=" * 60)

        await utils.screenshot(page, "after-visatype-submit")
        await utils.dump_page_html(page, "after-visatype-submit")

        # CONFIRMED: when nothing is free the portal answers with a modal —
        # "No Appointments Available / Currently, no slots are available for the
        # selected category." That is the no-slots signal for this whole flow,
        # not an error, so report it and dismiss the dialog cleanly.
        # SUCCESS: the portal moves on to the appointment manager.
        if "manageappointment" in (page.url or "").lower():
            logger.success(f"monitor: appointment page reached — {page.url}")
            self.state.log_event("bot", "SLOTS AVAILABLE — booking page reached", status="ok")
            self._last_form_result = "appointment_page"
            await self.notifier.slot_found(location, "booking page reached")
            await self._survey_page(page, "manage-appointment")

            # Step 1 of the booking: date -> time slot -> photo, in that order.
            if not await self.complete_appointment_details(page):
                logger.warning("monitor: could not complete the appointment details")
                self._last_form_result = "no_slots"
            return True

        # The form rejects an incomplete submission inline, e.g.
        # "Select Appointment Category" — the fields need setting again.
        problem = await self._form_validation_error(page)
        if problem:
            logger.warning(f"monitor: form validation error — {problem!r}")
            await utils.screenshot(page, "visatype-validation-error")
            self._last_form_result = "validation_error"
            return False

        # CONFIRMED: the portal answers through a shared modal with stable ids —
        #   <span id="commonModalHeader">No Appointments Available</span>
        #   <span id="commonModalBody">Currently, no slots are available ...</span>
        # Reading those beats scraping page text, and the same modal carries
        # other messages, so the header is what decides the meaning.
        modal = await self._read_common_modal(page)
        if modal:
            header = (modal.get("header") or "").strip()
            body = (modal.get("body") or "").strip()
            logger.info(f"monitor: portal modal — {header!r}")
            logger.info(f"           {body[:160]}")
            await self._dismiss_common_modal(page)

            detection = self.config.get("detection", {}) or {}
            combined = f"{header} {body}".lower()
            if utils.contains_any(combined, detection.get("unavailable_phrases", [])):
                logger.info("monitor: no slots available for this selection")
                self._last_form_result = "no_slots"
                return True

            logger.warning(f"monitor: unrecognised portal message — {header!r}")
            self._last_form_result = "modal_unknown"
            await utils.screenshot(page, "visatype-modal-unknown")
            return True

        detection = self.config.get("detection", {}) or {}
        text = await utils.page_text(page)
        hit = utils.contains_any(text, detection.get("unavailable_phrases", []))
        if hit:
            logger.info(f"monitor: no slots — portal said {hit!r}")
            await self._dismiss_common_modal(page)
            self._last_form_result = "no_slots"
            return True

        self._last_form_result = "slots_possible"
        logger.success("monitor: no 'no slots' message — the form advanced")
        await self._survey_page(page, "after-visatype-submit")
        return True

    async def _set_kendo(
        self, page: Any, label: str, wanted: str, *, strict: bool = False
    ) -> bool:
        """Set one Kendo DropDownList, chosen by visible label.

        Waits for the widget's data source to populate first: each list is
        refilled by the previous field's change handler, so it is briefly empty.

        ``strict`` marks a field that is being SWEPT across several configured
        values. There, an option the portal does not offer means this
        combination is invalid (a Schengen sub-type under National Visa) and
        should be skipped. For a single configured value it stays False, so an
        unavailable option falls back to the portal's first real choice and
        only warns — skipping would leave the cycle checking nothing at all.
        """
        deadline = 15.0
        waited = 0.0
        result: dict[str, Any] = {}
        while waited < deadline:
            try:
                result = await page.evaluate(
                    _KENDO_SET_JS, {"label": label, "wanted": wanted}
                )
            except Exception as exc:
                logger.warning(f"monitor: {label!r} set threw: {exc}")
                result = {"ok": False, "reason": str(exc)}
            if result.get("ok"):
                opts = result.get("options") or []
                logger.info(
                    f"  {label:<22}: {result.get('chosen')!r} "
                    f"(of {len(opts)} options, widget {result.get('id')})"
                )
                mismatch = (
                    wanted
                    and result.get("chosen")
                    and wanted.lower() not in str(result["chosen"]).lower()
                )
                if mismatch:
                    logger.warning(
                        f"      {wanted!r} is not available here (took "
                        f"{result['chosen']!r}); options: {opts}"
                    )
                    if strict:
                        # Swept field: this pairing is invalid, so skip it
                        # rather than re-test a combination already covered.
                        self._option_unavailable = f"{label}={wanted!r}"
                    else:
                        logger.warning(
                            f"      {label} is a single configured value, so "
                            "continuing with the portal's choice rather than "
                            "skipping the only combination there is"
                        )
                # Give the dependent list time to be refilled by 'change'.
                await asyncio.sleep(1.2)
                return True
            await asyncio.sleep(0.5)
            waited += 0.5

        logger.error(
            f"  {label:<22}: FAILED — {result.get('reason')} "
            f"(options seen: {result.get('options')})"
        )
        return False

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        """Accept either a scalar or a list in config, always return a list."""
        if value is None:
            return [""]
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            return items or [""]
        text = str(value).strip()
        return [text] if text else [""]

    def build_combinations(self) -> list[dict[str, Any]]:
        """Every booking option to try, in order.

        location, visa_type, visa_sub_type, appointment_category and
        appointment_for may each be a single value or a list. The cross-product
        of the lists is swept in one cycle.

        Ordered with location outermost so the two centres are covered early:
        if only one option per field is configured this is just the single
        combination, and nothing changes.
        """
        bls = self.config.get("bls", {}) or {}
        labels = bls.get("location_labels", {}) or {}

        # Which fields are genuinely being swept. An unavailable option only
        # justifies SKIPPING the combination when the field is a list: that
        # means invalid pairings are expected (a Schengen sub-type under
        # National Visa). For a single configured value, skipping would leave
        # the cycle checking nothing at all, so fall back and warn instead.
        swept = {
            name
            for name in (
                "visa_type",
                "visa_sub_type",
                "appointment_category",
                "appointment_for",
            )
            if len(self._as_list(bls.get(name))) > 1
        }
        if len(self.locations) > 1:
            swept.add("location")

        combos: list[dict[str, Any]] = []
        for location in self.locations:
            for visa_type in self._as_list(bls.get("visa_type")):
                for sub_type in self._as_list(bls.get("visa_sub_type")):
                    for category in self._as_list(bls.get("appointment_category")):
                        for who in self._as_list(bls.get("appointment_for")):
                            combos.append(
                                {
                                    "location_key": location,
                                    "location": labels.get(location, location.title()),
                                    "visa_type": visa_type,
                                    "visa_sub_type": sub_type,
                                    "appointment_category": category,
                                    "appointment_for": who or "Individual",
                                    "_swept": swept,
                                }
                            )

        if len(combos) > self.max_combinations:
            logger.warning(
                f"monitor: {len(combos)} option combinations configured — capping "
                f"at {self.max_combinations} per cycle. Each one needs its own "
                "verification captcha, and too many requests per cycle is what "
                "triggers the portal's rate limiting."
            )
            combos = combos[: self.max_combinations]
        return combos

    @staticmethod
    def describe_combo(combo: dict[str, Any]) -> str:
        return (
            f"{combo['location']} / {combo['visa_type']} / "
            f"{combo['visa_sub_type']} / {combo['appointment_category']} / "
            f"{combo['appointment_for']}"
        )

    def next_location(self) -> str:
        """The centre to ask about this round, rotating through bls.locations.

        Each centre has its own slot pool, so asking about the same one every
        time both misses openings elsewhere and repeats an identical request.
        Round 1 takes the first configured location, round 2 the next, and so on.
        """
        if not self.locations:
            return "cairo"
        location = self.locations[self._location_index % len(self.locations)]
        self._location_index += 1
        if len(self.locations) > 1:
            logger.info(
                f"monitor: this round checks {location!r} "
                f"(round {self._location_index}, rotating {self.locations})"
            )
        return location

    async def complete_appointment_details(self, page: Any) -> bool:
        """Fill the booking page's first step: date, time slot, photo.

        Order is fixed by the page itself — the Time Slot dropdown stays
        disabled until a date is chosen, and the slot list is fetched for that
        date. Availability is colour-coded: green is free, red is booked, grey
        is unselectable, so only green is ever clicked.
        """
        logger.info("=" * 60)
        logger.info("APPOINTMENT DETAILS (date / slot / photo)")
        await utils.dump_page_html(page, "manage-appointment")

        if not await self._pick_green_date(page):
            await utils.screenshot(page, "appointment-no-green-date")
            logger.warning("monitor: no selectable (green) date offered")
            return False

        if not await self._pick_green_slot(page):
            await utils.screenshot(page, "appointment-no-green-slot")
            logger.warning("monitor: no free (green) time slot for that date")
            return False

        await self._upload_photo(page)
        await utils.screenshot(page, "appointment-details-filled")

        # Request the emailed code and verify it.
        if not await self._verify_email(page):
            logger.warning("monitor: email not verified — cannot finalise")
            logger.info("=" * 60)
            return False

        # Finally: Verify Appointment, then Submit Appointment.
        ok = await self._finalise_appointment(page)
        logger.info("=" * 60)
        return ok

    async def _finalise_appointment(self, page: Any) -> bool:
        """Verify and submit the appointment, securing the slot.

        CONFIRMED controls, all hidden until their prerequisites are met:
            #btnVerifyAppointment    onclick="VerifyAppointment(event,this)"
            #btnVerifiedAppointment  (revealed once accepted)
            #btnSubmit               onclick="return OnAppointmentSubmit(event)"

        This books the slot. It is not a payment step — payment comes later and
        the bot stops there by design.
        """
        # Mobile verification may also be demanded; it needs an SMS the bot
        # cannot read, so hand over rather than stalling silently.
        try:
            mobile_needed = await page.evaluate(
                """
                () => {
                  const box = document.querySelector('.div-mobile-code');
                  if (!box) return false;
                  return getComputedStyle(box).display !== 'none';
                }
                """
            )
        except Exception:
            mobile_needed = False
        if mobile_needed:
            logger.warning("monitor: mobile verification required — handing over")
            shot = await utils.screenshot(page, "appointment-mobile-verification")
            self.state.set_manual_pause("mobile verification code")
            await self.notifier.manual_required(
                "The booking needs an SMS verification code, which the bot cannot "
                "read. Enter it in the browser, then send /resume.",
                screenshot=shot,
            )
            await self.notifier.wait_for_resume()
            self.state.clear_manual_pause()

        for label, fn, button_id in (
            ("verify appointment", "VerifyAppointment", "btnVerifyAppointment"),
            ("submit appointment", "OnAppointmentSubmit", "btnSubmit"),
        ):
            clicked = await self._click_when_ready(page, button_id, fn)
            if not clicked:
                logger.error(f"  {label:<22}: control never became available")
                await utils.screenshot(page, f"appointment-{button_id}-missing")
                await utils.dump_page_html(page, f"appointment-{button_id}-missing")
                return False
            logger.success(f"  {label:<22}: clicked")
            await self._settle(page)
            await asyncio.sleep(2.0)

        await utils.screenshot(page, "appointment-submitted")
        await utils.dump_page_html(page, "appointment-submitted")
        logger.success(f"monitor: appointment submitted — now at {page.url}")
        self.state.log_event("bot", "APPOINTMENT SUBMITTED", status="ok")
        await self.notifier.send(
            "✅ Appointment details submitted — the slot is being held.\n"
            f"Page: {page.url[:120]}\n\n"
            "The next stage is payment. The bot will stop there and ask you to "
            "take over."
        )
        await self._survey_page(page, "after-appointment-submit")
        return True

    async def _click_when_ready(
        self, page: Any, button_id: str, fallback_fn: str, timeout: float = 30.0
    ) -> bool:
        """Click a button once the page reveals it, or call its handler.

        These controls start style="display:none" and are shown only when their
        preconditions are satisfied, so clicking immediately would miss them.
        """
        waited = 0.0
        while waited < timeout:
            try:
                state = await page.evaluate(
                    """
                    (args) => {
                      const el = document.getElementById(args.id);
                      if (!el) return 'missing';
                      const shown = getComputedStyle(el).display !== 'none';
                      if (!shown) return 'hidden';
                      if (el.disabled) return 'disabled';
                      el.click();
                      return 'clicked';
                    }
                    """,
                    {"id": button_id},
                )
            except Exception as exc:
                logger.debug(f"monitor: {button_id} probe failed: {exc}")
                state = "error"
            if state == "clicked":
                return True
            await asyncio.sleep(1.0)
            waited += 1.0

        logger.warning(f"monitor: #{button_id} never appeared — trying {fallback_fn}()")
        try:
            called = await page.evaluate(
                """
                (name) => {
                  if (typeof window[name] === 'function') {
                    window[name](new Event('click'), null);
                    return true;
                  }
                  return false;
                }
                """,
                fallback_fn,
            )
            return bool(called)
        except Exception as exc:
            logger.debug(f"monitor: {fallback_fn}() failed: {exc}")
            return False

    async def _verify_email(self, page: Any) -> bool:
        """Request the email verification code, read it, and submit it.

        CONFIRMED controls:
            <button id="btnSenderificationCode" onclick="RequestCode(event,this)">
            <input  id="EmailVerificationCode">
            <button id="btnVerifyEmail" onclick="VerifyEmailCode(event,this)">
            <button id="btnVerifiedEmail">  (revealed once accepted)

        The code is read over IMAP. Without otp.imap_user / imap_pass the step
        is skipped with a clear message rather than silently stalling.
        """
        otp_cfg = self.config.get("otp", {}) or {}
        if not otp_cfg.get("imap_user") or not otp_cfg.get("imap_pass"):
            logger.warning(
                "  email code            : SKIPPED — otp.imap_user / imap_pass "
                "are empty, so the code cannot be read"
            )
            self.state.log_event(
                "bot", "email verification needs IMAP credentials", status="warn"
            )
            return False

        try:
            requested = await page.evaluate(
                """
                () => {
                  const btn = document.getElementById('btnSenderificationCode')
                    || document.querySelector('[onclick*="RequestCode"]');
                  if (!btn) return false;
                  btn.click();
                  return true;
                }
                """
            )
        except Exception as exc:
            logger.warning(f"  email code            : request failed — {exc}")
            return False
        if not requested:
            logger.warning("  email code            : no 'Get Verification Code' button")
            return False
        logger.info("  email code            : requested, waiting for the mail")
        await asyncio.sleep(3.0)

        code = await otp.fetch_email_code(self.config)
        if not code:
            logger.error("  email code            : never arrived")
            self.state.log_event("bot", "email verification code not received", status="error")
            return False
        logger.success(f"  email code            : received ({len(code)} digits)")

        try:
            filled = await page.evaluate(
                """
                (value) => {
                  const box = document.getElementById('EmailVerificationCode');
                  if (!box) return false;
                  box.value = value;
                  box.dispatchEvent(new Event('input', { bubbles: true }));
                  box.dispatchEvent(new Event('change', { bubbles: true }));
                  const btn = document.getElementById('btnVerifyEmail')
                    || document.querySelector('[onclick*="VerifyEmailCode"]');
                  if (btn) { btn.click(); return true; }
                  return 'filled-no-button';
                }
                """,
                code,
            )
        except Exception as exc:
            logger.warning(f"  email code            : entry failed — {exc}")
            return False

        await asyncio.sleep(2.5)
        try:
            verified = await page.evaluate(
                """
                () => {
                  const ok = document.getElementById('btnVerifiedEmail');
                  const flag = document.getElementById('EmailVerified');
                  const shown = ok && getComputedStyle(ok).display !== 'none';
                  return { shown: !!shown, flag: flag ? flag.value : null };
                }
                """
            )
        except Exception:
            verified = {}
        logger.info(f"  email verified        : {verified}")
        await utils.screenshot(page, "appointment-email-verified")
        return bool(verified.get("shown") or str(verified.get("flag")).lower() == "true")

    async def _pick_green_date(self, page: Any) -> bool:
        """Open the live date picker and click an available day.

        The page carries decoy pickers (AppointmentDate1..5); all but one are
        k-state-disabled, so the enabled one is the real field. Availability is
        shown by cell colour, so green is detected from the computed background
        rather than a class name, which is random here as everywhere else.
        """
        try:
            opened = await page.evaluate(
                """
                () => {
                  for (const inp of document.querySelectorAll('input[data-role="datepicker"]')) {
                    if (inp.disabled) continue;
                    const wrap = inp.closest('.k-picker-wrap');
                    if (wrap && wrap.classList.contains('k-state-disabled')) continue;
                    const dp = window.jQuery && jQuery(inp).data('kendoDatePicker');
                    if (!dp) continue;
                    dp.open();
                    return inp.id;
                  }
                  return null;
                }
                """
            )
        except Exception as exc:
            logger.warning(f"monitor: could not open the date picker: {exc}")
            return False
        if not opened:
            logger.warning("monitor: no enabled date picker on the page")
            return False
        logger.info(f"  date picker           : opened #{opened}")
        await asyncio.sleep(1.2)
        await utils.screenshot(page, "appointment-calendar-open")

        try:
            picked = await page.evaluate(
                """
                (pickerId) => {
                  const green = (el) => {
                    const bg = getComputedStyle(el).backgroundColor || '';
                    const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(bg);
                    if (!m) return false;
                    const r = +m[1], g = +m[2], b = +m[3];
                    // Clearly green: dominant green channel, not grey/red/amber.
                    return g > 90 && g > r + 40 && g > b + 40;
                  };
                  const cells = Array.from(
                    document.querySelectorAll('.k-calendar td, .k-calendar .k-link'));
                  const options = [];
                  for (const cell of cells) {
                    if (cell.classList.contains('k-state-disabled')) continue;
                    if (cell.classList.contains('k-other-month')) continue;
                    const link = cell.matches('.k-link') ? cell : cell.querySelector('.k-link');
                    const paint = green(cell) || (link && green(link));
                    const text = ((link || cell).innerText || '').trim();
                    if (!text) continue;
                    options.push({ text, green: !!paint });
                    if (paint) {
                      (link || cell).click();
                      return { clicked: text, offered: options };
                    }
                  }
                  return { clicked: null, offered: options };
                }
                """,
                opened,
            )
        except Exception as exc:
            logger.warning(f"monitor: date selection failed: {exc}")
            return False

        offered = picked.get("offered") or []
        logger.info(
            "  days offered          : "
            + ", ".join(f"{d['text']}{'*' if d['green'] else ''}" for d in offered[:31])
            + "   (* = available)"
        )
        if not picked.get("clicked"):
            return False
        logger.success(f"  date chosen           : {picked['clicked']}")
        await self._settle(page)
        await asyncio.sleep(1.5)
        return True

    async def _pick_green_slot(self, page: Any) -> bool:
        """Choose a free time slot once a date has been picked.

        The Kendo template renders each slot with bg-success when free and
        bg-danger when booked (Count < Code), so a date can be selectable while
        every slot on it is full — only green is acceptable.
        """
        try:
            result = await page.evaluate(
                """
                () => {
                  let dd = null, id = null;
                  for (const inp of document.querySelectorAll('input[data-role="dropdownlist"]')) {
                    if (!/AppointmentSlot/i.test(inp.id || '')) continue;
                    if (inp.disabled) continue;
                    const w = window.jQuery && jQuery(inp).data('kendoDropDownList');
                    if (!w) continue;
                    dd = w; id = inp.id; break;
                  }
                  if (!dd) return { ok: false, reason: 'no enabled slot dropdown' };

                  const data = dd.dataSource.data() || [];
                  const slots = [];
                  let chosen = null;
                  for (let i = 0; i < data.length; i++) {
                    const item = data[i];
                    const name = String(item.Name || '');
                    // Mirrors the page's own template: Count < Code means full.
                    const free = !(Number(item.Count) < Number(item.Code));
                    slots.push({ name, free });
                    if (free && !chosen && name && !/^-+\\s*select/i.test(name)) {
                      chosen = { name, id: item.Id, index: i };
                    }
                  }
                  if (!chosen) return { ok: false, reason: 'all slots booked', slots, id };
                  dd.value(chosen.id);
                  dd.trigger('change');
                  return { ok: true, chosen: chosen.name, slots, id };
                }
                """
            )
        except Exception as exc:
            logger.warning(f"monitor: slot selection failed: {exc}")
            return False

        slots = result.get("slots") or []
        if slots:
            logger.info(
                "  slots offered         : "
                + ", ".join(f"{s['name']}{'*' if s['free'] else ''}" for s in slots[:20])
                + "   (* = free)"
            )
        if not result.get("ok"):
            logger.warning(f"  slot                  : {result.get('reason')}")
            return False
        logger.success(f"  slot chosen           : {result.get('chosen')!r}")
        await asyncio.sleep(1.0)
        return True

    async def _upload_photo(self, page: Any) -> bool:
        """Attach the applicant photo to the booking form."""
        photo = (self.config.get("applicant", {}) or {}).get("documents", {}).get("photo")
        if not photo:
            logger.warning("  photo                 : not configured")
            return False
        from pathlib import Path as _Path

        path = _Path(photo)
        if not path.is_absolute():
            path = _Path.cwd() / path
        if not path.exists():
            logger.error(f"  photo                 : MISSING at {path}")
            self.state.log_event("bot", f"photo not found: {photo}", status="error")
            return False
        size_kb = path.stat().st_size / 1024
        if size_kb > 200:
            logger.warning(
                f"  photo                 : {size_kb:.0f} KB — the form requires "
                "under 200 KB and will reject it"
            )

        try:
            element = await page.query_selector("#uploadfile-1")
            if element is None:
                element = await page.query_selector('input[type="file"]')
            if element is None:
                logger.warning("  photo                 : no file input found")
                return False
            await element.set_input_files(str(path))
            logger.success(f"  photo                 : uploaded {path.name} ({size_kb:.0f} KB)")
            await asyncio.sleep(1.5)
            return True
        except Exception as exc:
            logger.warning(f"  photo                 : upload failed — {exc}")
            return False

    async def _unfilled_fields(self, page: Any) -> list[str]:
        """Labels of visible required dropdowns still showing the placeholder."""
        try:
            return await page.evaluate(
                """
                () => {
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) return false;
                    let n = el;
                    while (n && n.nodeType === 1) {
                      const s = getComputedStyle(n);
                      if (s.display === 'none' || s.visibility === 'hidden') return false;
                      n = n.parentElement;
                    }
                    return true;
                  };
                  const empty = [];
                  for (const inp of document.querySelectorAll('input[data-role="dropdownlist"]')) {
                    const widget = inp.closest('span.k-widget');
                    if (!vis(widget)) continue;
                    const box = inp.closest('div.mb-3');
                    if (box && !vis(box)) continue;
                    const shown = widget.querySelector('.k-input');
                    const text = ((shown && shown.innerText) || '').trim();
                    if (!text || /^-+\\s*select/i.test(text)) {
                      const lab = inp.id
                        ? document.querySelector('label[for="' + inp.id + '"]') : null;
                      let name = (lab && lab.innerText) || inp.name || inp.id || '?';
                      empty.push(name.replace(/\\*/g, '').trim());
                    }
                  }
                  return empty;
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: field check failed: {exc}")
            return []

    async def _read_common_modal(self, page: Any) -> dict[str, str] | None:
        """Read the portal's shared result modal, if it is showing."""
        try:
            return await page.evaluate(
                """
                () => {
                  const head = document.getElementById('commonModalHeader');
                  const body = document.getElementById('commonModalBody');
                  if (!head && !body) return null;
                  const probe = head || body;
                  const r = probe.getBoundingClientRect();
                  if (r.width < 5 || r.height < 5) return null;
                  let n = probe;
                  while (n && n.nodeType === 1) {
                    if (getComputedStyle(n).display === 'none') return null;
                    n = n.parentElement;
                  }
                  return {
                    header: (head && head.innerText) || '',
                    body: (body && body.innerText) || '',
                  };
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: modal read failed: {exc}")
            return None

    async def _dismiss_common_modal(self, page: Any) -> None:
        """Close the result modal via its own dismiss control."""
        try:
            await page.evaluate(
                """
                () => {
                  const btn = document.querySelector(
                    '.modal-footer button[data-bs-dismiss="modal"], '
                    + '.modal-header button[data-bs-dismiss="modal"]');
                  if (btn) { btn.click(); return true; }
                  return false;
                }
                """
            )
            await asyncio.sleep(0.8)
        except Exception as exc:
            logger.debug(f"monitor: modal dismiss failed: {exc}")

    async def _form_validation_error(self, page: Any) -> str | None:
        """The portal's own inline validation message, if one is showing."""
        try:
            return await page.evaluate(
                """
                () => {
                  const sel = '.validation-summary, .field-validation-error,'
                            + ' .text-danger, .validation-summary-errors';
                  for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    if (getComputedStyle(el).display === 'none') continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length > 3) return t.slice(0, 200);
                  }
                  return null;
                }
                """
            )
        except Exception:
            return None

    async def _restart_journey(self, page: Any) -> None:
        """Reload, and if the portal has dropped us at login, start over.

        Reached when the appointment POST fails (4xx) or bounces to login —
        typically an expired session. Reloading alone is not enough: the stored
        cookies are stale too, so they are discarded and the next cycle performs
        a full login, verification captcha and form fill from scratch.
        """
        logger.warning("monitor: restarting the journey from the login page")
        try:
            await page.reload(wait_until="domcontentloaded")
            await self._settle(page)
        except Exception as exc:
            logger.debug(f"monitor: reload failed: {exc}")

        url_now = (page.url or "").lower()
        at_login = "/account/login" in url_now
        logger.info(f"monitor: after reload — {page.url} (login page: {at_login})")

        # Drop the saved session either way: whatever state produced a 4xx is
        # not worth replaying on the next cycle.
        try:
            self.auth._clear_saved_session()
            logger.info("monitor: stored session discarded — next cycle logs in fresh")
        except Exception as exc:
            logger.debug(f"monitor: could not clear the session: {exc}")

        self.state.log_event(
            "bot", "appointment submit failed — restarting from login", status="warn"
        )

    def install_navigation_watch(self, page: Any) -> None:
        """Track every main-frame navigation and name the stage it lands on.

        The portal can drop the session at any moment — a redirect to the login
        page can happen mid-step, not only at the checkpoints the code happens
        to test. Watching navigations catches it wherever it occurs, and sets a
        restart flag the running step checks so the journey begins again from
        the login page instead of continuing against a dead session.
        """
        if id(page) in self._nav_hooked:
            return

        def _on_nav(frame: Any) -> None:
            try:
                if frame != page.main_frame:
                    return  # iframes navigate constantly; only the page matters
                url = frame.url or ""
            except Exception:
                return

            stage = classify_page(url)
            previous = self._page_stage
            self._page_stage = stage
            if stage != previous:
                logger.info(f"monitor: page -> {stage}  ({url[:110]})")

            # Landing back on login from anywhere further along means the
            # session died; the journey has to restart.
            if stage == "login" and previous not in (None, "login", "login-captcha"):
                logger.warning(
                    f"monitor: bounced to the login page from {previous!r} — "
                    "session lost, restarting the journey"
                )
                self._needs_restart = True

            # A refresh can also drop us back to the verification gate. That is
            # cheaper than a full restart: the session is still good, only the
            # visa-type verification has to be redone.
            if stage == "verification" and previous in ("appointment-form", "appointment"):
                logger.warning(
                    f"monitor: returned to the verification gate from {previous!r} "
                    "— it must be solved again"
                )
                self._needs_reverify = True

        try:
            page.on("framenavigated", _on_nav)
            self._nav_hooked.add(id(page))
            self._page_stage = classify_page(page.url or "")
            logger.info(f"monitor: navigation watch installed (now: {self._page_stage})")
        except Exception as exc:
            logger.debug(f"monitor: could not install the navigation watch: {exc}")

    def _restart_requested(self) -> bool:
        """True once a navigation showed we were dropped back at login."""
        if self._needs_restart:
            self._needs_restart = False
            return True
        return False

    def _install_form_listener(self, page: Any) -> None:
        """Record the status of the appointment form's own POST."""
        if id(page) in self._form_hooked:
            return

        def _on_response(response: Any) -> None:
            url = (response.url or "").lower()
            if "/bls/visatype" in url and "verification" not in url:
                self._last_visatype_status = response.status
                logger.info(f"monitor: [net] {response.status} {response.url[:120]}")

        try:
            page.on("response", _on_response)
            self._form_hooked.add(id(page))
        except Exception as exc:
            logger.debug(f"monitor: could not install the form listener: {exc}")

    async def _handle_form_modals(self, page: Any) -> None:
        """Dismiss the modals the appointment form raises while being filled.

        CONFIRMED:
          * Family Appointment — <button onclick="return OnFamilyAccept();">
            Accept. Appears whenever Appointment For is set to Family, and the
            form cannot be submitted until it is cleared.
          * #PremiumTypeModel — shown by onCategoryChangeN() when the chosen
            category's Code is CATEGORY_PREMIUM.

        Calls the page's own handlers; clicking by text would hit the <button>
        correctly here, but the handler is what the site actually relies on.
        """
        try:
            result = await page.evaluate(
                """
                () => {
                  const out = [];
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 5 && r.height > 5 &&
                           getComputedStyle(el).display !== 'none';
                  };
                  // Family confirmation.
                  const fam = Array.from(document.querySelectorAll('button'))
                    .find((b) => /OnFamilyAccept/.test(b.getAttribute('onclick') || '')
                                 && vis(b));
                  if (fam) { fam.click(); out.push('family-accept'); }
                  else if (typeof OnFamilyAccept === 'function') {
                    const dlg = document.querySelector('.modal.show, .modal[style*="display: block"]');
                    if (dlg && /family/i.test(dlg.innerText || '')) {
                      OnFamilyAccept(); out.push('family-accept-fn');
                    }
                  }
                  // Premium category notice.
                  const prem = document.getElementById('PremiumTypeModel');
                  if (prem && vis(prem)) {
                    const ok = prem.querySelector('button.btn-success, button.btn-primary');
                    if (ok) { ok.click(); out.push('premium-dismissed'); }
                    else { out.push('premium-open-no-button'); }
                  }
                  return out;
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: modal handling failed: {exc}")
            return
        if result:
            logger.info(f"  form modals           : {result}")
            await asyncio.sleep(1.0)

    async def _form_failed(self, page: Any, tag: str) -> bool:
        await utils.screenshot(page, f"visatype-{tag}-failed")
        await utils.dump_page_html(page, f"visatype-{tag}-failed")
        await self._survey_page(page, f"visatype-{tag}-failed")
        return False

    @staticmethod
    async def _frame_offset(frame: Any) -> tuple[float, float]:
        """Where the iframe sits in the page, for converting clip coordinates.

        Tile rects measured inside the frame are frame-relative, but only a Page
        can screenshot, and its clip is in page coordinates. Adding the iframe's
        own position bridges the two.
        """
        try:
            element = await frame.frame_element()
            box = await element.bounding_box()
            if box:
                logger.info(
                    f"monitor: captcha frame at x={box['x']:.0f} y={box['y']:.0f} "
                    f"({box['width']:.0f}x{box['height']:.0f})"
                )
                return float(box["x"]), float(box["y"])
        except Exception as exc:
            logger.warning(f"monitor: could not locate the iframe: {exc}")
        return 0.0, 0.0

    async def _probe_captcha_endpoint(self, page: Any) -> None:
        """Fetch /Global/NewCaptcha/GenerateCaptcha directly and report back.

        Separates the two candidate causes. If this returns the grid, the frame
        failed for transport reasons (mixed-content blocking). If it returns the
        login page, the session is not valid for that endpoint and no amount of
        frame handling will help.
        """
        base = (self.config.get("bls", {}) or {}).get("url", "").rstrip("/")
        url = f"{base}/Global/NewCaptcha/GenerateCaptcha"
        logger.info(f"monitor: probing the captcha endpoint directly — {url}")
        probe = None
        try:
            probe = await page.context.new_page()
            response = await probe.goto(url, wait_until="domcontentloaded")
            status = response.status if response else "?"
            text = (await probe.inner_text("body"))[:300] if response else ""
            looks_login = "/account/login" in (probe.url or "").lower()
            has_grid = bool(re.search(r"number\s+\d+", text, re.I))
            logger.info(f"  probe status    : {status}")
            logger.info(f"  probe final url : {probe.url}")
            logger.info(f"  looks like login: {looks_login}")
            logger.info(f"  has number prompt: {has_grid}")
            logger.info(f"  body starts     : {text[:160]!r}")
            await utils.screenshot(probe, "captcha-endpoint-probe")
            await utils.dump_page_html(probe, "captcha-endpoint-probe")
            if looks_login:
                logger.error(
                    "monitor: the captcha endpoint redirects to login — the "
                    "session is not accepted for it. This is an auth problem, "
                    "not an iframe problem."
                )
                await self._dump_session_cookies(page, url)
            elif has_grid:
                logger.warning(
                    "monitor: the endpoint serves the grid fine on its own, so "
                    "the iframe is being blocked in the browser (mixed content)."
                )
        except Exception as exc:
            logger.warning(f"monitor: captcha endpoint probe failed: {exc}")
        finally:
            if probe is not None:
                try:
                    await probe.close()
                except Exception:
                    pass

    async def _dump_session_cookies(self, page: Any, target_url: str) -> None:
        """List the cookies the browser holds, to show what the endpoint saw.

        The parent page is authenticated while GenerateCaptcha is not, so the
        difference is in what gets sent: either an auth cookie is scoped so it
        does not reach that path, or a fingerprint cookie the endpoint requires
        was never set (which is what a crashed FingerprintJS would cause).
        """
        try:
            cookies = await page.context.cookies([target_url])
        except Exception as exc:
            logger.warning(f"monitor: could not read cookies: {exc}")
            return

        logger.info(f"  cookies sent to {target_url[:70]} — {len(cookies)} total")
        for cookie in cookies:
            name = cookie.get("name", "")
            value = str(cookie.get("value", ""))
            # Never log a session or fingerprint value, only its shape.
            logger.info(
                f"     {name:<34} len={len(value):<5} "
                f"path={cookie.get('path', '')} "
                f"secure={cookie.get('secure')} "
                f"httpOnly={cookie.get('httpOnly')} "
                f"sameSite={cookie.get('sameSite')}"
            )
        names = {c.get("name", "").lower() for c in cookies}
        if not any("auth" in n or "session" in n or ".aspnet" in n for n in names):
            logger.error(
                "  no auth/session cookie reaches this URL — that alone explains "
                "the redirect to login"
            )

    async def _open_verification_popup(self, page: Any) -> Any | None:
        """Click "Verify Selection" and return the page holding the challenge.

        Returns the popup page when one opens, the original page when the
        challenge renders inline, or None when the button could not be clicked.
        """
        clicked = False
        popup = None
        try:
            async with page.context.expect_page(timeout=8000) as info:
                clicked = await utils.click_by_text(
                    page, VERIFY_SELECTION_TEXTS, config=self.config
                )
                if not clicked:
                    raise PlaywrightTimeout("button not clicked")
            popup = await info.value
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            logger.debug(f"monitor: popup wait ended: {exc}")

        if not clicked:
            # Last resort: the button carries a stable id on this portal.
            try:
                await page.evaluate(
                    "() => { const b = document.getElementById('btnVerify');"
                    " if (b) b.click(); }"
                )
                clicked = True
                logger.info("monitor: clicked Verify Selection via #btnVerify")
            except Exception as exc:
                logger.warning(f"monitor: #btnVerify click failed: {exc}")
                return None
        else:
            logger.info("monitor: clicked 'Verify Selection'")

        if popup is not None:
            try:
                await popup.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            await utils.human_delay(self.config)
            return popup

        await self._settle(page)
        return page

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
        """Click the Submit revealed once the captcha has been verified.

        CONFIRMED markup:
            <button class="btn btn-primary" id="btnSubmit" type="submit">Submit</button>
        It is hidden until verification passes, then shown alongside a
        "Verified" badge. Submitting redirects to /Global/bls/visatype?data=...
        """
        clicked = False
        try:
            button = page.locator("#btnSubmit")
            await button.wait_for(state="visible", timeout=10000)
            await button.click(timeout=10000)
            clicked = True
            logger.info("monitor: clicked #btnSubmit")
        except Exception as exc:
            logger.debug(f"monitor: #btnSubmit not clickable ({exc}) — trying text")
            clicked = await utils.click_by_text(page, ("submit",), config=self.config)

        if not clicked:
            logger.warning("monitor: could not click the verification Submit")
            await utils.screenshot(page, "verified-submit-missing")
            return False

        # The form posts and redirects to the appointment selection page.
        try:
            await page.wait_for_url(
                lambda url: "/bls/visatype" in (url or "").lower()
                and "visatypeverification" not in (url or "").lower(),
                timeout=30000,
            )
            logger.success(f"monitor: reached the appointment form — {page.url}")
        except PlaywrightTimeout:
            logger.warning(
                f"monitor: no redirect to /bls/visatype within 30s — still at {page.url}"
            )
        await self._settle(page)
        await utils.screenshot(page, "visatype-form")
        await utils.dump_page_html(page, "visatype-form")
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

    async def _handle_portal_block(self, exc: Exception) -> None:
        """403 / blocked: wait out the cooldown, then log in again from scratch.

        Never stops the bot and never asks for a human. The stored session is
        discarded so the next attempt is a clean login rather than a replay of
        cookies the portal may already have invalidated.
        """
        wait_minutes = self.block_cooldown_minutes
        logger.error(f"monitor: portal blocked — {exc}")
        logger.warning(
            f"monitor: sleeping {wait_minutes:.0f} min, then logging in from scratch"
        )
        self.state.log_event(
            "bot", f"portal blocked — retrying in {wait_minutes:.0f} min", status="error"
        )
        for location in self.locations:
            self.state.set_location_status(
                location, STATUS_PAUSED, f"blocked — retry in {wait_minutes:.0f} min"
            )

        # Only alert the first time, so a long block does not spam Telegram.
        if not self._block_alerted:
            self._block_alerted = True
            await self.notifier.error(
                f"Portal is blocking us (HTTP 403). The bot will keep running and "
                f"retry automatically in {wait_minutes:.0f} minutes.",
                retry_info="no action needed — this is handled automatically",
            )

        await self._reset_session()
        await utils.interruptible_sleep(wait_minutes * 60, self.state)

    async def _handle_login_failure(self, exc: Exception) -> None:
        """Login failed for a non-block reason: back off, then try again.

        Backoff grows with consecutive failures so a persistent problem (wrong
        password, portal change) does not hammer the site, while a transient one
        recovers quickly.
        """
        self._login_failures += 1
        schedule = [2, 5, 10, 20, 30]
        idx = min(self._login_failures - 1, len(schedule) - 1)
        wait_minutes = schedule[idx]

        logger.error(
            f"monitor: login failed ({self._login_failures} in a row): {exc}"
        )
        logger.warning(f"monitor: retrying login in {wait_minutes} min")
        self.state.log_event(
            "bot",
            f"login failed x{self._login_failures} — retry in {wait_minutes} min",
            status="error",
        )
        for location in self.locations:
            self.state.set_location_status(
                location, STATUS_ERROR, f"login failed — retry in {wait_minutes} min"
            )

        # A stale session is a common cause; drop it after the first failure.
        if self._login_failures >= 1:
            await self._reset_session()

        # Beyond this many consecutive failures it is no longer a blip — wrong
        # credentials, a changed portal, something the bot cannot fix by waiting.
        # Escalate to a real pause so the manager is told, instead of retrying
        # quietly forever.
        if self._login_failures >= self.critical_login_failures:
            logger.critical(
                f"monitor: {self._login_failures} consecutive login failures — "
                "escalating to a manual pause"
            )
            self.state.set_manual_pause(
                f"CRITICAL: login failing repeatedly ({self._login_failures}x)"
            )
            self.state.log_event("bot", "CRITICAL: login failing repeatedly", status="error")
            await self.notifier.manual_required(
                f"CRITICAL: login has failed {self._login_failures} times in a row.\n\n"
                f"Last error: {exc}\n\n"
                "The bot cannot fix this by waiting. Likely causes: wrong "
                "password, account locked, or the portal changed. Check, then "
                "send /resume to continue."
            )
            await self.notifier.wait_for_resume()
            self.state.clear_manual_pause()
            self._login_failures = 0
            return

        if self._login_failures in (1, 3, 6):
            await self.notifier.error(
                f"Login failed {self._login_failures} time(s) in a row: {exc}",
                retry_info=f"retrying automatically in {wait_minutes} min",
            )

        await utils.interruptible_sleep(wait_minutes * 60, self.state)

    async def _reset_session(self) -> None:
        """Drop cookies and the browser so the next attempt is a clean login."""
        try:
            self.auth._clear_saved_session()
        except Exception as exc:
            logger.debug(f"monitor: could not clear the stored session: {exc}")
        try:
            await self.auth.stop()
            logger.info("monitor: browser closed — next cycle logs in fresh")
        except Exception as exc:
            logger.debug(f"monitor: could not close the browser: {exc}")

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
