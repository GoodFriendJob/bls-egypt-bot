"""Flask dashboard — monitoring status, applicant summary, live log, controls.

Runs in a daemon thread next to the asyncio bot and shares the same BotState
instance. Local use only: no authentication, bound to 127.0.0.1 by default.
"""

from __future__ import annotations

import logging
from typing import Any

import hmac
import os
import secrets
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dashboard import config_api
from loguru import logger


LOGIN_ENDPOINT = "login"


def _install_auth(app: Flask, password: str) -> None:
    """Gate every page and API route behind a shared password.

    Static: one password from config, compared in constant time, held in a
    signed session cookie. That is proportionate for a single-operator tool,
    but it is only as private as the transport -- see the warning in
    dashboard/server.py about running this over plain HTTP.

    With no password configured the dashboard is open; server.py refuses to
    bind anything but loopback in that case, so "open" means "this machine
    only".
    """
    # A fresh key each start means restarting the dashboard logs everyone out,
    # which is the safer default for a tool that edits credentials.
    app.secret_key = os.environ.get("BLS_DASHBOARD_SECRET") or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    )

    if not password:
        app.config["BLS_AUTH"] = False
        return
    app.config["BLS_AUTH"] = True

    @app.before_request
    def _require_login():  # type: ignore[no-untyped-def]
        if request.endpoint in (LOGIN_ENDPOINT, "static"):
            return None
        if session.get("bls_ok"):
            return None
        # An expired session on a background poll must not render the login
        # PAGE into the fetch; answer API calls with a status the UI can act on.
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "auth required"}), 401
        return redirect(url_for(LOGIN_ENDPOINT, next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():  # type: ignore[no-untyped-def]
        error = ""
        if request.method == "POST":
            supplied = str(request.form.get("password", ""))
            # compare_digest keeps the check constant-time, so a wrong password
            # cannot be narrowed down by timing it.
            if hmac.compare_digest(supplied, password):
                session.clear()
                session["bls_ok"] = True
                session.permanent = True
                target = request.args.get("next") or "/"
                # Only ever bounce to a local path; an attacker-supplied
                # absolute URL would turn this into an open redirect.
                if not target.startswith("/") or target.startswith("//"):
                    target = "/"
                logger.info("dashboard: login accepted")
                return redirect(target)
            error = "Wrong password"
            logger.warning(f"dashboard: failed login from {request.remote_addr}")
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():  # type: ignore[no-untyped-def]
        session.clear()
        return redirect(url_for(LOGIN_ENDPOINT))


def create_app(
    config: dict[str, Any], state: Any, config_path: str = "config.yaml"
) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False  # keep Arabic readable in API responses
    dash_cfg = config.get("dashboard", {}) or {}
    _install_auth(app, str(dash_cfg.get("password", "") or ""))

    @app.route("/")
    def index() -> str:
        # ?lang=en / ?lang=ar overrides the configured default for this visit,
        # so the English dashboard has a shareable link of its own.
        requested = str(request.args.get("lang", "")).lower()
        language = requested if requested in ("ar", "en") else str(
            dash_cfg.get("default_language", "ar")
        )
        return render_template(
            "index.html",
            default_language=language,
            poll_interval=config.get("poll_interval", 90),
        )

    @app.route("/api/config")
    def api_config() -> Any:
        """The whole of config.yaml, grouped, with secrets masked."""
        return jsonify(config_api.describe(config))

    @app.route("/api/config", methods=["POST"])
    def api_config_save() -> Any:
        payload = request.get_json(silent=True) or {}
        changes = payload.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return jsonify({"ok": False, "error": "no changes supplied"}), 400

        applied, rejected = config_api.apply_changes(config, changes)
        if not applied:
            return jsonify({"ok": False, "error": "nothing applied", "rejected": rejected}), 400

        saved = True
        error = ""
        try:
            config_api.save(config, Path(config_path))
        except Exception as exc:  # the live edit still stands if the write fails
            saved = False
            error = str(exc)
            logger.error(f"dashboard: could not write config: {exc}")

        # Push the edit into the values the Monitor cached at startup.
        live = state.reload_config()
        state.log_event(
            "bot",
            f"settings updated from dashboard: {', '.join(applied)}",
            status="ok" if (saved and live) else "warn",
        )
        logger.info(f"dashboard: applied {applied} (saved={saved}, live={live})")
        return jsonify({
            "ok": True,
            "applied": applied,
            "rejected": rejected,
            "saved_to_disk": saved,
            "applied_live": live,
            "error": error,
        })

    @app.route("/api/status")
    def api_status() -> Any:
        return jsonify(state.snapshot())

    @app.route("/api/log")
    def api_log() -> Any:
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        return jsonify({"entries": state.recent_log(limit=max(1, min(limit, 1000)))})

    @app.route("/api/control", methods=["POST"])
    def api_control() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", "")).lower()

        if action == "start":
            state.request_start()
        elif action == "pause":
            state.request_pause()
        elif action == "stop":
            state.request_stop()
        else:
            return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 400

        logger.info(f"dashboard: control action {action!r}")
        return jsonify({"ok": True, "action": action, "status": state.snapshot()})

    @app.errorhandler(500)
    def handle_500(exc: Any) -> Any:  # pragma: no cover - defensive
        logger.error(f"dashboard: internal error: {exc}")
        return jsonify({"ok": False, "error": "internal dashboard error"}), 500

    return app


def run_dashboard(
    config: dict[str, Any], state: Any, config_path: str = "config.yaml"
) -> None:
    """Blocking Flask server — call from a daemon thread."""
    dash_cfg = config.get("dashboard", {}) or {}
    host = dash_cfg.get("host", "127.0.0.1")
    port = int(dash_cfg.get("port", 5000))

    # Werkzeug's per-request logging would drown the bot's own output.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app(config, state, config_path)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as exc:
        logger.error(f"dashboard: server stopped: {exc}")
