"""Shared bot state.

A single :class:`BotState` instance is created by ``bot.py`` and handed to every
module. The Flask dashboard reads it from another thread, so every mutation goes
through a lock and every read returns a plain-dict snapshot.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

# Location status values surfaced on the dashboard.
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_CHECKING = "checking"
STATUS_NO_SLOTS = "no_slots"
STATUS_SLOT_FOUND = "slot_found"
STATUS_BOOKING = "booking"
STATUS_BOOKED = "booked"
STATUS_PAUSED = "paused"
STATUS_ERROR = "error"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class BotState:
    """Thread-safe snapshot of what the bot is doing right now."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._lock = threading.RLock()
        self._config = config

        dash = config.get("dashboard", {}) or {}
        self._log: deque[dict[str, Any]] = deque(maxlen=int(dash.get("log_limit", 300)))

        self.started_at = _now()
        self._running = bool(dash.get("autostart", True))
        self._stop_requested = False
        self._paused = False
        self._pause_reason: str | None = None

        self._locations: dict[str, dict[str, Any]] = {}
        for loc in config.get("bls", {}).get("locations", []) or []:
            self._locations[str(loc).lower()] = {
                "status": STATUS_IDLE,
                "detail": "",
                "last_checked": None,
                "last_slot_found": None,
                "checks": 0,
                "errors": 0,
            }

        self._counters = {"cycles": 0, "slots_found": 0, "errors": 0, "bookings": 0}
        self._last_error: str | None = None
        self._appointment: dict[str, Any] | None = None
        self._last_heartbeat: datetime | None = None

    # ------------------------------------------------------------------ #
    # Run / pause control
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        with self._lock:
            return self._running and not self._stop_requested

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def pause_reason(self) -> str | None:
        with self._lock:
            return self._pause_reason

    def request_start(self) -> None:
        with self._lock:
            self._running = True
            self._stop_requested = False
        self.log_event("control", "monitoring start requested", status="ok")

    def request_pause(self) -> None:
        """Pause the monitoring loop without tearing the browser down."""
        with self._lock:
            self._running = False
        self.log_event("control", "monitoring pause requested", status="ok")

    def request_stop(self) -> None:
        """Ask the whole bot to shut down."""
        with self._lock:
            self._running = False
            self._stop_requested = True
        self.log_event("control", "bot stop requested", status="ok")

    def set_manual_pause(self, reason: str) -> None:
        with self._lock:
            self._paused = True
            self._pause_reason = reason
        self.log_event("manual", f"paused — waiting for /resume: {reason}", status="warn")

    def clear_manual_pause(self) -> None:
        with self._lock:
            self._paused = False
            self._pause_reason = None
        self.log_event("manual", "resumed by user", status="ok")

    # ------------------------------------------------------------------ #
    # Per-location status
    # ------------------------------------------------------------------ #
    def set_location_status(
        self,
        location: str,
        status: str,
        detail: str = "",
        *,
        checked: bool = False,
    ) -> None:
        key = location.lower()
        with self._lock:
            entry = self._locations.setdefault(
                key,
                {
                    "status": STATUS_IDLE,
                    "detail": "",
                    "last_checked": None,
                    "last_slot_found": None,
                    "checks": 0,
                    "errors": 0,
                },
            )
            entry["status"] = status
            entry["detail"] = detail
            if checked:
                entry["last_checked"] = _now()
                entry["checks"] += 1
            if status == STATUS_SLOT_FOUND:
                entry["last_slot_found"] = _now()
                self._counters["slots_found"] += 1
            if status == STATUS_ERROR:
                entry["errors"] += 1
                self._counters["errors"] += 1
                self._last_error = f"{key}: {detail}"

    def mark_cycle(self) -> None:
        with self._lock:
            self._counters["cycles"] += 1

    def mark_booked(self, appointment: dict[str, Any]) -> None:
        with self._lock:
            self._counters["bookings"] += 1
            self._appointment = dict(appointment)

    def mark_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = _now()

    # ------------------------------------------------------------------ #
    # Live log
    # ------------------------------------------------------------------ #
    def log_event(
        self,
        location: str,
        event: str,
        *,
        status: str = "info",
    ) -> dict[str, Any]:
        """Append one row to the dashboard's live log (newest first on read)."""
        entry = {
            "timestamp": _iso(_now()),
            "location": location,
            "event": event,
            "status": status,
        }
        with self._lock:
            self._log.append(entry)
        return entry

    def recent_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._log)
        rows.reverse()  # newest first
        return rows[:limit]

    # ------------------------------------------------------------------ #
    # Snapshot for the dashboard / Telegram /status
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            locations = {
                key: {
                    "status": val["status"],
                    "detail": val["detail"],
                    "last_checked": _iso(val["last_checked"]),
                    "last_slot_found": _iso(val["last_slot_found"]),
                    "checks": val["checks"],
                    "errors": val["errors"],
                }
                for key, val in self._locations.items()
            }
            applicant = self._config.get("applicant", {}) or {}
            bls = self._config.get("bls", {}) or {}
            return {
                "running": self._running and not self._stop_requested,
                "stopping": self._stop_requested,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "started_at": _iso(self.started_at),
                "uptime_seconds": int((_now() - self.started_at).total_seconds()),
                "poll_interval": self._config.get("poll_interval", 90),
                "visa_type": bls.get("visa_type", ""),
                "locations": locations,
                "counters": dict(self._counters),
                "last_error": self._last_error,
                "appointment": dict(self._appointment) if self._appointment else None,
                "last_heartbeat": _iso(self._last_heartbeat),
                # Applicant summary only — never expose credentials to the UI.
                "applicant": {
                    "full_name": applicant.get("full_name", ""),
                    "passport_number": applicant.get("passport_number", ""),
                    "nationality": applicant.get("nationality", ""),
                    "dob": applicant.get("dob", ""),
                    "phone": applicant.get("phone", ""),
                    "email": applicant.get("email", ""),
                },
            }

    def status_text(self) -> str:
        """Compact human-readable status used by the Telegram /status command."""
        snap = self.snapshot()
        if snap["stopping"]:
            head = "🛑 Stopping"
        elif snap["paused"]:
            head = f"⏸ Paused — {snap['pause_reason']}"
        elif snap["running"]:
            head = "✅ Monitoring"
        else:
            head = "⏹ Idle"

        lines = [
            head,
            f"Visa type: {snap['visa_type']}",
            f"Cycles: {snap['counters']['cycles']} | "
            f"Slots: {snap['counters']['slots_found']} | "
            f"Errors: {snap['counters']['errors']}",
            "",
        ]
        for loc, info in snap["locations"].items():
            last = info["last_checked"] or "never"
            lines.append(f"• {loc.title()}: {info['status']} (last check: {last})")
        if snap["appointment"]:
            lines += ["", f"Booked: {snap['appointment']}"]
        if snap["last_error"]:
            lines += ["", f"Last error: {snap['last_error']}"]
        return "\n".join(lines)
