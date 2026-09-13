"""Capa mínima sobre Postgres (asyncpg): usuarios y objetos OAuth genéricos.

Sin framework de migraciones: a este tamaño, CREATE TABLE/ALTER TABLE
IF NOT EXISTS al arrancar es suficiente y evita una dependencia más.

PII y credenciales cifradas (ver crypto_utils.py): el email de Garmin se
guarda como un hash con clave (nunca en claro) y la sesión de Garmin — que
equivale a una contraseña, da acceso a datos de salud — cifrada con Fernet.
"""

from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel

import crypto_utils

_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_email_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_blob_encrypted BYTEA;

CREATE TABLE IF NOT EXISTS oauth_objects (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    data JSONB NOT NULL,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS oauth_objects_expires_at_idx ON oauth_objects (expires_at);
"""

# Índice único y NOT NULL aplicados aparte (no en el CREATE TABLE) porque las
# filas migradas desde el esquema anterior (ver _migrate_legacy_rows) empiezan
# sin estas columnas rellenas.
_FINALIZE_SCHEMA = """
ALTER TABLE users ALTER COLUMN garmin_email_hash SET NOT NULL;
ALTER TABLE users ALTER COLUMN session_blob_encrypted SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_garmin_email_hash_idx ON users (garmin_email_hash);
"""


async def connect(database_url: str) -> None:
    global _pool
    # min/max_size bajos: esta app tiene tráfico mínimo (uso personal/pequeño
    # grupo), no hace falta el pool de 10 conexiones por defecto de asyncpg
    # ocupando cupo del Postgres compartido.
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
        await _migrate_legacy_rows(conn)
        await conn.execute(_FINALIZE_SCHEMA)


async def _migrate_legacy_rows(conn: asyncpg.Connection) -> None:
    """Rellena garmin_email_hash/session_blob_encrypted a partir de las columnas
    en claro de una versión anterior de este proyecto (antes de cifrar PII), si
    todavía existen. Idempotente: no hace nada si ya no hay columnas legacy."""
    has_legacy = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'garmin_email'
        )
        """
    )
    if not has_legacy:
        return

    rows = await conn.fetch(
        "SELECT id, garmin_email, session_blob FROM users WHERE garmin_email_hash IS NULL"
    )
    for row in rows:
        await conn.execute(
            "UPDATE users SET garmin_email_hash = $1, session_blob_encrypted = $2 WHERE id = $3",
            crypto_utils.hash_email(row["garmin_email"]),
            crypto_utils.encrypt(row["session_blob"]),
            row["id"],
        )
    await conn.execute("ALTER TABLE users DROP COLUMN garmin_email, DROP COLUMN session_blob")


async def disconnect() -> None:
    if _pool is not None:
        await _pool.close()


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.connect() no se ha llamado todavía")
    return _pool


async def upsert_user(garmin_email: str, session_blob: str) -> int:
    """Crea el usuario la primera vez que se ve ese email de Garmin, o
    actualiza su sesión si ya existía. El email (con clave, nunca en claro) es
    la identidad duradera: volver a pasar por /login con el mismo email
    siempre da el mismo user_id."""
    row = await _pool_or_raise().fetchrow(
        """
        INSERT INTO users (garmin_email_hash, session_blob_encrypted, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (garmin_email_hash)
        DO UPDATE SET session_blob_encrypted = EXCLUDED.session_blob_encrypted, updated_at = now()
        RETURNING id
        """,
        crypto_utils.hash_email(garmin_email),
        crypto_utils.encrypt(session_blob),
    )
    return row["id"]


async def get_user_session(user_id: int) -> str | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT session_blob_encrypted FROM users WHERE id = $1", user_id
    )
    return crypto_utils.decrypt(row["session_blob_encrypted"]) if row else None


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
    """None si no existe o ya caducó (y en ese caso se borra de paso)."""
    row = await _pool_or_raise().fetchrow(
        "SELECT data, expires_at FROM oauth_objects WHERE kind = $1 AND key = $2", kind, key
    )
    if row is None:
        return None
    if row["expires_at"] is not None and row["expires_at"] < datetime.now(timezone.utc):
        await delete_object(kind, key)
        return None
    return model_cls.model_validate_json(row["data"])


async def delete_object(kind: str, key: str) -> bool:
    """Devuelve True si había una fila y se borró, False si ya no existía —
    permite detectar el canje/uso concurrente de un mismo objeto de un solo
    uso (p.ej. un authorization code) en vez de asumir a ciegas que el borrado
    de esta llamada fue el único."""
    result = await _pool_or_raise().execute("DELETE FROM oauth_objects WHERE kind = $1 AND key = $2", kind, key)
    return result != "DELETE 0"


async def get_user_id_by_email(garmin_email: str) -> int | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT id FROM users WHERE garmin_email_hash = $1", crypto_utils.hash_email(garmin_email)
    )
    return row["id"] if row else None


async def delete_user(user_id: int) -> None:
    """Borra la cuenta y sus tokens de acceso/refresco (derecho al olvido).
    access/refresh no tienen su propia columna de subject indexada — viven
    dentro del JSONB de oauth_objects — así que se filtran por ahí; a este
    tamaño de tabla (limpiada cada hora por purge_expired_objects) no hace
    falta un índice para esa consulta."""
    async with _pool_or_raise().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
            await conn.execute(
                "DELETE FROM oauth_objects WHERE kind IN ('access', 'refresh') AND data->>'subject' = $1",
                str(user_id),
            )


async def purge_expired_objects() -> int:
    """Borra en bloque lo que ya caducó (pending_authorize/code/access sin usar
    o sin leer nunca) — sin esto, la limpieza perezosa de load_object nunca se
    dispara para filas que nadie vuelve a pedir, y la tabla crece sin límite.
    Se llama periódicamente desde un task de fondo (ver mcp_server.py)."""
    result = await _pool_or_raise().execute(
        "DELETE FROM oauth_objects WHERE expires_at IS NOT NULL AND expires_at < now()"
    )
    return int(result.split()[-1]) if result else 0
