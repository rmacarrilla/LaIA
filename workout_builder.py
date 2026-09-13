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
