"""OTP handling — IMAP auto-read for email, Telegram reply fallback for SMS."""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import time
from email.header import decode_header
from typing import Any

from loguru import logger

from modules import utils

OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
OTP_FIELD_KEYWORDS = ("otp", "code", "verification", "pin", "رمز")
OTP_PROMPT_PHRASES = (
    "otp",
    "one time password",
    "one-time password",
    "verification code",
    "enter the code",
    "رمز التحقق",
)
OTP_SUBMIT_TEXTS = ("submit", "verify", "confirm", "continue", "تحقق", "تأكيد")


class OtpError(RuntimeError):
    """Raised when no OTP could be obtained or entered."""


async def is_otp_prompt(page: Any, config: dict[str, Any]) -> bool:
    """True when the current page is asking for an OTP."""
    text = await utils.page_text(page)
    if utils.contains_any(text, OTP_PROMPT_PHRASES):
        return True
    field = await utils.find_visible_input(
        page, kinds=("text", "tel", "number"), keywords=OTP_FIELD_KEYWORDS
    )
    return field is not None


async def handle_otp(
    page: Any,
    config: dict[str, Any],
    *,
    state: Any = None,
    notifier: Any = None,
) -> bool:
    """Obtain an OTP and type it into the page. Returns True on success.

    TODO(unconfirmed — needs live portal inspection): the OTP screen has not been
    observed. Confirm whether the code box follows the same honeypot pattern as
    the login form (10 inputs, only index 0 real), and whether the code is split
    across several single-character boxes — if so, ``_fill_code`` must distribute
    digits across them instead of filling one field.
    """
    method = ((config.get("otp") or {}).get("method") or "email").lower()
    timeout = float((config.get("otp") or {}).get("timeout", 300))
    _log(state, f"OTP required — method={method}")

    deadline = time.monotonic() + timeout
    try:
        if method == "email":
            code = await _code_from_email(config, deadline)
        else:
            code = await _code_from_telegram(config, notifier, state, timeout)
    except Exception as exc:
        logger.exception(f"otp: retrieval failed: {exc}")
        code = None

    if not code:
        message = f"Could not obtain the OTP within {int(timeout)}s (method={method})"
        _log(state, message, status="error")
        if notifier is not None:
            await notifier.error(message, retry_info="booking aborted — will retry next cycle")
        raise OtpError(message)

    logger.info(f"otp: got code {code[:2]}{'*' * (len(code) - 2)}")
    if not await _fill_code(page, code, config):
        message = "OTP code obtained but no input field was found on the page"
        _log(state, message, status="error")
        if notifier is not None:
            await notifier.error(message, retry_info="manual entry required")
        raise OtpError(message)

    _log(state, "OTP submitted", status="ok")
    return True


# --------------------------------------------------------------------------- #
# Email (IMAP)
# --------------------------------------------------------------------------- #
async def _code_from_email(config: dict[str, Any], deadline: float) -> str | None:
    """Poll the IMAP inbox until the BLS OTP mail arrives."""
    otp_cfg = config.get("otp", {}) or {}
    attempts = int(otp_cfg.get("poll_attempts", 10))
    delay = float(otp_cfg.get("poll_delay", 5))

    for attempt in range(1, attempts + 1):
        if time.monotonic() > deadline:
            logger.warning("otp: overall timeout reached while polling email")
            return None
        logger.info(f"otp: checking inbox ({attempt}/{attempts})")
        # imaplib is blocking — keep it off the event loop.
        code = await asyncio.to_thread(_fetch_otp_sync, otp_cfg)
        if code:
            return code
        await asyncio.sleep(delay)
    return None


