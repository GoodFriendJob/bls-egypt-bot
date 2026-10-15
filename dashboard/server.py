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
import subprocess
import sys
import time
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

# The dashboard is a long-lived process, so "git pull" does not change the code
# it is RUNNING -- only the code on disk. That is invisible, and it bites
# exactly when it matters: the fix that made Start launch a bot shipped while a
# dashboard from before it was still open, so Start went on doing what the old
# code did, which was nothing. Comparing what is on disk now against what it
# was at startup turns that into something the page can say out loud.
_WATCHED_SOURCE = (
    "dashboard/server.py",
    "dashboard/app.py",
    "dashboard/config_api.py",
    "dashboard/templates/index.html",
    "dashboard/static/style.css",
    "modules/ipc.py",
)


def _source_mtime() -> float:
    newest = 0.0
    for name in _WATCHED_SOURCE:
        try:
            newest = max(newest, (ROOT / name).stat().st_mtime)
        except OSError:
            continue
    return newest


_SOURCE_MTIME_AT_START = _source_mtime()


class RemoteState:
    """BotState's read side, backed by the file the bot publishes.

    Only the handful of members the dashboard touches are implemented. Writes
    become commands for the bot to pick up; nothing here mutates bot state
    directly, because the bot lives in another process.
    """

    def __init__(self, config: dict[str, Any], config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = Path(config_path or ROOT / "config.yaml")
        # The bot process this dashboard launched, if it launched one.
        self._child: Any = None

    # --- is there a bot to talk to at all? ------------------------------- #
    def bot_online(self) -> bool:
        _status, _log, age = ipc.read_state()
        return age <= ipc.STALE_AFTER

    @staticmethod
    def _note(message: str) -> None:
        """Write a line to the launch log, whatever happens next.

        Pressing Start used to be able to leave NO trace at all: the child's
        output goes to this file, so if the child never started there was
        nothing in it, and the only record was a line on the dashboard's own
        console -- which is usually a window nobody is looking at. Every press
        is recorded here now, including the ones that fail.
        """
        try:
            path = ROOT / "logs" / "dashboard-launch.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} | dashboard | {message}\n")
        except OSError as exc:  # pragma: no cover - diagnostics only
            logger.debug(f"dashboard: could not write the launch log: {exc}")

    def _python(self) -> str:
        """The interpreter the bot is installed under, not whatever runs this.

        The dashboard may be started by a different python (or by a service),
        and bot.py needs the venv that has playwright in it.
        """
        for candidate in (
            ROOT / ".venv" / "Scripts" / "python.exe",   # Windows
            ROOT / ".venv" / "bin" / "python",           # POSIX
        ):
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def launch_bot(self) -> str:
        """Start bot.py as its own process. Returns a line for the UI.

        Start used to only write a command file for a running bot to pick up,
        which does nothing at all when no bot is running -- and that is exactly
        when someone presses Start. The dashboard is a separate process by
        design, so starting the bot means launching it.
        """
        if self._child is not None and self._child.poll() is None:
            return "the bot is already starting up"

        python = self._python()
        command = [python, str(ROOT / "bot.py"), "--config", str(self._config_path)]

        # Anything the bot prints before loguru is configured -- a config error,
        # a missing dependency -- would otherwise go nowhere, and the symptom
        # would be "Start does nothing" all over again.
        log_path = ROOT / "logs" / "dashboard-launch.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = log_path.open("a", encoding="utf-8")
        except OSError:
            sink = None

        # Detached, so the bot keeps running when the dashboard is closed.
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            extra["start_new_session"] = True

        try:
            self._child = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=sink or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **extra,
            )
        except Exception as exc:
            logger.error(f"dashboard: could not launch the bot — {exc}")
            self._note(f"FAILED to launch: {exc}  (command: {command})")
            return f"could not launch the bot: {exc}"

        logger.info(
            f"dashboard: launched the bot (pid {self._child.pid}) with {python}"
        )
        self._note(f"launched pid {self._child.pid} using {python}")

        # It may fail immediately -- a bad config, a missing package. Saying so
        # now beats a panel that sits at "offline" with no explanation.
        time.sleep(1.5)
        if self._child.poll() is not None:
            tail = ""
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace")
                tail = lines.strip().splitlines()[-1][:200] if lines.strip() else ""
            except OSError:
                pass
            logger.error(
                f"dashboard: the bot exited immediately (code "
                f"{self._child.returncode}) — {tail}"
            )
            return (
                f"the bot exited immediately (code {self._child.returncode}). "
                f"See logs/dashboard-launch.log. {tail}"
            )

        return f"bot started (pid {self._child.pid}); it reports in within ~15s"

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
        # A second of slack: a checkout can rewrite a file with a timestamp a
        # hair newer than the read taken moments before.
        status["dashboard_stale"] = _source_mtime() > _SOURCE_MTIME_AT_START + 1
        status["state_age_seconds"] = None if age == float("inf") else round(age, 1)
        if not online:
            # However the bot left off, it is not running now.
            status["running"] = False
        return status

    def recent_log(self, limit: int = 100) -> list[dict[str, Any]]:
        _status, log, _age = ipc.read_state()
        return log[:limit]

    # --- writes become commands ----------------------------------------- #
    def request_start(self) -> str:
        """Resume a running bot, or launch one when none is running."""
        if self.bot_online():
            ipc.send_command("start")
            self._note("Start pressed; a bot is already online — command sent")
            return "start sent to the running bot"
        self._note("Start pressed; no bot online — launching one")
        return self.launch_bot()

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
    state = RemoteState(config, config_path)
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
