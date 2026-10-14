"""File-based channel between the bot and a standalone dashboard process.

The dashboard used to run in a thread inside the bot and share its BotState
object directly. It now runs as its own process (dashboard.bat), so there is no
shared memory and the two have to talk through disk:

    logs/state.json     bot  -> dashboard    status snapshot + recent log
    logs/command.json   dashboard -> bot     start / pause / stop requests
    config.yaml         dashboard -> bot     settings, picked up by mtime

Everything is written atomically (temp file then os.replace) because the reader
polls continuously and must never see a half-written file. Reads never raise:
a missing or torn file simply reads as "nothing yet", which is the correct
state while the bot is starting up or has been stopped.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

STATE_FILE = Path("logs/state.json")
COMMAND_FILE = Path("logs/command.json")

# How stale a state file may be before the dashboard calls the bot offline.
# The publisher writes every 2s, so this tolerates a few missed beats without
# flapping on a slow cycle.
STALE_AFTER = 15.0


def _write_atomic(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.debug(f"ipc: could not write {path.name}: {exc}")
        return False


def _read(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A torn read is normal when the writer is mid-replace; the next poll
        # gets it. Never let this surface as an error.
        return {}


def publish_state(snapshot: dict[str, Any], log: list[dict[str, Any]]) -> bool:
    """Bot side: make the current status visible to the dashboard."""
    return _write_atomic(
        STATE_FILE, {"written_at": time.time(), "status": snapshot, "log": log}
    )


def read_state() -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    """Dashboard side: (status, log, age_seconds). age is inf when absent."""
    data = _read(STATE_FILE)
    if not data:
        return {}, [], float("inf")
    written = float(data.get("written_at") or 0)
    age = time.time() - written if written else float("inf")
    return data.get("status") or {}, data.get("log") or [], age


def send_command(action: str) -> bool:
    """Dashboard side: ask the bot to start / pause / stop."""
    return _write_atomic(
        COMMAND_FILE, {"action": str(action), "issued_at": time.time()}
    )


def take_command() -> str:
    """Bot side: consume a pending command, or "" when there is none.

    The file is deleted as it is read, so a command is acted on exactly once.
    """
    data = _read(COMMAND_FILE)
    if not data:
        return ""
    try:
        COMMAND_FILE.unlink()
    except OSError:
        pass
    action = str(data.get("action") or "").strip().lower()
    # Ignore anything left over from a previous run; a stale stop arriving at
    # startup would shut the bot straight back down.
    issued = float(data.get("issued_at") or 0)
    if issued and time.time() - issued > 120:
        logger.info(f"ipc: ignoring stale {action!r} command")
        return ""
    return action


def clear() -> None:
    """Remove both files — used at startup so nothing stale is picked up."""
    for path in (STATE_FILE, COMMAND_FILE):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.debug(f"ipc: could not clear {path.name}: {exc}")
