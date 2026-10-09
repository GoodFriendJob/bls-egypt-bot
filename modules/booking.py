"""Slot selection and applicant form filling.

Only invoked by monitor.py once a slot has actually been detected.

TODO(unconfirmed — needs live portal inspection): per CLAUDE.md the appointment
page structure is still TBD. Everything below is written against the *documented*
flow (location → visa type → calendar slot → applicant form → documents → OTP →
confirm) using label/text-based lookups rather than ids. After the first
successful login, capture and record:
  * the appointment page URL (``bls.paths.appointment``)
  * the location selector — ``<select>`` or custom dropdown? option labels?
  * the visa-type selector and its exact option text
  * the calendar/slot widget — table cells, buttons, or a JS datepicker? how is
    an *available* day marked vs a disabled one?
  * the applicant form field labels, and whether honeypots appear here too
  * the confirmation page markers and the reference-number element
Then replace the heuristics in ``_select_location``, ``_select_visa_type`` and
``_pick_slot`` with the confirmed selectors.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from modules import documents, otp, utils
from modules.state import STATUS_BOOKED, STATUS_BOOKING, STATUS_ERROR

# Slot cells that are *not* bookable usually carry one of these markers.
DISABLED_HINTS = ("disabled", "inactive", "unavailable", "booked", "full", "off", "closed")
# ...and bookable ones one of these.
AVAILABLE_HINTS = ("available", "active", "free", "open", "enabled", "has-slot")

SLOT_SELECTORS = (
    "td.available",
    "td[class*='available']",
    "button[class*='available']",
    "a[class*='available']",
    ".slot:not(.disabled)",
    "[data-slot]:not([disabled])",
    "td[onclick]",
)

CONFIRM_BUTTON_TEXTS = ("confirm", "book", "submit", "finish", "complete", "تأكيد", "حجز")
NEXT_BUTTON_TEXTS = ("next", "continue", "proceed", "save and continue", "التالي", "متابعة")
SUCCESS_PHRASES = (
    "appointment confirmed",
    "successfully booked",
    "booking confirmed",
    "your appointment",
    "reference number",
    "confirmation number",
    "تم تأكيد",
)
REFERENCE_REGEX = re.compile(r"(?:ref(?:erence)?|confirmation)\D{0,20}([A-Z0-9-]{5,})", re.I)

# Applicant config key → words likely to appear in that field's label.
FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "full_name": ("full name", "name", "applicant name", "الاسم"),
    "passport_number": ("passport number", "passport no", "passport", "جواز"),
    "nationality": ("nationality", "citizenship", "الجنسية"),
    "dob": ("date of birth", "birth date", "dob", "تاريخ الميلاد"),
    "phone": ("phone", "mobile", "contact number", "هاتف"),
    "email": ("email", "e-mail", "بريد"),
    "passport_issue_date": ("issue date", "date of issue"),
    "passport_expiry_date": ("expiry", "expiration", "valid until"),
    "gender": ("gender", "sex", "النوع"),
    "place_of_birth": ("place of birth", "birth place"),
    "address": ("address", "street", "العنوان"),
    "city": ("city", "town", "المدينة"),
}


class BookingError(RuntimeError):
    """Raised when the booking flow cannot be completed."""


class Booking:
    """Drives one booking attempt from slot click to confirmation."""

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

    async def book(self, page: Any, location: str, slot_hint: str = "") -> dict[str, Any] | None:
        """Attempt a booking for ``location``. Returns appointment details or None."""
        self._set_status(location, STATUS_BOOKING, "booking started")
        self._log(location, f"booking flow started ({slot_hint or 'slot detected'})")

        try:
            await self._select_location(page, location)
            await self._select_visa_type(page)

            slot = await self._pick_slot(page)
            if slot is None:
                self._log(location, "slot vanished before it could be selected", status="warn")
                self._set_status(location, STATUS_ERROR, "slot vanished")
                return None

            await self._fill_applicant_form(page)
            await documents.upload_documents(
                page, self.config, state=self.state, notifier=self.notifier
            )

            await self._advance(page)
            await self._handle_liveness(page, location)
            await self._handle_payment(page, location)

            if await otp.is_otp_prompt(page, self.config):
                await otp.handle_otp(
                    page, self.config, state=self.state, notifier=self.notifier
                )

            await self._confirm(page)
            details = await self._read_confirmation(page, location, slot)

            if details is None:
                shot = await utils.screenshot(page, f"booking-unconfirmed-{location}")
                self._log(location, "booking submitted but no confirmation found", status="warn")
                self._set_status(location, STATUS_ERROR, "confirmation not detected")
                if self.notifier is not None:
                    await self.notifier.manual_required(
                        f"Booking for {location.title()} was submitted but the bot could not "
                        "read a confirmation. Check the portal, then send /resume.",
                        screenshot=shot,
                    )
                    await self.notifier.wait_for_resume()
                return None

            self._set_status(location, STATUS_BOOKED, "appointment confirmed")
            self._log(location, f"appointment confirmed: {details}", status="ok")
            if self.state is not None:
                self.state.mark_booked(details)
            if self.notifier is not None:
                await self.notifier.appointment_confirmed(details)
            return details

        except Exception as exc:
            logger.exception(f"booking: failed for {location}: {exc}")
            shot = await utils.screenshot(page, f"booking-error-{location}")
            self._set_status(location, STATUS_ERROR, str(exc))
            self._log(location, f"booking failed: {exc}", status="error")
            if self.notifier is not None:
                await self.notifier.error(
                    f"Booking for {location.title()} failed: {exc}",
                    retry_info="monitoring continues — will retry on the next slot",
                )
                if shot:
                    await self.notifier.send_photo(shot, f"Booking error — {location.title()}")
            return None

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    async def _select_location(self, page: Any, location: str) -> None:
        """Pick the city. TODO: replace with the confirmed selector."""
        labels = (self.config.get("bls", {}) or {}).get("location_labels", {}) or {}
        wanted = labels.get(location, location.title())

        for select in await utils.visible_elements(page, "select"):
            context = await self._select_context(select)
            if any(hint in context for hint in ("location", "city", "centre", "center", "مدينة")):
                chosen = await utils.select_option_like(select, wanted)
                if chosen:
                    logger.info(f"booking: location set to {chosen!r}")
                    await utils.human_delay(self.config)
                    return

        # Fallback: a radio/button/link carrying the city name.
        if await utils.click_by_text(page, (wanted,), config=self.config):
            logger.info(f"booking: location {wanted!r} selected by text")
            await utils.human_delay(self.config)
            return

        logger.warning(f"booking: could not set location {wanted!r} — continuing")

    async def _select_visa_type(self, page: Any) -> None:
        """Pick the visa category. TODO: replace with the confirmed selector."""
        wanted = (self.config.get("bls", {}) or {}).get("visa_type", "")
        if not wanted:
            return

        for select in await utils.visible_elements(page, "select"):
            context = await self._select_context(select)
            if any(hint in context for hint in ("visa", "category", "type", "purpose", "تأشيرة")):
                chosen = await utils.select_option_like(select, wanted)
                if chosen:
                    logger.info(f"booking: visa type set to {chosen!r}")
                    await utils.human_delay(self.config)
                    return

        if await utils.click_by_text(page, (wanted,), config=self.config):
            logger.info(f"booking: visa type {wanted!r} selected by text")
            await utils.human_delay(self.config)
            return

        logger.warning(f"booking: could not set visa type {wanted!r} — continuing")

    async def _select_context(self, element: Any) -> str:
        try:
            return (
                await element.evaluate(
                    """
                    (el) => {
                      const bits = [el.name || '', el.id || ''];
                      if (el.id) {
                        const lab = document.querySelector(`label[for="${el.id}"]`);
                        if (lab) bits.push(lab.innerText || '');
                      }
                      const wrap = el.closest('.form-group, label, div, td, tr');
                      if (wrap) bits.push((wrap.innerText || '').slice(0, 160));
                      return bits.join(' ');
                    }
                    """
                )
                or ""
            ).lower()
        except Exception:
            return ""

    async def _pick_slot(self, page: Any) -> str | None:
        """Click the first bookable slot. Returns a label describing it.

        TODO(unconfirmed): the calendar widget is unknown. This tries common
        "available" class patterns and then any enabled-looking day cell.
        """
        for selector in SLOT_SELECTORS:
            for element in await utils.visible_elements(page, selector):
                marker = await self._slot_marker(element)
                if any(hint in marker for hint in DISABLED_HINTS):
                    continue
                label = (await utils.element_text(element)).strip() or "slot"
                try:
                    await utils.human_delay(self.config, scale=0.5)
                    await element.click()
                    logger.info(f"booking: clicked slot {label!r} via {selector}")
                    await self._settle(page)
                    return label
                except Exception as exc:
                    logger.debug(f"booking: slot click failed ({selector}): {exc}")

        # Broader sweep: any day cell that advertises availability.
        for element in await utils.visible_elements(page, "td, button, a"):
            marker = await self._slot_marker(element)
            if not any(hint in marker for hint in AVAILABLE_HINTS):
                continue
            if any(hint in marker for hint in DISABLED_HINTS):
                continue
            label = (await utils.element_text(element)).strip() or "slot"
            try:
                await element.click()
                logger.info(f"booking: clicked slot {label!r} via availability sweep")
                await self._settle(page)
                return label
            except Exception as exc:
                logger.debug(f"booking: sweep click failed: {exc}")
        return None

    async def _slot_marker(self, element: Any) -> str:
        try:
            return (
                await element.evaluate(
                    "(el) => [el.className, el.getAttribute('aria-disabled') || '', "
                    "el.disabled ? 'disabled' : '', el.title || ''].join(' ')"
                )
                or ""
            ).lower()
        except Exception:
            return ""

    async def _fill_applicant_form(self, page: Any) -> None:
        """Fill every applicant field whose label we can recognise."""
        applicant = self.config.get("applicant", {}) or {}
        filled = 0

        for key, hints in FIELD_HINTS.items():
            value = applicant.get(key)
            if not value:
                continue

            # Dropdown first (gender/nationality are often selects).
            handled = False
            for select in await utils.visible_elements(page, "select"):
                context = await self._select_context(select)
                if any(hint in context for hint in hints):
                    if await utils.select_option_like(select, str(value)):
                        logger.info(f"booking: {key} selected from dropdown")
                        handled = True
                        filled += 1
                        break
            if handled:
                await utils.human_delay(self.config, scale=0.4)
                continue

            element = await utils.find_visible_input(
                page, kinds=("text", "email", "tel", "date", "number"), keywords=hints
            )
            if element is None:
                logger.debug(f"booking: no field found for {key}")
                continue
            try:
                await utils.type_like_human(element, str(value))
                filled += 1
                logger.info(f"booking: filled {key}")
            except Exception as exc:
                logger.warning(f"booking: could not fill {key}: {exc}")
            await utils.human_delay(self.config, scale=0.4)

        self._log("form", f"applicant form filled ({filled} field(s))", status="ok")

    async def _advance(self, page: Any) -> None:
        """Click a Next/Continue button if the form is multi-step."""
        if await utils.click_by_text(page, NEXT_BUTTON_TEXTS, config=self.config):
            logger.info("booking: advanced to the next step")
            await self._settle(page)

    async def _handle_liveness(self, page: Any, location: str) -> None:
        """Pause for facial/liveness verification and wait for /resume."""
        phrases = (self.config.get("detection", {}) or {}).get("liveness_phrases", [])
        hit = utils.contains_any(await utils.page_text(page), phrases)
        if not hit:
            return

        shot = await utils.screenshot(page, f"liveness-{location}")
        logger.warning(f"booking: liveness step detected ({hit!r}) — pausing")
        self._log(location, f"liveness/facial verification required ({hit})", status="warn")
        if self.state is not None:
            self.state.set_manual_pause("liveness / facial verification")
        if self.notifier is not None:
            await self.notifier.manual_required(
                f"{location.title()}: the portal is asking for liveness/facial verification "
                f"(matched {hit!r}). Complete it in the browser window, then send /resume.",
                screenshot=shot,
            )
            await self.notifier.wait_for_resume()
        if self.state is not None:
            self.state.clear_manual_pause()
        await self._settle(page)

    async def _handle_payment(self, page: Any, location: str) -> None:
        """Stop and hand over before anything involving money.

        The bot must never confirm a payment or enter card details by itself:
        that is the manager's decision. This pause is deliberate and waits
        indefinitely for /resume — unlike the login/403 failures, which recover
        automatically.
        """
        phrases = (self.config.get("detection", {}) or {}).get("payment_phrases", [])
        hit = utils.contains_any(await utils.page_text(page), phrases)
        if not hit:
            return

        shot = await utils.screenshot(page, f"payment-{location}")
        logger.warning(f"booking: payment step detected ({hit!r}) — handing over")
        self._log(location, f"payment confirmation required ({hit})", status="warn")
        if self.state is not None:
            self.state.set_manual_pause("payment confirmation")
        if self.notifier is not None:
            await self.notifier.manual_required(
                f"{location.title()}: the booking has reached a PAYMENT step "
                f"(matched {hit!r}). The bot will not pay or enter card details. "
                "Complete it in the browser, then send /resume.",
                screenshot=shot,
            )
            await self.notifier.wait_for_resume()
        if self.state is not None:
            self.state.clear_manual_pause()
        await self._settle(page)

    async def _confirm(self, page: Any) -> None:
        if await utils.click_by_text(page, CONFIRM_BUTTON_TEXTS, config=self.config):
            logger.info("booking: confirmation submitted")
        else:
            logger.warning("booking: no confirm button found — pressing Enter")
            await page.keyboard.press("Enter")
        await self._settle(page)

    async def _read_confirmation(
        self, page: Any, location: str, slot: str
    ) -> dict[str, Any] | None:
        """Scrape the confirmation page. TODO: use the confirmed markers."""
        text = await utils.page_text(page)
        hit = utils.contains_any(text, SUCCESS_PHRASES)
        if not hit:
            return None

        match = REFERENCE_REGEX.search(text)
        shot = await utils.screenshot(page, f"booking-confirmed-{location}")
        return {
            "location": location.title(),
            "visa_type": (self.config.get("bls", {}) or {}).get("visa_type", ""),
            "slot": slot,
            "applicant": (self.config.get("applicant", {}) or {}).get("full_name", ""),
            "reference": match.group(1) if match else "see portal",
            "matched_phrase": hit,
            "screenshot": shot or "",
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            logger.debug("booking: networkidle timed out — continuing")
        await utils.human_delay(self.config, scale=0.5)

    def _set_status(self, location: str, status: str, detail: str) -> None:
        if self.state is not None:
            self.state.set_location_status(location, status, detail)

    def _log(self, location: str, message: str, *, status: str = "info") -> None:
        logger.info(f"booking[{location}]: {message}")
        if self.state is not None:
            self.state.log_event(location, message, status=status)
