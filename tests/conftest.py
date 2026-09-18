"""Dobles de prueba compartidos.

Los tres invariantes que cubre esta carpeta son de comportamiento, no de
integración: se prueban contra un Postgres y un Garmin falsos, para que
corran en la CI sin credenciales, sin red y en menos de un segundo. Lo que
nunca se falsea es el código bajo prueba — `oauth_provider`, `mcp_server` y
`training_data` se importan de verdad.
"""

import os
import sys
from pathlib import Path

import pytest

# El proyecto no es un paquete instalable: los módulos viven en la raíz.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# crypto_utils exige la clave al usarla. Valor de juguete, solo para tests.
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "clave-de-pruebas-no-usar-en-produccion")


class DbFalso:
    """Reproduce la semántica que importa de db.py: `delete_object` devuelve
    si de verdad borró una fila. De eso depende que un code o un refresh
    reutilizado se rechace, así que un doble que devuelva siempre True
    haría pasar el test con el bug dentro."""

    def __init__(self) -> None:
        self.filas: dict[tuple[str, str], object] = {}
        self.guardados: list[tuple[str, str, object]] = []
        self.usuarios: dict[int, str] = {}
        self.borrados_usuario: list[int] = []

    async def save_object(self, kind, key, model, expires_at=None):
        self.filas[(kind, key)] = model
        self.guardados.append((kind, key, expires_at))

    async def load_object(self, kind, key, model_cls=None):
        return self.filas.get((kind, key))

    async def delete_object(self, kind, key):
        return self.filas.pop((kind, key), None) is not None

    async def upsert_user(self, email, session_blob):
        user_id = abs(hash(email)) % 100000
        self.usuarios[user_id] = session_blob
        return user_id

    async def get_user_session(self, user_id):
        return self.usuarios.get(user_id)

    async def get_user_id_by_email(self, email):
        return abs(hash(email)) % 100000 if email in getattr(self, "emails", set()) else None

    async def delete_user(self, user_id):
        self.borrados_usuario.append(user_id)
        self.usuarios.pop(user_id, None)


@pytest.fixture
def db_falso(monkeypatch):
    """Sustituye db.py entero por el doble, en todos los módulos que lo usan."""
    import db

    doble = DbFalso()
    for nombre in ("save_object", "load_object", "delete_object", "upsert_user",
                   "get_user_session", "get_user_id_by_email", "delete_user"):
        monkeypatch.setattr(db, nombre, getattr(doble, nombre))
    return doble


@pytest.fixture
def autenticado(monkeypatch):
    """Deja fijar quién es el usuario de la petición en curso, como haría el
    middleware del SDK a partir del token."""
    from mcp.server.auth.provider import AccessToken

    import mcp_server

    def como(user_id: int | None):
        token = (
            None
            if user_id is None
            else AccessToken(token="t", client_id="c", scopes=["activities"], subject=str(user_id))
        )
        monkeypatch.setattr(mcp_server, "get_access_token", lambda: token)

    return como
