"""Traduce un esquema de entrenamiento genérico (independiente del deporte) a los
modelos tipados de garminconnect.workout y lo sube a Garmin Connect.

Un paso puede terminar por tiempo, por distancia o por botón de vuelta
(`lap_button`), y llevar objetivo de potencia con rango calculado a mano
(`power_watts` + `power_margin_watts`) o texto propio (`text`). Los ids y las
claves de texto de condiciones y objetivos salen del catálogo real de Garmin
(`/workout-service/workout/types`), no de suposiciones: tienen que coincidir
exactamente o Garmin rechaza la plantilla.

Sigue sin haber objetivo de FC ni de ritmo con alerta sonora, a propósito: en
carrera el ritmo va como texto en el propio paso para no interrumpir con
pitidos (ver docs/LaIA-MCP-metodos-garmin.md).
"""

from __future__ import annotations

import asyncio
from itertools import count
from typing import Any

from garminconnect import Garmin
from garminconnect.workout import (
    BaseWorkout,
    ConditionType,
    CyclingWorkout,
    ExecutableStep,
    StepType,
    TargetType,
    RepeatGroup,
    RunningWorkout,
    StrengthWorkout,
    SwimmingWorkout,
    WorkoutSegment,
    create_repeat_group,
    create_strength_set,
)

import training_data

_WORKOUT_CLASSES: dict[str, type[BaseWorkout]] = {
    "running": RunningWorkout,
    "cycling": CyclingWorkout,
    "swimming": SwimmingWorkout,
    "strength": StrengthWorkout,
}

# Copiados del catálogo real de Garmin, campo a campo (strength_training tiene
# displayOrder 4, no 5 como estaba antes: Garmin lo aceptaba igual porque lo
# que mira es el sportTypeId, pero no hay motivo para llevar un valor que no
# es el suyo).
_SPORT_TYPES: dict[str, dict[str, Any]] = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swimming": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 4},
}

_UPLOAD_METHODS: dict[str, str] = {
    "running": "upload_running_workout",
    "cycling": "upload_cycling_workout",
    "swimming": "upload_swimming_workout",
    "strength": "upload_strength_workout",
}

# Valores sacados del catálogo real de Garmin (`/workout-service/workout/types`),
# no de una suposición: los ids y las claves de texto tienen que coincidir
# exactamente o Garmin rechaza la plantilla. Los stepTypeId de la librería
# coinciden con el catálogo, así que se reutilizan sus constantes.
_STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": StepType.WARMUP, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": StepType.COOLDOWN, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": StepType.RECOVERY, "stepTypeKey": "recovery", "displayOrder": 4},
}

_END_LAP_BUTTON = {
    "conditionTypeId": ConditionType.LAP_BUTTON,
    "conditionTypeKey": "lap.button",
    "displayOrder": 1,
    "displayable": True,
}
_END_TIME = {"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_END_DISTANCE = {
    "conditionTypeId": ConditionType.DISTANCE,
    "conditionTypeKey": "distance",
    "displayOrder": 3,
    "displayable": True,
}

