"""Flask application factory.

Serves the frontend as static files from the same origin as ``/stream``, so there is no
CORS story to get wrong and no second server to run.

**On ``gunicorn -w 1``.** Session state -- the upstream connection, the acquisition
thread, the publisher -- lives in this process's memory. A second worker would be a
second, independent copy of that state, and which one your browser reached would depend
on which socket the OS handed the connection to. The symptom would be an instrument
that works, then inexplicably does not. So the worker count is pinned in the Dockerfile,
the compose file and the README, and ``create_app`` logs it at startup so the constraint
is visible in the output rather than only in the docs.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, send_from_directory

from backend.config import Config
from backend.infrastructure.stream_route import build_blueprint

log = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


def create_app(config: Config | None = None) -> Flask:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
    )
    config = config or Config.from_env()

    app = Flask(__name__, static_folder=None)
    app.config["SOCK_SERVER_OPTIONS"] = {
        "max_message_size": config.max_downstream_message_bytes,
    }
    app.config["SIGNALSCOPE"] = config

    blueprint, sock = build_blueprint(config)
    app.register_blueprint(blueprint)
    sock.init_app(app)

    @app.get("/healthz")
    def healthz():
        """Enough to tell a container orchestrator the process is up, and enough to
        tell a human which configuration it came up with."""
        return jsonify(
            status="ok",
            expected_samples=config.expected_samples,
            publish_hz=config.publish_hz,
        )

    @app.get("/")
    def index():
        if not os.path.exists(os.path.join(FRONTEND_DIR, "index.html")):
            # Phase 3 ships the backend; the frontend lands in Phase 4. Say so plainly
            # rather than returning a bare 404 that looks like a broken deployment.
            return (
                "SignalScope backend is running. The frontend is not built yet "
                "(Phase 4). The WebSocket endpoint is at /stream.",
                200,
                {"Content-Type": "text/plain; charset=utf-8"},
            )
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(FRONTEND_DIR, filename)

    log.info(
        "SignalScope backend ready: expected_samples=%d publish_hz=%.1f "
        "target_columns=%d (single worker required: gunicorn -w 1)",
        config.expected_samples,
        config.publish_hz,
        config.target_columns,
    )
    return app
