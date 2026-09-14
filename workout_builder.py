"""Traduce un esquema de entrenamiento genérico (independiente del deporte) a los
modelos tipados de garminconnect.workout y lo sube a Garmin Connect.

Primera iteración: solo pasos por tiempo/distancia, sin target de zona
(FC/ritmo/potencia). La librería no trae helper para construir el dict de
target con zona — solo se documenta el caso NO_TARGET — y Garmin no publica
el shape exacto (API no oficial). Añadir el target de zona requiere antes
verificarlo contra un entrenamiento real: crearlo a mano en la app de Garmin
y leer su JSON con get_workout_by_id.
"""

from __future__ import annotations

import asyncio
from itertools import count
from typing import Any

from garminconnect import Garmin
from garminconnect.workout import (
    BaseWorkout,
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    StrengthWorkout,
    SwimmingWorkout,
    WorkoutSegment,
    create_cooldown_step,
    create_distance_interval_step,
    create_interval_step,
    create_recovery_step,
    create_repeat_group,
    create_strength_set,
    create_warmup_step,
)

import training_data

_WORKOUT_CLASSES: dict[str, type[BaseWorkout]] = {
    "running": RunningWorkout,
    "cycling": CyclingWorkout,
    "swimming": SwimmingWorkout,
    "strength": StrengthWorkout,
}

_SPORT_TYPES: dict[str, dict[str, Any]] = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swimming": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
}

_UPLOAD_METHODS: dict[str, str] = {
    "running": "upload_running_workout",
    "cycling": "upload_cycling_workout",
    "swimming": "upload_swimming_workout",
    "strength": "upload_strength_workout",
}

_TIMED_STEP_BUILDERS = {
    "warmup": create_warmup_step,
    "cooldown": create_cooldown_step,
    "recovery": create_recovery_step,
}


def _duration_seconds(step: dict[str, Any]) -> float:
    try:
        return float(step["duration_seconds"])
    except KeyError:
        raise ValueError(f"El paso {step!r} necesita 'duration_seconds'") from None


def _build_interval(step: dict[str, Any], order: int) -> ExecutableStep:
    if "distance_meters" in step:
        return create_distance_interval_step(float(step["distance_meters"]), order)
    return create_interval_step(_duration_seconds(step), order)


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
        elif kind == "interval":
            built.append(_build_interval(step, next(order_counter)))
        elif kind in _TIMED_STEP_BUILDERS:
            built.append(_TIMED_STEP_BUILDERS[kind](_duration_seconds(step), next(order_counter)))
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
    """Construye y sube el workout a la cuenta de Garmin del usuario. Llamada
    bloqueante — envolver con asyncio.to_thread desde la tool."""
    workout = build_workout(sport, name, steps)
    upload = getattr(client, _UPLOAD_METHODS[sport])
    result = upload(workout)
    return {"workout_id": result.get("workoutId"), "name": result.get("workoutName", name)}


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
    que solo desagendan del calendario."""
    results = await asyncio.gather(
        *(asyncio.to_thread(client.delete_workout, i) for i in workout_ids),
        return_exceptions=True,
    )
    deleted = [i for i, r in zip(workout_ids, results) if not isinstance(r, Exception)]
    failed = [i for i, r in zip(workout_ids, results) if isinstance(r, Exception)]
    return {"deleted_count": len(deleted), "deleted_ids": deleted, "failed_ids": failed}
