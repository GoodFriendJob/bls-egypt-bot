"""BLS Spain Egypt appointment bot — main entry point.

Usage
-----
    python bot.py                     # normal 24/7 run
    python bot.py --headful           # visible browser (selector debugging)
    python bot.py --once              # single availability check, then exit
    python bot.py --no-dashboard      # skip the Flask UI
    python bot.py --config other.yaml

Shut down with Ctrl+C, the dashboard Stop button, or Telegram /stop.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parent

from modules.auth import BLSAuth
from modules.monitor import Monitor
from modules.notifier import Notifier
from modules.state import BotState
from modules.utils import interruptible_sleep

CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"

# Defaults mirror config.example.yaml; config.yaml only needs to override.
DEFAULT_CONFIG: dict[str, Any] = {
    "bls": {
        "url": "https://egypt.blsspainglobal.com/",
        "email": "",
        "password": "",
        "locations": ["cairo", "alexandria"],
        "visa_type": "Short Stay / Tourist",
        "location_labels": {"cairo": "Cairo", "alexandria": "Alexandria"},
        "paths": {
            # CONFIRMED paths. There is no /account page on this portal.
            "login": "/Global/account/login",
            "dashboard": "/Global/bls/visatypeverification",
            "appointment": "/Global/bls/appointment",  # TODO: confirm
        },
    },
    "applicant": {"documents": {}},
    "telegram": {"token": "", "chat_id": "", "heartbeat_hours": 6},
    "otp": {
        "method": "email",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_user": "",
        "imap_pass": "",
        "imap_folder": "INBOX",
        "sender_contains": ["blsspainglobal", "bls"],
        "subject_contains": ["otp", "verification", "code"],
        "poll_attempts": 10,
        "poll_delay": 5,
        "timeout": 300,
    },
    "poll_interval": 90,
    "headless": True,
    "proxy": {"enabled": False, "server": "", "username": "", "password": ""},
    "browser": {
        "navigation_timeout": 60000,
        "action_timeout": 20000,
        "min_delay": 0.5,
        "max_delay": 2.0,
        "locale": "en-US",
        "timezone": "Africa/Cairo",
        "user_agent": "",
    },
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 5000,
        "autostart": True,
        "log_limit": 300,
        "default_language": "ar",
    },
    "logging": {
        "level": "INFO",
        "file": "logs/bot.log",
        "rotation": "10 MB",
        "retention": "14 days",
    },
    "retry": {
        "max_attempts": 3,
        "page_error_wait": 30,
        "network_error_wait": 60,
        "login_attempts": 2,
    },
    "detection": {
        "unavailable_phrases": [
            "no appointment",
            "no appointments",
            "not available",
            "no slot",
            "no slots",
            "fully booked",
            "currently unavailable",
            "try again later",
            "لا توجد مواعيد",
        ],
        "available_phrases": [
            "appointment available",
            "available slot",
            "select a date",
            "select date",
            "choose your slot",
        ],
        "liveness_phrases": [
            "liveness",
            "facial",
            "face verification",
            "selfie",
            "take a photo of yourself",
        ],
        "captcha_phrases": [
            "captcha",
            "select the image",
            "verify you are human",
            "i'm not a robot",
        ],
    },
}

REQUIRED_DIRS = ("session", "logs", "logs/screenshots", "logs/dom", "docs")

# --------------------------------------------------------------------------- #
# Per-run transcript
#
# Every run mirrors the complete terminal output into logs/run_<stamp>.txt so it
# can be committed and read from another machine. stdout/stderr are teed rather
# than adding a second loguru sink, because that also captures output written
# outside loguru (the Flask banner, bare tracebacks, library warnings).
# --------------------------------------------------------------------------- #
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_run_log_handle: Any = None


class _Tee:
    """Write through to the real stream, mirroring a plain-text copy to a file."""

    def __init__(self, stream: Any, handle: Any, lock: threading.Lock) -> None:
        self._stream = stream
        self._handle = handle
        self._lock = lock

    def write(self, data: str) -> int:
        written = self._stream.write(data)
        try:
            with self._lock:
                # Strip colour codes so the committed file stays readable.
                self._handle.write(_ANSI_RE.sub("", data))
        except Exception:
            pass
        return written if isinstance(written, int) else len(data)

    def flush(self) -> None:
        for target in (self._stream, self._handle):
            try:
                target.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", "utf-8") or "utf-8"


def start_run_log(stamp: str, command: str) -> Path:
    """Begin mirroring stdout/stderr into logs/run_<stamp>.txt."""
    global _run_log_handle
    path = ROOT / "logs" / f"run_{stamp}.txt"
    try:
        handle = path.open("w", encoding="utf-8", buffering=1, errors="replace")
    except OSError as exc:  # never let logging break the run
        print(f"WARNING: could not open run log {path}: {exc}", file=sys.stderr)
        return path

    _run_log_handle = handle
    handle.write(f"BLS appointment bot - run transcript\n")
    handle.write(f"started : {datetime.now().isoformat(timespec='seconds')}\n")
    handle.write(f"command : {command}\n")
    handle.write(f"python  : {sys.version.split()[0]}\n")
    handle.write("=" * 70 + "\n")

    lock = threading.Lock()
    sys.stdout = _Tee(sys.stdout, handle, lock)
    sys.stderr = _Tee(sys.stderr, handle, lock)
    return path


def close_run_log() -> None:
    global _run_log_handle
    if _run_log_handle is None:
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        _run_log_handle.write(
            "=" * 70 + f"\nfinished: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        _run_log_handle.close()
    except Exception:
        pass
    finally:
        _run_log_handle = None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base``."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Copy the template first:  copy {EXAMPLE_PATH.name} {path.name}"
        )
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must contain a YAML mapping at the top level.")

    config = deep_merge(DEFAULT_CONFIG, raw)
    config["bls"]["locations"] = [str(loc).lower() for loc in config["bls"]["locations"] or []]
    return config


def validate_config(config: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty = good to go)."""
    problems: list[str] = []
    bls = config["bls"]
    if not bls.get("email") or not bls.get("password"):
        problems.append("bls.email and bls.password are required")
    if not bls.get("locations"):
        problems.append("bls.locations must list at least one location")

    applicant = config.get("applicant") or {}
    for field in ("full_name", "passport_number"):
        if not applicant.get(field):
            problems.append(f"applicant.{field} is required")

    telegram = config.get("telegram") or {}
    if not telegram.get("token") or not telegram.get("chat_id"):
        problems.append("telegram.token / telegram.chat_id missing — alerts disabled")

    if (config["otp"].get("method") or "").lower() == "email":
        if not config["otp"].get("imap_user") or not config["otp"].get("imap_pass"):
            problems.append("otp.method is 'email' but imap_user/imap_pass are empty")

    for key, rel in (applicant.get("documents") or {}).items():
        if rel and not (ROOT / rel).exists():
            problems.append(f"document '{key}' not found at {rel}")
    return problems


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {}) or {}
    log_file = ROOT / log_cfg.get("file", "logs/bot.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=log_cfg.get("level", "INFO"),
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
            "<cyan>{name}</cyan> - <level>{message}</level>"
        ),
        enqueue=True,
    )
    logger.add(
        str(log_file),
        level=log_cfg.get("level", "INFO"),
        rotation=log_cfg.get("rotation", "10 MB"),
        retention=log_cfg.get("retention", "14 days"),
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )


