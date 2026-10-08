"""Flask dashboard — monitoring status, applicant summary, live log, controls.

Runs in a daemon thread next to the asyncio bot and shares the same BotState
instance. Local use only: no authentication, bound to 127.0.0.1 by default.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, render_template, request
from loguru import logger


def create_app(config: dict[str, Any], state: Any) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False  # keep Arabic readable in API responses
    dash_cfg = config.get("dashboard", {}) or {}

    @app.route("/")
    def index() -> str:
        return render_template(
            "index.html",
            default_language=dash_cfg.get("default_language", "ar"),
            poll_interval=config.get("poll_interval", 90),
        )

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


def run_dashboard(config: dict[str, Any], state: Any) -> None:
    """Blocking Flask server — call from a daemon thread."""
    dash_cfg = config.get("dashboard", {}) or {}
    host = dash_cfg.get("host", "127.0.0.1")
    port = int(dash_cfg.get("port", 5000))

    # Werkzeug's per-request logging would drown the bot's own output.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app(config, state)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as exc:
        logger.error(f"dashboard: server stopped: {exc}")
