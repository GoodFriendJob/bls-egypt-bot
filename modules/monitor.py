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
        """One full pass over every location."""
        page = await self.auth.ensure_logged_in()
        self.state.mark_cycle()

        for location in self.locations:
            if self.state.stop_requested or not self.state.running:
                return
            await self._check_location(page, location)
            await utils.human_delay(self.config)

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
