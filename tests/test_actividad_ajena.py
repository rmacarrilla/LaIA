"""sesion() rechaza una actividad que no es de esta cuenta.

Por qué está aquí, y por qué es el test más importante de esta tanda: Garmin
sirve por id las actividades públicas de CUALQUIER usuario. Pedir un id
inventado no da 404 — devuelve la sesión de un desconocido con toda
normalidad. Comprobado contra la API real: el id 999999999 devuelve un
ciclismo en Clapham de otra cuenta.

Sin esta comprobación, `sesion()` presentaba esa sesión como si fuera del
deportista, con `advertencias` vacías, y el análisis salía sobre datos de otra
persona. Un error que no falla: solo miente. Y encima expone datos ajenos.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
import training_data


class _ClienteFalso:
    """El display_name lo trae ya cargado el cliente real, así que comprobar
    la propiedad no cuesta ninguna llamada extra."""

    display_name = "RobertoMacarrilla"

    def __init__(self, duenio):
        self.duenio = duenio

    def get_activity(self, activity_id):
        return {
            "activityId": int(activity_id),
            "activityName": "Sesión",
            "activityTypeDTO": {"typeKey": "running"},
            "summaryDTO": {"duration": 3600},
            "metadataDTO": {"userInfoDto": {"displayname": self.duenio}},
        }

    def get_activity_splits(self, activity_id):
        return {"lapDTOs": []}

    def get_activity_gear(self, activity_id):
        return []


async def test_una_actividad_ajena_se_rechaza():
    cliente = _ClienteFalso(duenio="Rebecca21")

    with pytest.raises(training_data.ActividadAjenaError) as err:
        await training_data.sesion(cliente, 999999999)

    assert "otra cuenta" in str(err.value)


async def test_la_propia_pasa():
    cliente = _ClienteFalso(duenio="RobertoMacarrilla")

    resultado = await training_data.sesion(cliente, 24374494333)

    assert resultado["activity"]["activityId"] == 24374494333


async def test_el_modelo_recibe_el_motivo(db_falso, autenticado, monkeypatch):
    """Tiene que llegar como ToolError: si sube como RuntimeError pelado, el
    modelo solo ve 'Error executing tool sesion' y no sabe que el id es de
    otra persona."""
    autenticado(1)
    db_falso.usuarios = {1: "blob"}
    cliente = _ClienteFalso(duenio="Rebecca21")

    async def _cliente():
        return cliente

    monkeypatch.setattr(mcp_server, "_client_for_current_user", _cliente)

    with pytest.raises(ToolError, match="otra cuenta"):
        await mcp_server.sesion(activity_id=999999999)


async def test_sin_datos_de_dueño_no_se_bloquea():
    """Si Garmin no manda el dueño, no se puede afirmar que sea ajena: mejor
    devolverla que rechazar una sesión legítima por un campo que falta."""
    cliente = _ClienteFalso(duenio=None)

    resultado = await training_data.sesion(cliente, 1)

    assert resultado["activity"]["activityId"] == 1
