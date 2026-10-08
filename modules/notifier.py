"""Telegram alerts + command listener.

Alert types (per CLAUDE.md): SLOT_FOUND, MANUAL_REQUIRED, APPOINTMENT_CONFIRMED,
ERROR, STATUS. Commands: /resume, /status, /stop, /help.

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
        self.chat_id = str(telegram_cfg.get("chat_id") or "").strip()
        self.enabled = bool(self.token and self.chat_id and TELEGRAM_AVAILABLE)

        self._app: Any = None
        self._monitor: Any = None
        self._resume_event: asyncio.Event | None = None
        self._replies: asyncio.Queue[str] | None = None

        if not TELEGRAM_AVAILABLE:
            logger.warning("notifier: python-telegram-bot not installed — log-only mode")
        elif not self.enabled:
            logger.warning("notifier: telegram token/chat_id missing — log-only mode")

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
            self._app.add_handler(CommandHandler("start", self._cmd_help))
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
        try:
            await self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.warning(f"notifier: send failed: {exc}")
            return False

    async def send_photo(self, path: str | Path, caption: str = "") -> bool:
        if not self.enabled or self._app is None:
            logger.info(f"notifier (log-only) photo {path}: {caption}")
            return False
        file_path = Path(path)
        if not file_path.exists():
            return await self.send(caption)
        try:
            with file_path.open("rb") as handle:
                await self._app.bot.send_photo(
                    chat_id=self.chat_id, photo=handle, caption=caption[:1024]
                )
            return True
        except Exception as exc:
            logger.warning(f"notifier: photo send failed: {exc}")
            return await self.send(caption)

    # ------------------------------------------------------------------ #
    # Alert types
    # ------------------------------------------------------------------ #
    async def slot_found(self, location: str, date: str = "", time: str = "") -> bool:
        detail = " ".join(part for part in (date, time) if part) or "details on the page"
        return await self.send(
            f"🟢 SLOT FOUND\nLocation: {location.title()}\nWhen: {detail}\n\nBooking now…"
        )

    async def manual_required(self, reason: str, *, screenshot: str | None = None) -> bool:
        text = (
            f"🟠 MANUAL STEP REQUIRED\n{reason}\n\n"
            "The bot is paused. Finish the step in the browser, then send /resume."
        )
        if screenshot:
            return await self.send_photo(screenshot, text)
        return await self.send(text)

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

    async def startup_notice(self) -> bool:
        bls = self.config.get("bls", {}) or {}
        locations = ", ".join(loc.title() for loc in bls.get("locations", []))
        return await self.send(
            "🤖 Bot started\n"
            f"Locations: {locations}\n"
            f"Visa type: {bls.get('visa_type', '')}\n"
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
    def _authorised(self, update: Any) -> bool:
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            return False
        if str(chat.id) != self.chat_id:
            logger.warning(f"notifier: ignoring command from unauthorised chat {chat.id}")
            return False
        return True

    async def _cmd_help(self, update: Any, _context: Any) -> None:
        if self._authorised(update):
            await update.message.reply_text(HELP_TEXT)

    async def _cmd_status(self, update: Any, _context: Any) -> None:
        if not self._authorised(update):
            return
        text = self.state.status_text() if self.state is not None else "no state available"
        await update.message.reply_text(text)

    async def _cmd_resume(self, update: Any, _context: Any) -> None:
        if not self._authorised(update):
            return
        if self.state is not None:
            self.state.clear_manual_pause()
        if self._resume_event is not None:
            self._resume_event.set()
        logger.info("notifier: /resume command received")
        await update.message.reply_text("▶️ Resuming.")

    async def _cmd_stop(self, update: Any, _context: Any) -> None:
        if not self._authorised(update):
            return
        logger.info("notifier: /stop command received")
        if self.state is not None:
            self.state.request_stop()
        if self._resume_event is not None:
            self._resume_event.set()  # unblock any manual wait so we can exit
        await update.message.reply_text("🛑 Stopping the bot…")

    async def _on_text(self, update: Any, _context: Any) -> None:
        """Queue free text so otp.py can consume a code sent by the user."""
        if not self._authorised(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        if self._replies is not None:
            await self._replies.put(text)
        logger.debug("notifier: queued a text reply")
