"""Startup helpers for logging connection URLs."""

from __future__ import annotations

import socket

import structlog

logger = structlog.get_logger(__name__)


def _guess_lan_ip() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def log_ui_urls(port: int = 8787) -> None:
    """Log likely URLs for accessing the UI from localhost or LAN."""
    logger.info("UI available", url=f"http://localhost:{port}/")
    lan_ip = _guess_lan_ip()
    if lan_ip:
        logger.info("UI available (LAN)", url=f"http://{lan_ip}:{port}/")
