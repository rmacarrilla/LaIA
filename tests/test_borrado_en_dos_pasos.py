"""Invariante añadido: el modo por filtro de `desagendar` y `borrar_entreno`
NUNCA ejecuta; solo el modo por ids.

No estaba en la lista mínima de la auditoría, pero se añade porque protege una
acción irreversible y cuesta muy poco: si alguien "simplifica" estas tools y
vuelve a hacer que un filtro borre directamente, el usuario acabaría aprobando
una regla ("las de Shape") en vez de la lista concreta de lo que desaparece,
que puede ser 3 plantillas o 38 sin que se vea en el diálogo.
"""

import pytest

from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
import training_data
import workout_builder


class _GarminFalso:
    def __init__(self):
        self.borrados = []
        self.desagendados = []

    def delete_workout(self, workout_id):
        self.borrados.append(workout_id)

    def unschedule_workout(self, scheduled_id):
        self.desagendados.append(scheduled_id)


@pytest.fixture
def garmin(monkeypatch, db_falso, autenticado):
    falso = _GarminFalso()
    autenticado(1)
    db_falso.usuarios = {1: "blob"}
    monkeypatch.setattr(mcp_server, "_client_for_current_user", lambda: _async(falso))

    async def plantillas(_client):
        return [
            {"workout_id": 10, "name": "Rodillo Z2", "sport": "cycling", "source": "Shape"},
            {"workout_id": 11, "name": "Series 5x1000", "sport": "running", "source": "Shape"},
            {"workout_id": 12, "name": "Mía", "sport": "running", "source": None},
        ]

    async def agendados(_client, inicio, fin):
        return [{"scheduled_workout_id": 100, "date": inicio, "title": "Rodillo"}]

    monkeypatch.setattr(training_data, "list_workout_templates", plantillas)
    monkeypatch.setattr(training_data, "find_scheduled_in_range", agendados)
    return falso


async def _async(valor):
    return valor


async def test_borrar_por_origen_no_borra_nada(garmin):
    resultado = await mcp_server.borrar_entreno(source="Shape")

    assert garmin.borrados == [], "el modo por origen ejecutó un borrado"
    assert resultado["coinciden"] == 2
    assert [p["workout_id"] for p in resultado["plantillas"]] == [10, 11]
    assert "siguiente_paso" in resultado, "no se explica cómo confirmar"


async def test_borrar_por_ids_si_borra(garmin):
    resultado = await mcp_server.borrar_entreno(workout_ids=[10, 11])

    assert garmin.borrados == [10, 11]
    assert resultado["deleted_count"] == 2


async def test_desagendar_por_rango_no_desagenda_nada(garmin):
    resultado = await mcp_server.desagendar(start_date="2026-09-01", end_date="2026-09-30")

    assert garmin.desagendados == [], "el modo por rango ejecutó un desagendado"
    assert resultado["coinciden"] == 1
    assert "siguiente_paso" in resultado


async def test_desagendar_por_ids_si_desagenda(garmin):
    resultado = await mcp_server.desagendar(scheduled_workout_ids=[100])

    assert garmin.desagendados == [100]
    assert resultado["removed_count"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {},                                             # ni ids ni filtro
        {"workout_ids": [1], "source": "Shape"},        # los dos a la vez
    ],
)
async def test_borrar_exige_un_modo_u_otro(garmin, kwargs):
    # ToolError y no ValueError: el decorador `errores_claros` las traduce
    # para que el texto llegue al modelo. Con un ValueError pelado el SDK
    # responde "Error executing tool borrar_entreno" y se pierde el motivo.
    with pytest.raises(ToolError, match="workout_ids O source"):
        await mcp_server.borrar_entreno(**kwargs)
    assert garmin.borrados == []


async def test_el_filtro_no_alcanza_las_plantillas_propias(garmin):
    """Las creadas a mano tienen source=None, y el modo por origen exige un
    valor concreto: no hay forma de barrerlas con un filtro."""
    with pytest.raises(ToolError):
        await mcp_server.borrar_entreno(source=None)
    assert garmin.borrados == []
