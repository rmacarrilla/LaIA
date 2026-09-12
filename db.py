"""Capa mínima sobre Postgres (asyncpg): usuarios y objetos OAuth genéricos.

Sin framework de migraciones: a este tamaño, CREATE TABLE IF NOT EXISTS al
arrancar es suficiente y evita una dependencia más.
"""

from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel

_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    garmin_email TEXT UNIQUE NOT NULL,
    session_blob TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_objects (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    data JSONB NOT NULL,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (kind, key)
);
"""


async def connect(database_url: str) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)


async def disconnect() -> None:
    if _pool is not None:
        await _pool.close()


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.connect() no se ha llamado todavía")
    return _pool


async def upsert_user(garmin_email: str, session_blob: str) -> int:
    """Crea el usuario la primera vez que se ve ese email de Garmin, o
    actualiza su sesión si ya existía. El email es la identidad duradera:
    volver a pasar por /login con el mismo email siempre da el mismo user_id."""
    row = await _pool_or_raise().fetchrow(
        """
        INSERT INTO users (garmin_email, session_blob, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (garmin_email)
        DO UPDATE SET session_blob = EXCLUDED.session_blob, updated_at = now()
        RETURNING id
        """,
        garmin_email.strip().lower(),
        session_blob,
    )
    return row["id"]


async def get_user_session(user_id: int) -> str | None:
    row = await _pool_or_raise().fetchrow("SELECT session_blob FROM users WHERE id = $1", user_id)
    return row["session_blob"] if row else None


async def save_object(kind: str, key: str, model: BaseModel, expires_at: datetime | None = None) -> None:
    await _pool_or_raise().execute(
        """
        INSERT INTO oauth_objects (kind, key, data, expires_at)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (kind, key) DO UPDATE SET data = EXCLUDED.data, expires_at = EXCLUDED.expires_at
        """,
        kind,
        key,
        model.model_dump_json(),
        expires_at,
    )


async def load_object(kind: str, key: str, model_cls: type[BaseModel]) -> BaseModel | None:
    """None si no existe o ya caducó (y en ese caso se borra de paso — limpieza
    perezosa, sin necesidad de un cron aparte)."""
    row = await _pool_or_raise().fetchrow(
        "SELECT data, expires_at FROM oauth_objects WHERE kind = $1 AND key = $2", kind, key
    )
    if row is None:
        return None
    if row["expires_at"] is not None and row["expires_at"] < datetime.now(timezone.utc):
        await delete_object(kind, key)
        return None
    return model_cls.model_validate_json(row["data"])


async def delete_object(kind: str, key: str) -> None:
    await _pool_or_raise().execute("DELETE FROM oauth_objects WHERE kind = $1 AND key = $2", kind, key)