_TARGET_NONE = {"workoutTargetTypeId": TargetType.NO_TARGET, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
# power.zone vale para un rango de vatios calculado a mano; power.3s/10s/30s
# son el mismo rango pero comparado contra la potencia promediada a esos
# segundos, que es lo que evita los pitidos por la oscilación de la lectura
# instantánea sin tener que ensanchar tanto el rango.
_TARGETS_POTENCIA = {
    "instantanea": {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2},
    "3s": {"workoutTargetTypeId": 10, "workoutTargetTypeKey": "power.3s", "displayOrder": 10},
    "10s": {"workoutTargetTypeId": 11, "workoutTargetTypeKey": "power.10s", "displayOrder": 11},
    "30s": {"workoutTargetTypeId": 12, "workoutTargetTypeKey": "power.30s", "displayOrder": 12},
}


def _duration_seconds(step: dict[str, Any]) -> float:
    try:
        return float(step["duration_seconds"])
    except KeyError:
        raise ValueError(f"El paso {step!r} necesita 'duration_seconds'") from None


def _fin_de_paso(step: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
    """Cómo termina el paso. Por defecto por tiempo o distancia; con
    `lap_button: true` termina cuando el deportista pulsa el botón de vuelta,
    que es como se marcan las series en pista y en las salidas de grupo — ahí
    no hay una duración fija que programar, la marca el propio deportista."""
    if step.get("lap_button"):
        return _END_LAP_BUTTON, None
    if "distance_meters" in step:
        return _END_DISTANCE, float(step["distance_meters"])
    return _END_TIME, _duration_seconds(step)


def _objetivo(step: dict[str, Any]) -> tuple[dict[str, Any], float | None, float | None]:
    """Objetivo de potencia para los pasos que lo lleven, centrado en
    `power_watts` y ensanchado por `power_margin_watts` (220 W con margen de
    20 → 200-240 W). Se calcula a mano en vez de referenciar la zona que el
    deportista tiene configurada, porque esa zona es demasiado estrecha para
    un intervalo: la lectura de potencia oscila y un margen ajustado hace
    pitar el reloj aunque la media del intervalo esté bien.

    `power_avg` elige contra qué se compara: "3s", "10s" o "30s" usan la
    potencia promediada a esos segundos (el propio Garmin los ofrece como
    tipos de objetivo distintos), que ataca la oscilación en origen;
    "instantanea" es el valor crudo. Por defecto 3s.

    En carrera no se pone objetivo con alerta: el ritmo va como texto en el
    propio paso (campo `text`), sin pitido que interrumpa."""
    vatios = step.get("power_watts")
    if vatios is None:
        return _TARGET_NONE, None, None

    margen = float(step.get("power_margin_watts", 0))
    if margen < 0:
        raise ValueError(f"power_margin_watts no puede ser negativo: {step!r}")

    promedio = str(step.get("power_avg", "3s"))
    if promedio not in _TARGETS_POTENCIA:
        raise ValueError(f"power_avg debe ser uno de {sorted(_TARGETS_POTENCIA)}, no {promedio!r}")

    centro = float(vatios)
    return _TARGETS_POTENCIA[promedio], centro - margen, centro + margen


def _construir_paso(kind: str, step: dict[str, Any], order: int) -> ExecutableStep:
    """Construye el paso con el fin y el objetivo que toquen. Se arma el
    ExecutableStep directamente, en vez de usar los helpers de la librería,
    porque esos fijan el fin por tiempo y no dejan poner botón de vuelta."""
    fin, valor_fin = _fin_de_paso(step)
    objetivo, minimo, maximo = _objetivo(step)

    paso = ExecutableStep(
        stepOrder=order,
        stepType=_STEP_TYPES[kind],
        endCondition=fin,
        endConditionValue=valor_fin,
        targetType=objetivo,
    )
    if minimo is not None:
        # targetValueOne/Two es como Garmin guarda el rango del objetivo,
        # confirmado leyendo una plantilla real con objetivo de ritmo.
        # ExecutableStep acepta campos extra (extra="allow"), así que llegan
        # tal cual al JSON que se sube.
        paso.targetValueOne = minimo
        paso.targetValueTwo = maximo
    texto = step.get("text")
    if texto:
        paso.description = str(texto)
    return paso


def _build_steps(
    steps: list[dict[str, Any]], order_counter: count
) -> list[ExecutableStep | RepeatGroup]:
    built: list[ExecutableStep | RepeatGroup] = []
    for step in steps:
        kind = step.get("kind")
        if kind == "repeat":
            group_order = next(order_counter)
            inner = _build_steps(step["steps"], order_counter)
            built.append(create_repeat_group(int(step["count"]), inner, group_order))
        elif kind == "strength_set":
            order = next(order_counter)
            next(order_counter)  # el paso de ejercicio consume order + 1
            next(order_counter)  # el paso de descanso consume order + 2
            built.append(
                create_strength_set(
                    step["category"],
                    order,
                    sets=int(step["sets"]),
                    reps=int(step["reps"]),
                    rest_seconds=float(step["rest_seconds"]),
                    exercise_name=step.get("exercise_name", ""),
                    weight_kg=step.get("weight_kg"),
                )
            )
        elif kind in _STEP_TYPES:
            built.append(_construir_paso(kind, step, next(order_counter)))
        else:
            raise ValueError(f"Tipo de paso desconocido: {kind!r}")
    return built


def _estimated_duration_seconds(steps: list[dict[str, Any]]) -> int:
    total = 0.0
    for step in steps:
        kind = step.get("kind")
        if kind == "repeat":
            total += int(step["count"]) * _estimated_duration_seconds(step["steps"])
        elif kind == "strength_set":
            continue  # basado en repeticiones, no en tiempo
        elif step.get("lap_button"):
            continue  # lo decide el deportista en el momento, no se puede estimar
        elif "duration_seconds" in step:
            total += float(step["duration_seconds"])
        # pasos por distancia: duración desconocida sin el ritmo del atleta, se ignoran
    return int(total)


def build_workout(sport: str, name: str, steps: list[dict[str, Any]]) -> BaseWorkout:
    """Construye (sin subir) un workout tipado a partir del esquema genérico de
    pasos usado por la tool `create_workout`."""
    if sport not in _WORKOUT_CLASSES:
        raise ValueError(f"Deporte no soportado: {sport!r}. Usa uno de {sorted(_WORKOUT_CLASSES)}")

    order_counter = count(1)
    workout_steps = _build_steps(steps, order_counter)
    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=_SPORT_TYPES[sport],
        workoutSteps=workout_steps,
    )
    workout_cls = _WORKOUT_CLASSES[sport]
    return workout_cls(
        workoutName=name,
        estimatedDurationInSecs=_estimated_duration_seconds(steps),
        workoutSegments=[segment],
    )


def upload_workout(client: Garmin, sport: str, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Construye y sube el workout a la cuenta de Garmin del usuario — usado
    por la tool crear_entreno. Llamada bloqueante — envolver con
    asyncio.to_thread desde la tool."""
    workout = build_workout(sport, name, steps)
    upload = getattr(client, _UPLOAD_METHODS[sport])
    result = upload(workout)
    return {"workout_id": result.get("workoutId"), "name": result.get("workoutName", name)}


def update_workout(client: Garmin, workout_id: int, sport: str, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruye la plantilla (mismo esquema genérico de pasos que
    build_workout, usado por crear_entreno) y la manda con update_workout —
    usado por la tool modificar_entreno. Garmin reemplaza la plantilla
    entera vía PUT y fuerza el workoutId del cuerpo a coincidir con el de la
    URL, así que lo ya agendado no se rompe. Llamada bloqueante — envolver
    con asyncio.to_thread desde la tool."""
    workout = build_workout(sport, name, steps)
    result = client.update_workout(workout_id, workout.to_dict())
    return {"workout_id": workout_id, "result": result}


async def unschedule_workouts_in_range(
    client: Garmin, start_date: str, end_date: str, exclude_ids: list[int]
) -> dict[str, Any]:
    """Desagenda (nunca borra plantillas — usar delete_workouts para eso) todo
    lo agendado en [start_date, end_date] salvo los scheduled_workout_id en
    exclude_ids. A diferencia de upload_workout (una sola llamada bloqueante),
    esta orquesta varias llamadas (encontrar candidatos + desagendar cada
    uno), así que es async — igual que training_data.find_scheduled_in_range,
    del que depende. Modo "rango" de la tool unschedule_workouts
    (mcp_server.py); ver también unschedule_workouts_by_id para el modo "ids"."""
    exclude = set(exclude_ids)
    candidates = await training_data.find_scheduled_in_range(client, start_date, end_date)
    to_remove = [c for c in candidates if c["scheduled_workout_id"] not in exclude]

    results = await asyncio.gather(
        *(asyncio.to_thread(client.unschedule_workout, c["scheduled_workout_id"]) for c in to_remove),
        return_exceptions=True,
    )

    removed = [c["scheduled_workout_id"] for c, r in zip(to_remove, results) if not isinstance(r, Exception)]
    failed = [c["scheduled_workout_id"] for c, r in zip(to_remove, results) if isinstance(r, Exception)]

    return {
        "removed_count": len(removed),
        "removed_ids": removed,
        "failed_ids": failed,
        "excluded_ids": sorted(exclude & {c["scheduled_workout_id"] for c in candidates}),
    }


async def unschedule_workouts_by_id(client: Garmin, scheduled_workout_ids: list[int]) -> dict[str, Any]:
    """Desagenda en paralelo cada scheduled_workout_id dado explícitamente —
    modo "ids" de unschedule_workouts (mcp_server.py), sin buscar candidatos
    en el calendario (a diferencia de unschedule_workouts_in_range)."""
    results = await asyncio.gather(
        *(asyncio.to_thread(client.unschedule_workout, i) for i in scheduled_workout_ids),
        return_exceptions=True,
    )
    removed = [i for i, r in zip(scheduled_workout_ids, results) if not isinstance(r, Exception)]
    failed = [i for i, r in zip(scheduled_workout_ids, results) if isinstance(r, Exception)]
    return {"removed_count": len(removed), "removed_ids": removed, "failed_ids": failed}


async def delete_workouts(client: Garmin, workout_ids: list[int]) -> dict[str, Any]:
    """Borra de verdad, en paralelo, cada plantilla de la librería de Garmin
    dada en workout_ids — a diferencia de unschedule_workouts_by_id/_in_range,
    que solo desagendan del calendario. Modo "ids" de la tool borrar_entreno
    (mcp_server.py); ver también delete_workouts_by_source para el modo
    "source"."""
    results = await asyncio.gather(
        *(asyncio.to_thread(client.delete_workout, i) for i in workout_ids),
        return_exceptions=True,
    )
    deleted = [i for i, r in zip(workout_ids, results) if not isinstance(r, Exception)]
    failed = [i for i, r in zip(workout_ids, results) if isinstance(r, Exception)]
    return {"deleted_count": len(deleted), "deleted_ids": deleted, "failed_ids": failed}


async def delete_workouts_by_source(client: Garmin, source: str) -> dict[str, Any]:
    """Borra de verdad todas las plantillas cuyo origen (training_data.
    list_workout_templates, campo "source" = consumerName de Garmin, p.ej.
    "prod_athletedata" o "Shape") coincide exactamente con `source`. Modo
    "source" de la tool borrar_entreno — para limpiar de golpe las plantillas
    que deja una integración de terceros sin tener que conocer cada
    workout_id."""
    templates = await training_data.list_workout_templates(client)
    matching_ids = [t["workout_id"] for t in templates if t["source"] == source]
    result = await delete_workouts(client, matching_ids)
    return {**result, "source": source}
