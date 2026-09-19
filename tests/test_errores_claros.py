"""Los fallos previsibles llegan al modelo con un motivo legible.

Por qué está aquí: el SDK solo deja pasar el texto de un `ToolError`.
Cualquier otra excepción se convierte en "Error executing tool <nombre>" y el
motivo se queda en el log del servidor, así que un id inventado, un 429 de
Garmin y un corte de red producían el mismo mensaje inútil. Sin esto, el
modelo no puede saber si reintentar, corregir el id o pedir reconectar.
"""

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server


class _GarminNotFound(Exception):
    """Reproduce garminconnect.exceptions.GarminConnectNotFoundError sin
    importarla: lo que mira el conector es el nombre de la clase y el 404."""
    __name__ = "GarminConnectNotFoundError"


@pytest.fixture
def con_cliente_que_falla(monkeypatch, db_falso, autenticado):
    autenticado(1)
    db_falso.usuarios = {1: "blob"}

    def falla_con(excepcion):
        async def _cliente():
            raise excepcion
        monkeypatch.setattr(mcp_server, "_client_for_current_user", _cliente)

    return falla_con


async def test_id_inexistente_dice_que_no_existe(con_cliente_que_falla):
    err = type("GarminConnectNotFoundError", (Exception,), {})("API Error 404 - HTTP 404 Not Found")
    con_cliente_que_falla(err)

    with pytest.raises(ToolError) as capturado:
        await mcp_server.sesion(activity_id=999999999)

    mensaje = str(capturado.value)
    assert "no encuentra ese id" in mensaje
    # Honesto sobre lo que no se puede distinguir: Garmin responde 404 igual
    # si el id no existe que si es de otra cuenta.
    assert "otra cuenta" in mensaje


@pytest.mark.parametrize(
    "excepcion, esperado",
    [
        (GarminConnectTooManyRequestsError("429"), "limitando las peticiones"),
        (GarminConnectAuthenticationError("auth"), "Vuelve a conectar"),
        (GarminConnectConnectionError("red"), "No se pudo contactar con Garmin"),
    ],
)
async def test_cada_fallo_dice_qué_hacer(con_cliente_que_falla, excepcion, esperado):
    con_cliente_que_falla(excepcion)

    with pytest.raises(ToolError) as capturado:
        await mcp_server.sesion(activity_id=1)

    assert esperado in str(capturado.value)


async def test_las_validaciones_propias_tambien_llegan(db_falso, autenticado):
    """Un modo mal usado tiene que decirle al modelo qué hizo mal, que es lo
    que le permite corregirlo sin preguntar."""
    autenticado(1)
    with pytest.raises(ToolError, match="scheduled_workout_ids O start_date"):
        await mcp_server.desagendar()


async def test_un_fallo_inesperado_no_se_disfraza(con_cliente_que_falla):
    """Lo que no se anticipó debe seguir subiendo como crash: disfrazarlo de
    mensaje amable escondería un bug real en los logs."""
    con_cliente_que_falla(KeyError("campo que no existe"))

    with pytest.raises(KeyError):
        await mcp_server.sesion(activity_id=1)
