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

from modules.auth import BLSAuth, PortalUnreachableError
from modules.monitor import APPLICANT_FIELDS, Monitor
from modules.notifier import Notifier
from modules import ipc
from modules.state import BotState
from modules.utils import block_cooldown_remaining as utils_block_cooldown
from modules.utils import prune_logs as utils_prune_logs
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
    # Every field the applicant form asks for, blank. Two reasons it is derived
    # from APPLICANT_FIELDS rather than typed out again: a second list would
    # drift, and a key absent from the live dict is a key the dashboard cannot
    # show — so a machine whose config.yaml predates a field had no way to add
    # it from the Settings tab, only by hand-editing YAML.
    "applicant": {
        "documents": {},
        # Not one of the mapped fields (it is a radio group, not a value), but
        # it has to be present for the same reason: a key the live dict lacks
        # is a key the dashboard cannot show.
        "inviting_party": "company",
        "fingerprints": "none",
        **{key: "" for _base, _kind, key, _required in APPLICANT_FIELDS},
    },
    "openai": {"api_key": "", "model": "gpt-4o", "timeout": 45},
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
        # Default OFF: on this portal the stealth script throws
        # "Cannot redefine property: offsetHeight" from inside FingerprintJS's
        # font probing, corrupting the fingerprint the server expects.
        "stealth": False,
        "allow_insecure_content": True,
        "device_scale_factor": 2,
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

REQUIRED_DIRS = (
    "session",
    "logs",
    "logs/screenshots",
    "logs/dom",
    "logs/captcha_samples",
    "docs",
)

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

# Run transcripts are committed to git, so anything secret-shaped is masked on
# the way into the file. Nothing deliberately logs a credential, but a library
# traceback or a repr of the config dict easily could.
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),                 # OpenAI keys
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),       # Telegram bot tokens
)


def _redact(text: str) -> str:
    for pattern in _SECRET_RES:
        text = pattern.sub("[REDACTED]", text)
    return text


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
                # Strip colour codes so the committed file stays readable, and
                # mask anything secret-shaped before it reaches a tracked file.
                self._handle.write(_redact(_ANSI_RE.sub("", data)))
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

    if not (config.get("openai") or {}).get("api_key"):
        problems.append(
            "openai.api_key is empty — CAPTCHAs cannot be solved automatically, "
            "the bot will pause for a manual solve instead"
        )

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
async def dashboard_bridge(
    config: dict[str, Any], state: BotState, config_path: Path
) -> None:
    """Keep the standalone dashboard fed, and act on what it sends back.

    The dashboard is its own process now, so it cannot touch BotState directly.
    This publishes the status for it to read, applies any control it issued,
    and reloads config.yaml when the dashboard has written to it. Polls at 2s,
    which is what the UI refreshes at.
    """
    log_limit = int((config.get("dashboard", {}) or {}).get("log_limit", 300))
    try:
        last_config_mtime = config_path.stat().st_mtime
    except OSError:
        last_config_mtime = 0.0

    ipc.clear()
    logger.info(f"dashboard bridge: publishing status to {ipc.STATE_FILE}")

    while not state.stop_requested:
        try:
            ipc.publish_state(state.snapshot(), state.recent_log(limit=log_limit))

            action = ipc.take_command()
            if action:
                logger.info(f"dashboard bridge: {action!r} requested")
                if action == "start":
                    state.request_start()
                elif action == "pause":
                    state.request_pause()
                elif action == "stop":
                    state.request_stop()
                else:
                    logger.warning(f"dashboard bridge: unknown action {action!r}")
                # Publish straight away so the panel reflects the button press
                # now, instead of up to one poll later.
                ipc.publish_state(
                    state.snapshot(), state.recent_log(limit=log_limit)
                )

            # The dashboard writes config.yaml directly; reload when it changes
            # so an edit reaches the running bot without a restart.
            try:
                mtime = config_path.stat().st_mtime
            except OSError:
                mtime = last_config_mtime
            if mtime > last_config_mtime:
                last_config_mtime = mtime
                logger.info("dashboard bridge: config.yaml changed — reloading")
                try:
                    fresh = load_config(config_path)
                    config.clear()
                    config.update(fresh)
                    state.reload_config()
                except Exception as exc:
                    logger.error(f"dashboard bridge: reload failed: {exc}")
        except Exception as exc:  # never let the bridge kill the run
            logger.debug(f"dashboard bridge: {exc}")

        await asyncio.sleep(2.0)

    ipc.publish_state(state.snapshot(), state.recent_log(limit=log_limit))
    logger.info("dashboard bridge: stopped")


