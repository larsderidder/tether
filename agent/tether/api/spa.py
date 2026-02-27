"""Static UI (SPA) fallback routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse

from tether.api.errors import raise_http_error

router = APIRouter(include_in_schema=False)

_static_root = Path(__file__).resolve().parents[1] / "static_ui"


@router.get("/{full_path:path}")
async def spa_fallback(full_path: str, request: Request):
    """Serve static UI assets or fall back to index.html for SPA routes."""
    if full_path.startswith("api") or full_path.startswith("events") or full_path == "health":
        raise_http_error("NOT_FOUND", "Not found", 404)
    if full_path:
        file_path = _static_root / full_path
        if file_path.is_file():
            return FileResponse(file_path)
    index_path = _static_root / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return PlainTextResponse("UI not built", status_code=404)
