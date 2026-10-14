"""Standalone dashboard process.

    python -m dashboard.server            (or dashboard.bat)

Runs on its own, reading the bot's status out of logs/state.json and sending
control back through logs/command.json. The bot does not have to be running —
the UI then simply reports it as offline.

SECURITY. This serves the applicant's personal data and edits a config file
that holds the portal password, the Telegram token and an OpenAI key. It is
therefore password-protected, and it REFUSES to listen on a non-loopback
address unless a password is set. Traffic is plain HTTP, so on anything other
than a trusted LAN put it behind a TLS reverse proxy or a VPN — otherwise the
password and everything it protects cross the network in the clear.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.app import create_app  # noqa: E402
from modules import ipc  # noqa: E402

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class RemoteState:
    """BotState's read side, backed by the file the bot publishes.

    Only the handful of members the dashboard touches are implemented. Writes
    become commands for the bot to pick up; nothing here mutates bot state
    directly, because the bot lives in another process.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    # --- reads ---------------------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        status, _log, age = ipc.read_state()
        online = age <= ipc.STALE_AFTER
        if not status:
            # Nothing published yet: report a believable idle shape so the UI
            # renders rather than erroring.
            bls = self._config.get("bls", {}) or {}
            status = {
                "running": False,
                "stopping": False,
                "paused": False,
                "pause_reason": "",
                "uptime_seconds": 0,
                "poll_interval": self._config.get("poll_interval", 90),
                "visa_type": bls.get("visa_type", ""),
                "locations": {},
                "counters": {"cycles": 0, "slots_found": 0, "errors": 0, "bookings": 0},
                "last_error": "",
                "appointment": None,
                "applicant": {},
            }
        status = dict(status)
        status["bot_online"] = online
        status["state_age_seconds"] = None if age == float("inf") else round(age, 1)
        if not online:
            # However the bot left off, it is not running now.
            status["running"] = False
        return status

    def recent_log(self, limit: int = 100) -> list[dict[str, Any]]:
        _status, log, _age = ipc.read_state()
        return log[:limit]

    # --- writes become commands ----------------------------------------- #
    def request_start(self) -> None:
        ipc.send_command("start")

    def request_pause(self) -> None:
        ipc.send_command("pause")

    def request_stop(self) -> None:
        ipc.send_command("stop")

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        """No-op: the log belongs to the bot, which publishes it."""

    def reload_config(self) -> bool:
        """The bot reloads config.yaml itself when it sees the file change."""
        return True

    def set_config_reloader(self, fn: Any) -> None:
        """Present for parity with BotState; nothing to register here."""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a mapping")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BLS bot dashboard (standalone)")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--host", default=None, help="override dashboard.host")
    parser.add_argument("--port", type=int, default=None, help="override dashboard.port")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"config not found: {config_path}", file=sys.stderr)
        return 2
    config = load_config(config_path)

    dash = config.get("dashboard", {}) or {}
    host = args.host or dash.get("host", "127.0.0.1")
    port = int(args.port or dash.get("port", 5000))
    password = str(dash.get("password", "") or "")

    # Refuse to expose personal data and credentials to a network with no
    # password at all. Binding to loopback without one is fine.
    if host not in LOOPBACK and not password:
        print(
            "REFUSING TO START.\n"
            f"  dashboard.host is {host!r}, which accepts connections from the\n"
            "  network, but dashboard.password is empty. This page shows the\n"
            "  applicant's personal data and edits a config holding the portal\n"
            "  password, Telegram token and OpenAI key.\n\n"
            "  Set dashboard.password in config.yaml, or set host to 127.0.0.1.",
            file=sys.stderr,
        )
        return 2

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    state = RemoteState(config)
    app = create_app(config, state, str(config_path))

    where = "this machine only" if host in LOOPBACK else "ANY machine that can reach this host"
    logger.info(f"dashboard: http://{host}:{port}  (reachable from: {where})")
    logger.info(f"dashboard: password protection {'ON' if password else 'OFF'}")
    if host not in LOOPBACK:
        logger.warning(
            "dashboard: traffic is plain HTTP — put it behind TLS or a VPN "
            "before using it over the internet"
        )

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except OSError as exc:
        logger.error(f"dashboard: could not bind {host}:{port} — {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
