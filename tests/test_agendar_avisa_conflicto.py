"""agendar() avisa si ese día ya había algo, sin bloquear.

Por qué está aquí: el aviso tiene que salir de la propia herramienta. Si
depende de acordarse de llamar a plan() antes, no es una salvaguarda — y el
síntoma real fue justo ese: dos veces el mismo entreno el mismo día, aceptado
en silencio.
"""

import pytest

import mcp_server
import training_data
import workout_builder


class _GarminFalso:
    def __init__(self):
        self.agendados = []

    def schedule_workout(self, workout_id, fecha):
        self.agendados.append((workout_id, fecha))
        return {"workoutScheduleId": 555, "workout": {"workoutName": "Rodillo Z2"}}


@pytest.fixture
def escenario(monkeypatch, db_falso, autenticado):
    autenticado(1)
    db_falso.usuarios = {1: "blob"}
    falso = _GarminFalso()

    async def cliente():
        return falso

    monkeypatch.setattr(mcp_server, "_client_for_current_user", cliente)

    def con_calendario(entradas):
        async def agendados(_client, inicio, fin):
            return entradas
        monkeypatch.setattr(training_data, "find_scheduled_in_range", agendados)

    return falso, con_calendario


async def test_dia_libre_no_avisa(escenario):
    falso, con_calendario = escenario
    con_calendario([])

    resultado = await mcp_server.agendar(workout_id=10, date="2026-10-01")

    assert resultado["advertencias"] == []
    assert falso.agendados == [(10, "2026-10-01")]
    assert resultado["scheduled_workout_id"] == 555


async def test_dia_ocupado_avisa_pero_agenda(escenario):
    falso, con_calendario = escenario
    con_calendario([{"scheduled_workout_id": 1, "date": "2026-10-01", "title": "Series 5x1000"}])

    resultado = await mcp_server.agendar(workout_id=10, date="2026-10-01")

    assert falso.agendados == [(10, "2026-10-01")], "no debe bloquear: dos sesiones en un día son legítimas"
    assert len(resultado["advertencias"]) == 1
    aviso = resultado["advertencias"][0]
    assert aviso["dato"] == "conflicto_de_calendario"
    assert "Series 5x1000" in aviso["motivo"]


async def test_duplicado_de_la_misma_plantilla_se_señala(escenario):
    """El caso que se reportó: la misma sesión agendada dos veces el mismo día."""
    falso, con_calendario = escenario
    con_calendario([{"scheduled_workout_id": 1, "date": "2026-10-01", "title": "Rodillo Z2"}])

    resultado = await mcp_server.agendar(workout_id=10, date="2026-10-01")

    motivo = resultado["advertencias"][0]["motivo"]
    assert "duplicado" in motivo, "no distingue un duplicado de dos entrenos distintos el mismo día"
