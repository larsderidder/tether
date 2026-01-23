"""FastAPI application entrypoint for the agent server."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from tether.api import api_router, root_router
from tether.http import (
    http_exception_handler,
    request_logging_middleware,
    validation_exception_handler,
)
from tether.logging import configure_logging
from tether.maintenance import maintenance_loop
from tether.settings import settings
from tether.startup import log_ui_urls

configure_logging()

app = FastAPI()

app.middleware("http")(request_logging_middleware)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)


def _ensure_token() -> None:
    if settings.token() or settings.dev_mode():
        return
    raise RuntimeError(
        "TETHER_AGENT_TOKEN is required unless TETHER_AGENT_DEV_MODE=1"
    )


@app.on_event("startup")
async def _start_maintenance() -> None:
    _ensure_token()
    app.state.agent_token = settings.token()
    asyncio.create_task(maintenance_loop())
    log_ui_urls(port=settings.port())


app.include_router(api_router)
app.include_router(root_router)

if __name__ == "__main__":
    import uvicorn

    _ensure_token()
    app.state.agent_token = settings.token()
    uvicorn.run(
        "tether.main:app",
        host=settings.host(),
        port=settings.port(),
        reload=False,
    )
else:
    app.state.agent_token = settings.token()