def start_dashboard(
    config: dict[str, Any], state: BotState, config_path: str = "config.yaml"
) -> threading.Thread | None:
    dash = config.get("dashboard", {}) or {}
    if not dash.get("enabled", True):
        logger.info("dashboard disabled by config")
        return None

    from dashboard.app import run_dashboard

    thread = threading.Thread(
        target=run_dashboard,
        args=(config, state, config_path),
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
        "--ignore-cooldown",
        action="store_true",
        help="start even if the portal blocked us recently (use sparingly)",
    )
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

    # A recorded block is honoured by waiting inside the monitor loop, not by
    # exiting: the bot is meant to run unattended and recover by itself.
    # --once is a test mode, so there it still refuses rather than hanging.
    cooldown = float((config.get("retry") or {}).get("block_cooldown_minutes", 60))
    remaining = utils_block_cooldown(cooldown)
    if remaining > 0:
        if args.once and not args.ignore_cooldown:
            logger.error(
                f"portal blocked us recently — {remaining:.0f} min of cooldown "
                "left. Single-shot mode will not wait; use --ignore-cooldown to "
                "force, or run without --once to wait it out automatically."
            )
            return 3
        logger.warning(
            f"portal blocked us recently — the loop will wait {remaining:.0f} "
            "min before its first attempt."
        )

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
        # The dashboard is a SEPARATE process now (dashboard.bat); the bot
        # only publishes status for it and applies what it sends back.
        # Housekeeping before anything writes new artifacts.
        utils_prune_logs(config)
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
    # The dashboard edits config.yaml and mutates the shared config dict; this
    # is what pushes those edits into the values Monitor cached at startup.
    # Registered here rather than passed to the dashboard because the dashboard
    # thread is already running by this point.
    state.set_config_reloader(monitor.reload_config)

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
    # Before any browser work: a blank required applicant setting cannot be
    # discovered any later than this without costing a real slot and a 27-minute
    # portal cooldown.
    await monitor.warn_about_missing_settings()

    tasks: list[asyncio.Task[Any]] = []
    exit_code = 0
    try:
        if args.once:
            logger.info("single-cycle mode (--once)")
            # Publish during a single cycle too. Without this the dashboard
            # showed nothing at all while a --once run (start_headful.bat) was
            # in progress, because the bridge only existed on the 24/7 path.
            bridge: asyncio.Task[Any] | None = None
            if (config.get("dashboard", {}) or {}).get("enabled", True):
                bridge = asyncio.create_task(
                    dashboard_bridge(config, state, Path(args.config)),
                    name="dashboard-bridge",
                )
            try:
                await monitor.run_once()
            except PortalUnreachableError as exc:
                # A 403 is an expected outcome, not a crash. Under --once it
                # used to escape as a fatal error with a full traceback
                # (run 20261013_184753); report it plainly and exit non-zero.
                logger.error(f"portal unreachable: {exc}")
                logger.info(
                    "the cooldown is recorded — a normal run would wait it out "
                    "and carry on by itself"
                )
                exit_code = 2
            finally:
                if bridge is not None:
                    # One last publish so the panel shows how the cycle ended,
                    # then stop.
                    try:
                        ipc.publish_state(state.snapshot(), state.recent_log(limit=300))
                    except Exception:
                        pass
                    bridge.cancel()
        else:
            tasks.append(asyncio.create_task(monitor.run(), name="monitor"))
            tasks.append(
                asyncio.create_task(heartbeat_task(config, state, notifier), name="heartbeat")
            )
            if (config.get("dashboard", {}) or {}).get("enabled", True):
                tasks.append(
                    asyncio.create_task(
                        dashboard_bridge(config, state, Path(args.config)),
                        name="dashboard-bridge",
                    )
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