def ensure_dirs() -> None:
    for rel in REQUIRED_DIRS:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Background tasks
# --------------------------------------------------------------------------- #
def start_dashboard(config: dict[str, Any], state: BotState) -> threading.Thread | None:
    dash = config.get("dashboard", {}) or {}
    if not dash.get("enabled", True):
        logger.info("dashboard disabled by config")
        return None

    from dashboard.app import run_dashboard

    thread = threading.Thread(
        target=run_dashboard,
        args=(config, state),
        name="dashboard",
        daemon=True,
    )
    thread.start()
    logger.info(f"dashboard on http://{dash.get('host', '127.0.0.1')}:{dash.get('port', 5000)}")
    return thread


async def heartbeat_task(config: dict[str, Any], state: BotState, notifier: Notifier) -> None:
    """Periodic STATUS alert so the client knows the bot is alive."""
    hours = float((config.get("telegram") or {}).get("heartbeat_hours", 6) or 0)
    if hours <= 0:
        # Must NOT return: the caller waits on FIRST_COMPLETED, so a heartbeat
        # task that finishes immediately would shut the whole bot down on
        # startup. Idle here until a stop is requested instead.
        logger.info("heartbeat disabled (telegram.heartbeat_hours = 0)")
        while not state.stop_requested:
            await interruptible_sleep(3600, state)
        return
    interval = hours * 3600
    while not state.stop_requested:
        await interruptible_sleep(interval, state)
        if state.stop_requested:
            return
        state.mark_heartbeat()
        await notifier.status_heartbeat(state.status_text())


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BLS Spain Egypt appointment bot")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single check cycle and exit")
    parser.add_argument("--headful", action="store_true", help="force a visible browser window")
    parser.add_argument("--no-dashboard", action="store_true", help="do not start the Flask UI")
    parser.add_argument(
        "--ignore-config-warnings",
        action="store_true",
        help="start even if the config validation reports problems",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_dirs()

    # Start the transcript before anything else can print.
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log = start_run_log(run_stamp, " ".join(sys.argv))

    try:
        return _main_inner(args, run_log)
    finally:
        close_run_log()


def _main_inner(args: argparse.Namespace, run_log: Path) -> int:
    config = load_config(Path(args.config))

    if args.headful:
        config["headless"] = False
    if args.no_dashboard:
        config["dashboard"]["enabled"] = False

    setup_logging(config)
    logger.info("=" * 62)
    logger.info("BLS Spain Egypt appointment bot starting")
    logger.info(f"run transcript: {run_log}")
    logger.info(f"portal={config['bls']['url']} locations={config['bls']['locations']}")
    logger.info(f"visa_type={config['bls']['visa_type']} poll_interval={config['poll_interval']}s")
    logger.info("=" * 62)

    problems = validate_config(config)
    for problem in problems:
        logger.warning(f"config: {problem}")
    blocking = [p for p in problems if "required" in p or "must list" in p]
    if blocking and not args.ignore_config_warnings:
        logger.error("refusing to start — fix the config problems above (or pass --ignore-config-warnings)")
        return 2

    state_holder: dict[str, Any] = {}

    async def _bootstrap() -> int:
        state = BotState(config)
        state_holder["state"] = state
        # Dashboard shares the same BotState instance as the bot.
        start_dashboard(config, state)
        return await _run_with_state(config, args, state)

    try:
        return asyncio.run(_bootstrap())
    except KeyboardInterrupt:
        state = state_holder.get("state")
        if state is not None:
            state.request_stop()
        logger.info("interrupted by user")
        return 130


async def _run_with_state(
    config: dict[str, Any],
    args: argparse.Namespace,
    state: BotState,
) -> int:
    """Wire the modules around an already-created state and run until stop."""
    notifier = Notifier(config, state)
    auth = BLSAuth(config, state=state, notifier=notifier)
    monitor = Monitor(config, state=state, auth=auth, notifier=notifier)
    notifier.bind(monitor=monitor)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, state.request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    await notifier.start()
    await notifier.startup_notice()

    tasks: list[asyncio.Task[Any]] = []
    exit_code = 0
    try:
        if args.once:
            logger.info("single-cycle mode (--once)")
            await monitor.run_once()
        else:
            tasks.append(asyncio.create_task(monitor.run(), name="monitor"))
            tasks.append(
                asyncio.create_task(heartbeat_task(config, state, notifier), name="heartbeat")
            )
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
    except asyncio.CancelledError:
        logger.info("cancelled — shutting down")
    except Exception as exc:
        logger.exception(f"fatal error: {exc}")
        state.log_event("bot", f"fatal error: {exc}", status="error")
        await notifier.error(
            f"Bot stopped with a fatal error: {exc}",
            retry_info="manual restart required",
        )
        exit_code = 1
    finally:
        state.request_stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await monitor.close()
        await notifier.shutdown_notice()
        await notifier.stop()
        logger.info("shutdown complete")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
