"""Invariante 3: cada tool trabaja con la sesión de Garmin de quien hizo la
petición, y dos usuarios concurrentes no se mezclan.

Por qué está aquí: es el fallo que convertiría un bug tonto en una brecha de
datos de salud. El riesgo concreto de este diseño es el caché de clientes de
`garmin_client`, que es un diccionario a nivel de módulo compartido por todas
las peticiones del proceso: si su clave no incluyera el usuario, dos personas
distintas podrían acabar usando el mismo cliente de Garmin.
"""

import asyncio

import pytest

import garmin_client
import mcp_server


class _ClienteGarminFalso:
    def __init__(self, blob):
        self.blob = blob


@pytest.fixture(autouse=True)
def _sin_cache_sucia():
    """El caché es estado de proceso: se limpia entre tests para que uno no
    contamine al siguiente (que es justo lo que se está probando)."""
    garmin_client._client_cache.clear()
    yield
    garmin_client._client_cache.clear()


async def test_cada_usuario_recibe_su_propia_sesion(db_falso, autenticado, monkeypatch):
    monkeypatch.setattr(garmin_client, "client_from_session", _ClienteGarminFalso)
    db_falso.usuarios = {1: "sesion-de-ana", 2: "sesion-de-luis"}

    autenticado(1)
    cliente_ana = await mcp_server._client_for_current_user()
    autenticado(2)
    cliente_luis = await mcp_server._client_for_current_user()

    assert cliente_ana.blob == "sesion-de-ana"
    assert cliente_luis.blob == "sesion-de-luis"
    assert cliente_ana is not cliente_luis


async def test_el_cache_no_mezcla_usuarios_concurrentes(db_falso, monkeypatch):
    """Dos peticiones a la vez, de dos personas, contra el caché compartido."""
    monkeypatch.setattr(garmin_client, "client_from_session", _ClienteGarminFalso)
    db_falso.usuarios = {1: "sesion-de-ana", 2: "sesion-de-luis"}

    async def como_usuario(user_id):
        # Cada tarea fija su propio token: asyncio.gather copia el contexto,
        # así que esto reproduce dos peticiones HTTP simultáneas.
        from mcp.server.auth.provider import AccessToken
        token = AccessToken(token="t", client_id="c", scopes=[], subject=str(user_id))
        monkeypatch.setattr(mcp_server, "get_access_token", lambda: token)
        return await mcp_server._client_for_current_user()

    ana, luis = await asyncio.gather(como_usuario(1), como_usuario(2))
    assert {ana.blob, luis.blob} == {"sesion-de-ana", "sesion-de-luis"}


def test_la_clave_del_cache_incluye_al_usuario(monkeypatch):
    """Si alguien 'optimiza' la clave quitando el user_id, dos usuarios con
    la misma sesión cacheada compartirían cliente. Aquí se fija por contrato."""
    monkeypatch.setattr(garmin_client, "client_from_session", _ClienteGarminFalso)

    uno = garmin_client.client_from_session_cached(1, "mismo-blob")
    otro = garmin_client.client_from_session_cached(2, "mismo-blob")

    assert uno is not otro, "el caché devolvió el mismo cliente a dos usuarios distintos"
    assert all(isinstance(k, tuple) and len(k) == 2 for k in garmin_client._client_cache)


async def test_sin_token_no_hay_datos(db_falso, autenticado):
    """Una petición sin autenticar no debe caer en un usuario por defecto."""
    autenticado(None)
    with pytest.raises(RuntimeError, match="No hay ningún usuario autenticado"):
        await mcp_server._client_for_current_user()


async def test_usuario_con_token_pero_sin_sesion_guardada(db_falso, autenticado):
    """Token válido de alguien que ya borró su cuenta: no debe colarse a la
    sesión de otro ni devolver un cliente vacío."""
    db_falso.usuarios = {1: "sesion-de-ana"}
    autenticado(999)
    with pytest.raises(RuntimeError, match="Usuario no encontrado"):
        await mcp_server._client_for_current_user()
