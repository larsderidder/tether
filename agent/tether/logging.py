"""Structlog configuration used by the agent and Uvicorn."""

from __future__ import annotations

import logging
import logging.config
import sys

import structlog

from tether.settings import settings


def _add_uvicorn_access_fields(
    logger: logging.Logger | None, name: str, event_dict: dict
) -> dict:
    """Attach structured access log fields extracted from uvicorn.access records.

    Args:
        logger: Logging.Logger instance (unused by this processor).
        name: Logger name passed by structlog.
        event_dict: Structlog event dict to enrich.
    """
    record = event_dict.get("__record__")
    if not record or record.name != "uvicorn.access":
        return event_dict
    args = record.args
    if isinstance(args, tuple) and len(args) >= 5:
        client_addr, method, path, http_version, status_code = args[:5]
        event_dict["client_addr"] = client_addr
        event_dict["method"] = method
        event_dict["path"] = path
        event_dict["http_version"] = http_version
        event_dict["status_code"] = status_code
    return event_dict


def configure_logging() -> None:
    """Configure structlog + stdlib logging using env-driven settings."""
    log_level_name = settings.log_level()
    log_level = getattr(logging, log_level_name, logging.INFO)
    log_format = settings.log_format()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        # Default to a dev-friendly console renderer.
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors + [_add_uvicorn_access_fields],
    )

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"()": lambda: formatter}},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": sys.stdout,
                }
            },
            "root": {"handlers": ["default"], "level": log_level},
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": log_level, "propagate": False},
                "uvicorn.error": {
                    "handlers": ["default"],
                    "level": log_level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["default"],
                    "level": log_level,
                    "propagate": False,
                },
            },
        }
    )
