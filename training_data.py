"""Agregaciones de datos de Garmin para las tools de coaching.

Regla general: siempre resúmenes (medias, mínimos/máximos, última lectura),
nunca series punto a punto (FC/potencia por segundo, movimiento de sueño
minuto a minuto, body battery intradía) — eso es lo que dispara el gasto de
tokens que este servidor quiere evitar.

Se usa `client.typed` (namespace tipado con Pydantic que trae la propia
librería) siempre que existe, porque valida y nombra los campos de la
respuesta de Garmin en vez de tener que adivinar claves de un dict crudo.
Para los métodos sin wrapper tipado (training status, FTP, umbral de
carrera, zonas, récords, predicciones de carrera, calendario de
entrenamientos agendados) se devuelve tal cual el dict de Garmin bajo una
clave con nombre: son respuestas ya compactas (un valor o pocos campos por
fecha, no arrays largos), así que pasarlas tal cual no incumple la regla de
"nada punto a punto" y evita adivinar un shape que no está documentado.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from garminconnect import Garmin
from garminconnect.typed import Activity, DailyStats, HrvData


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "avg": round(sum(values) / len(values), 1),
        "min": min(values),
        "max": max(values),
        "latest": values[-1],
    }


def _summarize_activity(activity: Activity) -> dict[str, Any]:
    return {
        "activity_id": activity.activity_id,
        "sport": activity.activity_type.type_key if activity.activity_type else None,
        "date": activity.start_time_local,
        "name": activity.activity_name,
        "duration_seconds": activity.duration,
        "distance_meters": activity.distance,
        "average_hr": activity.average_hr,
        "average_power": activity.avg_power,
        "training_load": activity.activity_training_load,
    }


async def training_snapshot(client: Garmin, days: int = 7) -> dict[str, Any]:
    """Resumen de fisiología (HRV, sueño, FC en reposo, body battery, estrés,
    training readiness) y de la carga de entrenamiento reciente, para que el
    modelo evalúe cómo está el atleta antes de planificar o re-planificar."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    start_str, end_str = start.isoformat(), end.isoformat()
    day_strings = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    daily_stats, hrv_by_day, readiness, activities = await asyncio.gather(
        asyncio.gather(
            *(asyncio.to_thread(client.typed.get_stats, d) for d in day_strings),
            return_exceptions=True,
        ),
        asyncio.gather(
            *(asyncio.to_thread(client.typed.get_hrv_data, d) for d in day_strings),
            return_exceptions=True,
        ),
        asyncio.to_thread(client.typed.get_training_readiness, end_str),
        asyncio.to_thread(client.typed.get_activities_by_date, start_str, end_str),
    )

    valid_stats = [s for s in daily_stats if isinstance(s, DailyStats)]
    valid_hrv = [h for h in hrv_by_day if isinstance(h, HrvData) and h.hrv_summary]

    latest_readiness = readiness[-1] if readiness else None

    return {
        "range": {"start": start_str, "end": end_str},
        "resting_heart_rate": _summary(
            [s.resting_heart_rate for s in valid_stats if s.resting_heart_rate is not None]
        ),
        "sleep_hours_per_night": _summary(
            [round(s.sleeping_seconds / 3600, 1) for s in valid_stats if s.sleeping_seconds]
        ),
        "body_battery_high": _summary(
            [s.body_battery_highest_value for s in valid_stats if s.body_battery_highest_value is not None]
        ),
        "body_battery_low": _summary(
            [s.body_battery_lowest_value for s in valid_stats if s.body_battery_lowest_value is not None]
        ),
        "stress_level": _summary(
            [s.average_stress_level for s in valid_stats if s.average_stress_level is not None]
        ),
        "hrv_last_night_avg": _summary(
            [h.hrv_summary.last_night_avg for h in valid_hrv if h.hrv_summary.last_night_avg is not None]
        ),
        "hrv_status": next((h.hrv_summary.status for h in reversed(valid_hrv) if h.hrv_summary.status), None),
        "training_readiness": (
            {
                "score": latest_readiness.score,
                "level": latest_readiness.level,
                "feedback": latest_readiness.feedback_long,
            }
            if latest_readiness
            else None
        ),
        "activities": [_summarize_activity(a) for a in activities],
    }


async def performance_profile(client: Garmin) -> dict[str, Any]:
    """Datos de capacidad del atleta que cambian poco (FTP, umbral de carrera,
    zonas, VO2max, récords, predicciones de carrera, estado de forma), para
    fijar objetivos de intensidad correctos al generar entrenamientos."""
    today = date.today().isoformat()

    results = await asyncio.gather(
        asyncio.to_thread(client.get_cycling_ftp),
        asyncio.to_thread(client.get_lactate_threshold),
        asyncio.to_thread(client.get_heart_rate_zones),
        asyncio.to_thread(client.get_power_zones_for_sport, "running"),
        asyncio.to_thread(client.get_power_zones_for_sport, "cycling"),
        asyncio.to_thread(client.get_max_metrics, today),
        asyncio.to_thread(client.get_endurance_score, today),
        asyncio.to_thread(client.get_hill_score, today),
        asyncio.to_thread(client.get_personal_record),
        asyncio.to_thread(client.get_race_predictions),
        asyncio.to_thread(client.get_training_status, today),
        return_exceptions=True,
    )
    keys = [
        "cycling_ftp",
        "lactate_threshold",
        "heart_rate_zones",
        "running_power_zones",
        "cycling_power_zones",
        "max_metrics",
        "endurance_score",
        "hill_score",
        "personal_records",
        "race_predictions",
        "training_status",
    ]
    return {key: (None if isinstance(value, Exception) else value) for key, value in zip(keys, results)}


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


async def calendar(client: Garmin, start_date: str, end_date: str) -> dict[str, Any]:
    """Cruza lo agendado en el calendario de Garmin con lo realmente
    entrenado en [start_date, end_date] — para evaluar (planificado vs.
    realizado) y re-planificar (huecos, qué queda por agendar).

    `scheduled` es la respuesta de Garmin tal cual, agregada mes a mes
    (formato no documentado por Garmin); quien la use debe cruzarla por
    fecha/deporte con `completed_activities`."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    months = _months_between(start, end)

    scheduled_by_month, activities = await asyncio.gather(
        asyncio.gather(*(asyncio.to_thread(client.get_scheduled_workouts, y, m) for y, m in months)),
        asyncio.to_thread(client.typed.get_activities_by_date, start_date, end_date),
    )

    return {
        "range": {"start": start_date, "end": end_date},
        "scheduled": scheduled_by_month,
        "completed_activities": [_summarize_activity(a) for a in activities],
    }
