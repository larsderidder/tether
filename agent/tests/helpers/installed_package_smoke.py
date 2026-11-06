"""Run an installed wheel's database upgrades and real application lifespan."""

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config

import tether
from tether.db import get_db_url, init_db, reset_engine
from tether.main import app
from tether.store import SessionStore


def main() -> None:
    """Check installation, legacy data upgrades, and startup/shutdown in a child."""
    package = Path(tether.__file__).resolve().parent
    assert package.is_relative_to(Path.cwd() / "installed")
    expected = os.environ["TETHER_EXPECTED_REVISION"]
    init_db()
    database = get_db_url().removeprefix("sqlite:///")
    with closing(sqlite3.connect(database)) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert revision == expected, revision

    store = SessionStore()
    session = store.create_session("external", None)
    store.set_runner_session_id(session.id, "legacy-external")
    config = Config()
    config.set_main_option("script_location", str(package / "db" / "migrations"))
    command.downgrade(config, "9d5e7f1a2b30")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE sessions SET synced_message_count = ?, synced_turn_count = ? WHERE id = ?",
            (500, 2, session.id),
        )
        connection.commit()
    reset_engine()
    init_db()
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT synced_message_count, synced_turn_count FROM sessions WHERE id = ?",
            (session.id,),
        ).fetchone()
        assert row == (None, None), row
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert revision == expected, revision
        connection.execute(
            "UPDATE sessions SET synced_message_count = ? WHERE id = ?",
            (750, session.id),
        )
        connection.commit()
    reset_engine()
    init_db()
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute(
                "SELECT synced_message_count FROM sessions WHERE id = ?", (session.id,)
            ).fetchone()[0]
            == 750
        )

    asyncio.run(check_lifespan())


async def check_lifespan() -> None:
    """Exercise the wheel's real lifespan without runners, models or messaging."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/api/health")
            assert response.status_code == 200, response.text
            assert response.json()["ok"]


if __name__ == "__main__":
    main()
