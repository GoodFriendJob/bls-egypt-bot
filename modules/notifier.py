"""Telegram alerts + command listener.

Alert types (per CLAUDE.md): SLOT_FOUND, MANUAL_REQUIRED, APPOINTMENT_CONFIRMED,
ERROR, STATUS. Commands: /start, /resume, /status, /stop, /help.

telegram.chat_id may name one chat or a list of them: the operator and the
applicant are not always the same person, and a command from a chat that is
not listed is refused with its own id so it can be added.

If the Telegram token or chat id is missing the notifier degrades to log-only
mode so the bot still runs — it never raises for a missing credential.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    TELEGRAM_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency missing
    TELEGRAM_AVAILABLE = False
    Application = CommandHandler = MessageHandler = filters = None  # type: ignore

HELP_TEXT = (
    "BLS appointment bot commands:\n"
    "/start — begin or resume monitoring\n"
    "/status — current monitoring status\n"
    "/resume — continue after a manual step\n"
    "/stop — stop the bot gracefully\n"
    "/help — this message"
)


class Notifier:
    """Outbound alerts and inbound commands over Telegram."""

    def __init__(self, config: dict[str, Any], state: Any) -> None:
        self.config = config
        self.state = state

        telegram_cfg = config.get("telegram", {}) or {}
        self.token = (telegram_cfg.get("token") or "").strip()
        # chat_id may name ONE chat or several. The bot is used by more than
        # one person -- whoever runs it and whoever the appointment is for --
        # and with a single id every command from the other one was dropped
        # without a word, which looks exactly like a bot that is not running.
        self.chat_ids = self._as_chat_ids(telegram_cfg.get("chat_id"))
        # Alerts go to the first; it is the one the operator configured.
        self.chat_id = self.chat_ids[0] if self.chat_ids else ""
        self.enabled = bool(self.token and self.chat_ids and TELEGRAM_AVAILABLE)
        # Chats told once that they are not on the list, so a stranger cannot
        # make the bot chatter by spamming it.
        self._told_unauthorised: set[str] = set()

        self._app: Any = None
        self._monitor: Any = None
        self._resume_event: asyncio.Event | None = None
        self._replies: asyncio.Queue[str] | None = None

        if not TELEGRAM_AVAILABLE:
            logger.warning("notifier: python-telegram-bot not installed — log-only mode")
        elif not self.enabled:
            logger.warning("notifier: telegram token/chat_id missing — log-only mode")

    @staticmethod
    def _as_chat_ids(value: Any) -> list[str]:
        """One chat id, or several, however the config spells it."""
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        out: list[str] = []
        for item in items:
            # YAML reads a bare number as an int; ids are compared as strings.
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out

    def bind(self, *, monitor: Any = None) -> None:
        """Attach collaborators that commands need to drive."""
        if monitor is not None:
            self._monitor = monitor

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._resume_event = asyncio.Event()
        self._replies = asyncio.Queue()
        if not self.enabled:
            return

        try:
            self._app = Application.builder().token(self.token).build()
            self._app.add_handler(CommandHandler("start", self._cmd_start))
            self._app.add_handler(CommandHandler("help", self._cmd_help))
            self._app.add_handler(CommandHandler("status", self._cmd_status))
            self._app.add_handler(CommandHandler("resume", self._cmd_resume))
            self._app.add_handler(CommandHandler("stop", self._cmd_stop))
            self._app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
            )
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("notifier: telegram listener started")
        except Exception as exc:
            logger.error(f"notifier: telegram startup failed ({exc}) — log-only mode")
            self.enabled = False
            self._app = None

    def listening(self) -> bool:
        """Is the command listener actually polling right now?"""
        if not self.enabled or self._app is None:
            return False
        try:
            updater = self._app.updater
            return bool(updater is not None and updater.running)
        except Exception:
            return False

    async def ensure_listening(self) -> bool:
        """Restart the Telegram listener if its polling has died.

        python-telegram-bot retries a failed getUpdates for a while and then
        gives up for good. A DNS outage on the host is enough -- run
        20261018_201235 filled the log with

            telegram.error.NetworkError: httpx.ConnectError:
            [Errno 11001] getaddrinfo failed

        and after that nothing reached the bot. For something meant to run
        unattended that is worse than the outage: every alert is lost and
        /resume cannot be delivered, so a booking waiting on a manual step
        waits forever.

        Returns True if it is listening when this returns.
        """
        if not self.enabled:
            return False
        if self.listening():
            return True

        logger.warning("notifier: the telegram listener has stopped — restarting it")
        try:
            await self.stop()
        except Exception as exc:
            logger.debug(f"notifier: shutdown before restart failed: {exc}")

        # start() rebuilds the Application and re-registers every handler.
        # enabled is cleared by start() if it fails, which would stop this
        # trying forever, so it is restored first.
        self.enabled = bool(self.token and self.chat_ids and TELEGRAM_AVAILABLE)
        await self.start()
        if self.listening():
            logger.success("notifier: telegram listener is back")
            return True
        logger.warning("notifier: telegram listener could not be restarted yet")
        return False

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("notifier: telegram listener stopped")
        except Exception as exc:
            logger.debug(f"notifier: shutdown error: {exc}")
        finally:
            self._app = None

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #
    async def send(self, text: str) -> bool:
        if not self.enabled or self._app is None:
            logger.info(f"notifier (log-only): {text}")
            return False
        sent = False
        for chat_id in self.chat_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=True,
                )
                sent = True
            except Exception as exc:
                # One unreachable chat must not silence the others.
                logger.warning(f"notifier: send to {chat_id} failed: {exc}")
        return sent

    async def send_photo(self, path: str | Path, caption: str = "") -> bool:
        if not self.enabled or self._app is None:
            logger.info(f"notifier (log-only) photo {path}: {caption}")
            return False
        file_path = Path(path)
        if not file_path.exists():
            return await self.send(caption)
        sent = False
        for chat_id in self.chat_ids:
            try:
                with file_path.open("rb") as handle:
                    await self._app.bot.send_photo(
                        chat_id=chat_id, photo=handle, caption=caption[:1024]
                    )
                sent = True
            except Exception as exc:
                logger.warning(f"notifier: photo send to {chat_id} failed: {exc}")
        if not sent:
            return await self.send(caption)
        return True

    # ------------------------------------------------------------------ #
    # Alert types
    # ------------------------------------------------------------------ #
    async def slot_found(
        self,
        location: str,
        date: str = "",
        time: str = "",
        *,
        selection: str = "",
        combo: dict[str, Any] | None = None,
    ) -> bool:
        """Announce an opening, naming the combination field by field.

        ``combo`` is the swept combination as build_combinations makes it, and
        each of its fields gets its own line: with a sweep running, a single
        slash-separated string is hard to read at a glance on a phone, and the
        city alone does not say which visa type actually opened up.

        ``selection`` is the same thing pre-formatted, kept for the caller that
        has only the text.
        """
        lines = ["🟢 Slot Found", f"Location: {(combo or {}).get('location') or location.title()}"]
        if combo:
            for label, key in (
                ("Category", "appointment_category"),
                ("Appointment For", "appointment_for"),
                ("Visa type", "visa_type"),
                ("Visa sub type", "visa_sub_type"),
            ):
                value = str(combo.get(key, "") or "").strip()
                if value:
                    lines.append(f"{label}: {value}")
        elif selection:
            lines.append(f"Selection: {selection}")

        # Only when it is actually known. On detection it usually is not: the
        # date and slot are chosen on the page that follows.
        when = " ".join(part for part in (date, time) if part).strip()
        if when:
            lines.append(f"When: {when}")

        return await self.send("\n".join(lines) + "\n\nBooking now…")

    async def manual_required(self, reason: str, *, screenshot: str | None = None) -> bool:
        text = (
            f"🟠 MANUAL STEP REQUIRED\n{reason}\n\n"
            "The bot is paused. Finish the step in the browser, then send /resume."
        )
        if screenshot:
            return await self.send_photo(screenshot, text)
        return await self.send(text)

    async def application_submitted(
        self,
        booked: dict[str, Any] | None = None,
        *,
        next_step: str = "",
    ) -> bool:
        """Confirm the applicant form went through, and summarise what went in.

        This is the message that matters most: it is the record of what was
        actually submitted on the applicant's behalf, so it reads back the
        appointment and the details that were filled rather than just saying
        "submitted". ``next_step`` names what the booking is waiting on.
        """
        applicant = self.config.get("applicant", {}) or {}
        combo = (booked or {}).get("combo") or {}

        def get(*keys: str) -> str:
            for key in keys:
                value = str(applicant.get(key, "") or "").strip()
                if value:
                    return value
            return ""

        lines = [
            "✅ Application submitted",
            "",
            "The applicant form was accepted and the appointment is held.",
        ]

        name = " ".join(p for p in (get("first_name"), get("surname")) if p)
        who = [
            ("Name", name or get("full_name")),
            ("Date of birth", get("dob")),
            ("Nationality", get("nationality")),
            ("Passport", " · ".join(
                p for p in (get("passport_number"), get("passport_type")) if p
            )),
        ]
        lines += ["", "👤 APPLICANT"] + [
            f"{label}: {value}" for label, value in who if value
        ]

        when = " at ".join(
            p for p in ((booked or {}).get("date"), (booked or {}).get("slot")) if p
        )
        visa = " · ".join(
            p for p in (combo.get("visa_type"), combo.get("visa_sub_type")) if p
        )
        appointment = [
            ("Centre", str(combo.get("location", "") or "")),
            ("Date & time", when),
            ("Visa", visa),
            ("Category", str(combo.get("appointment_category", "") or "")),
            ("Booked for", str(combo.get("appointment_for", "") or "")),
        ]
        lines += ["", "📅 APPOINTMENT"] + [
            f"{label}: {value}" for label, value in appointment if value
        ]

        stay = get("intended_stay_days")
        trip = [
            ("Purpose", get("purpose_of_journey")),
            ("Destination", get("destination_state")),
            ("Travel date", get("travel_date")),
            ("Arrival → departure", " → ".join(
                p for p in (get("arrival_date"), get("departure_date")) if p
            )),
            ("Length of stay", f"{stay} day(s)" if stay else ""),
            ("Entries", get("number_of_entries")),
            ("Staying at", " · ".join(
                p for p in (get("hotel_name"), get("hotel_city")) if p
            )),
        ]
        lines += ["", "✈️ TRIP"] + [
            f"{label}: {value}" for label, value in trip if value
        ]

        if next_step:
            lines += ["", f"➡️ NEXT: {next_step}"]

        return await self.send("\n".join(lines))

    async def appointment_confirmed(self, details: dict[str, Any]) -> bool:
        lines = "\n".join(f"{key}: {value}" for key, value in details.items() if value)
        return await self.send(f"✅ APPOINTMENT CONFIRMED\n{lines or 'see portal for details'}")

    async def error(self, message: str, *, retry_info: str = "") -> bool:
        text = f"🔴 ERROR\n{message}"
        if retry_info:
            text += f"\n\nNext: {retry_info}"
        return await self.send(text)

    async def status_heartbeat(self, status_text: str) -> bool:
        return await self.send(f"ℹ️ STATUS (heartbeat)\n\n{status_text}")

    @staticmethod
    def _options(value: Any, *, title: bool = False) -> str:
        """Render a config field that may be a scalar or a list, always as a list.

        Every appointment field can be swept, so showing them uniformly makes
        it obvious at a glance how wide the search actually is.
        """
        if value is None:
            items: list[str] = []
        elif isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
        else:
            items = [str(value).strip()] if str(value).strip() else []
        if title:
            items = [i.title() for i in items]
        return "[" + ", ".join(f"'{i}'" for i in items) + "]"

    async def startup_notice(self) -> bool:
        """Announce the run, listing every option the sweep will cover."""
        bls = self.config.get("bls", {}) or {}
        return await self.send(
            "🤖 Bot started\n"
            f"Locations: {self._options(bls.get('locations'), title=True)},\n"
            f"Category: {self._options(bls.get('appointment_category'))}\n"
            f"Appointment For: {self._options(bls.get('appointment_for'))},\n"
            f"Visa type: {self._options(bls.get('visa_type'))},\n"
            f"Visa sub type: {self._options(bls.get('visa_sub_type'))}\n"
            f"Interval: {self.config.get('poll_interval', 90)}s\n\n"
            "Send /help for commands."
        )

    async def shutdown_notice(self) -> bool:
        return await self.send("🛑 Bot stopped.")

    # ------------------------------------------------------------------ #
    # Waiting on the user
    # ------------------------------------------------------------------ #
    async def wait_for_resume(self, timeout: float | None = None) -> bool:
        """Block until /resume arrives (or the bot is stopped). True = resumed."""
        if self._resume_event is None:
            self._resume_event = asyncio.Event()
        self._resume_event.clear()

        if not self.enabled:
            logger.warning(
                "notifier: no telegram link — cannot wait for /resume, continuing immediately"
            )
            return False

        logger.info("notifier: waiting for /resume…")
        while True:
            try:
                await asyncio.wait_for(self._resume_event.wait(), timeout=timeout or 30)
                logger.info("notifier: /resume received")
                return True
            except asyncio.TimeoutError:
                if timeout is not None:
                    logger.warning("notifier: /resume wait timed out")
                    return False
                if self.state is not None and self.state.stop_requested:
                    return False
                # No overall timeout — keep waiting in 30s slices.

    async def wait_for_reply(self, timeout: float = 300) -> str | None:
        """Wait for a plain-text Telegram message (used for SMS OTP codes)."""
        if self._replies is None:
            self._replies = asyncio.Queue()
        while not self._replies.empty():  # drop anything stale
            self._replies.get_nowait()
        if not self.enabled:
            return None
        try:
            return await asyncio.wait_for(self._replies.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"notifier: no reply within {timeout}s")
            return None

    # ------------------------------------------------------------------ #
    # Command handlers
    # ------------------------------------------------------------------ #
    async def _authorised(self, update: Any) -> bool:
        """Is this chat allowed to drive the bot? Says so if not.

        It used to return False in silence. Someone added the bot, typed
        /start, saw nothing at all, and reasonably concluded the bot was not
        running -- while the log said "ignoring command from unauthorised chat
        7340367798" on a machine they could not see. Telling them their own
        chat id turns that into a thirty-second fix.
        """
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            return False
        chat_id = str(chat.id)
        if chat_id in self.chat_ids:
            return True

        logger.warning(
            f"notifier: ignoring a command from unauthorised chat {chat_id} — "
            "add it to telegram.chat_id in config.yaml to allow it"
        )
        # Once per chat per run, so this cannot be used to make the bot talk.
        if chat_id not in self._told_unauthorised:
            self._told_unauthorised.add(chat_id)
            try:
                await update.message.reply_text(
                    "This chat is not on the bot's allowed list, so its "
                    "commands are ignored.\n\n"
                    f"Your chat id is: {chat_id}\n\n"
                    "Send that to whoever runs the bot and ask them to add it "
                    "to telegram.chat_id in config.yaml (it accepts a list)."
                )
            except Exception as exc:
                logger.debug(f"notifier: could not reply to {chat_id}: {exc}")
        return False

    async def _cmd_help(self, update: Any, _context: Any) -> None:
        if await self._authorised(update):
            await update.message.reply_text(HELP_TEXT)

    async def _cmd_start(self, update: Any, _context: Any) -> None:
        """/start — begin (or resume) monitoring.

        This is the first thing anyone types to a Telegram bot, so it has to
        do the obvious thing rather than print a help page.
        """
        if not await self._authorised(update):
            return
        logger.info("notifier: /start command received")
        if self.state is None:
            await update.message.reply_text("No bot state to start.")
            return

        was_running = bool(getattr(self.state, "running", False))
        self.state.request_start()
        if was_running:
            await update.message.reply_text(
                "▶️ Already monitoring — carrying on.\n\n" + HELP_TEXT
            )
        else:
            await update.message.reply_text(
                "▶️ Monitoring started. You will get a message the moment a "
                "slot appears.\n\n" + HELP_TEXT
            )

    async def _cmd_status(self, update: Any, _context: Any) -> None:
        if not await self._authorised(update):
            return
        text = self.state.status_text() if self.state is not None else "no state available"
        await update.message.reply_text(text)

    async def _cmd_resume(self, update: Any, _context: Any) -> None:
        if not await self._authorised(update):
            return
        if self.state is not None:
            self.state.clear_manual_pause()
        if self._resume_event is not None:
            self._resume_event.set()
        logger.info("notifier: /resume command received")
        await update.message.reply_text("▶️ Resuming.")

    async def _cmd_stop(self, update: Any, _context: Any) -> None:
        if not await self._authorised(update):
            return
        logger.info("notifier: /stop command received")
        if self.state is not None:
            self.state.request_stop()
        if self._resume_event is not None:
            self._resume_event.set()  # unblock any manual wait so we can exit
        await update.message.reply_text("🛑 Stopping the bot…")

    async def _on_text(self, update: Any, _context: Any) -> None:
        """Queue free text so otp.py can consume a code sent by the user."""
        if not await self._authorised(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        if self._replies is not None:
            await self._replies.put(text)
        logger.debug("notifier: queued a text reply")