def _fetch_otp_sync(otp_cfg: dict[str, Any]) -> str | None:
    """Blocking IMAP fetch of the newest matching unread message."""
    host = otp_cfg.get("imap_host", "imap.gmail.com")
    port = int(otp_cfg.get("imap_port", 993))
    user = otp_cfg.get("imap_user", "")
    password = otp_cfg.get("imap_pass", "")
    folder = otp_cfg.get("imap_folder", "INBOX")
    senders = [s.lower() for s in otp_cfg.get("sender_contains", []) or []]
    subjects = [s.lower() for s in otp_cfg.get("subject_contains", []) or []]

    if not user or not password:
        logger.warning("otp: imap_user/imap_pass not configured")
        return None

    client: imaplib.IMAP4_SSL | None = None
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=30)
        client.login(user, password)
        client.select(folder)

        status, data = client.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return None

        for raw_id in reversed(data[0].split()):  # newest first
            status, payload = client.fetch(raw_id, "(RFC822)")
            if status != "OK" or not payload:
                continue
            message = email.message_from_bytes(payload[0][1])
            sender = _decode(message.get("From", "")).lower()
            subject = _decode(message.get("Subject", "")).lower()

            if senders and not any(s in sender for s in senders):
                if not (subjects and any(s in subject for s in subjects)):
                    continue

            body = _message_body(message)
            match = OTP_REGEX.search(subject) or OTP_REGEX.search(body)
            if match:
                client.store(raw_id, "+FLAGS", "\\Seen")
                logger.info(f"otp: code found in mail from {sender[:60]!r}")
                return match.group(1)
        return None
    except Exception as exc:
        logger.warning(f"otp: IMAP error: {exc}")
        return None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def _decode(value: str) -> str:
    parts = []
    for chunk, encoding in decode_header(value or ""):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _message_body(message: email.message.Message) -> str:
    """Flatten a mail into text, preferring text/plain over stripped HTML."""
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                chunks.append(_part_text(part))
    else:
        chunks.append(_part_text(message))
    text = "\n".join(chunks)
    return re.sub(r"<[^>]+>", " ", text)


def _part_text(part: email.message.Message) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# SMS (Telegram reply)
# --------------------------------------------------------------------------- #
async def _code_from_telegram(
    config: dict[str, Any],
    notifier: Any,
    state: Any,
    timeout: float,
) -> str | None:
    if notifier is None:
        logger.error("otp: method is 'sms' but no notifier is available")
        return None

    if state is not None:
        state.set_manual_pause("waiting for the SMS OTP code")
    await notifier.manual_required(
        "The portal is asking for an SMS OTP. Reply to this chat with the code "
        "(digits only) within 5 minutes."
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = await notifier.wait_for_reply(timeout=min(60, deadline - time.monotonic()))
        if reply is None:
            continue
        match = OTP_REGEX.search(reply)
        if match:
            if state is not None:
                state.clear_manual_pause()
            return match.group(1)
        await notifier.send("That did not look like a code — please send the digits only.")

    if state is not None:
        state.clear_manual_pause()
    return None


# --------------------------------------------------------------------------- #
# Page entry
# --------------------------------------------------------------------------- #
async def _fill_code(page: Any, code: str, config: dict[str, Any]) -> bool:
    """Type the code into the OTP field and submit.

    Honeypot-safe, same policy as the login form: a keyword-matched visible input
    first, then ``find_real_input`` (``div.mb-3`` wrapper with ``display:block``).
    Never ``.first`` and never a fixed index — positions randomise per load.
    """
    element = await utils.find_visible_input(
        page, kinds=("text", "tel", "number"), keywords=OTP_FIELD_KEYWORDS
    )
    if element is None:
        element = await utils.find_real_input(page, "text")

    if element is not None:
        await utils.type_like_human(element, code)
    else:
        logger.warning("otp: could not locate the code field")
        await utils.screenshot(page, "otp-no-field")
        return False

    await utils.human_delay(config)
    if not await utils.click_by_text(page, OTP_SUBMIT_TEXTS, config=config):
        await page.keyboard.press("Enter")
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    return True


def _log(state: Any, message: str, *, status: str = "info") -> None:
    logger.info(f"otp: {message}")
    if state is not None:
        state.log_event("otp", message, status=status)
