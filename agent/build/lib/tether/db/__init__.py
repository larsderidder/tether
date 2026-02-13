"""Database engine and session management."""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from sqlmodel import SQLModel, create_engine, Session as DBSession
from sqlalchemy import Engine

from tether.settings import settings

logger = structlog.get_logger(__name__)

# Lazy-initialized engine
_engine: Engine | None = None


def _get_engine() -> Engine:
    """Get or create the database engine."""
    global _engine
    if _engine is None:
        data_dir = settings.data_dir()
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "sessions.db")
        db_url = f"sqlite:///{db_path}"
        _engine = create_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def reset_engine() -> None:
    """Reset the engine so it will be recreated with current settings.

    Used by tests to point at a fresh database after changing TETHER_AGENT_DATA_DIR.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def get_db_url() -> str:
    """Return the database URL for Alembic."""
    data_dir = settings.data_dir()
    db_path = os.path.join(data_dir, "sessions.db")
    return f"sqlite:///{db_path}"


def get_session() -> DBSession:
    """Get a new database session."""
    return DBSession(_get_engine())


def _run_migrations() -> None:
    """Run Alembic migrations to bring schema up to date.

    Handles three cases:
    1. Brand-new DB (no tables) — runs all migrations from initial schema.
    2. Existing DB created by create_all() (no alembic_version) — stamps
       initial, then runs remaining migrations.
    3. Existing DB with alembic_version — runs only pending migrations.
    """
    from alembic.config import Config
    from alembic import command
    from sqlalchemy import inspect

    engine = _get_engine()
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    alembic_dir = Path(__file__).resolve().parent.parent.parent / "alembic"
    ini_path = alembic_dir.parent / "alembic.ini"

    if not ini_path.exists():
        # Running from installed package without alembic dir — fall back
        logger.debug("Alembic config not found, using create_all()")
        SQLModel.metadata.create_all(bind=engine)
        return

    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", get_db_url())

    has_tables = "sessions" in tables
    has_alembic = "alembic_version" in tables

    if not has_tables:
        # Case 1: fresh DB — run all migrations
        logger.info("New database, running all migrations")
        command.upgrade(cfg, "head")
    elif has_tables and not has_alembic:
        # Case 2: DB was created by create_all(), no migration tracking.
        # Stamp the initial migration so Alembic knows the base schema exists,
        # then upgrade to pick up any subsequent migrations.
        logger.info("Existing database without migration tracking, stamping initial revision")
        command.stamp(cfg, "b011541f1109")
        command.upgrade(cfg, "head")
    else:
        # Case 3: normal — run pending migrations
        command.upgrade(cfg, "head")


def init_db() -> None:
    """Initialize the database schema, running any pending migrations."""
    try:
        _run_migrations()
    except Exception:
        logger.warning("Alembic migration failed, falling back to create_all()", exc_info=True)
        SQLModel.metadata.create_all(bind=_get_engine())


__all__ = [
    "get_session",
    "get_db_url",
    "init_db",
    "reset_engine",
    "DBSession",
]
