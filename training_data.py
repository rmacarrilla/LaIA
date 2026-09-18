"""Agregaciones de datos de Garmin para las tools de coaching.

Selección de métodos y agrupación en tools: ver docs/LaIA-MCP-metodos-garmin.md,
la especificación verificada campo a campo contra una cuenta real de la que sale
este fichero. A diferencia de una versión anterior de este proyecto, aquí no se
usa `client.typed` (el namespace Pydantic que trae la propia librería): el spec
se construyó y verificó contra los dicts crudos de connectapi, con las claves
exactas documentadas en su sección 2 — así que se adopta ese mismo estilo en vez
de mezclar dos formas de leer la misma API.

Regla general: siempre resúmenes o dicts ya compactos, nunca series punto a
punto (FC/potencia por segundo, sueño minuto a minuto, body battery intradía)
— eso es lo que dispara el gasto de tokens que este servidor quiere evitar. La
única excepción es `sesion(detalle=True)`, a propósito y documentada ahí.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from garminconnect import Garmin

# Deliberadamente incompleto: el spec documenta el mecanismo (RPE/10, feel por
# umbral, feedbackPhrase traducido) pero no enumera cada código posible de
# Garmin. Un código no listado se devuelve tal cual (nunca se hace fallar una
# lectura por esto) y se añade aquí según se vaya viendo en uso real — ver
# "Riesgo conocido" en CLAUDE.md.
_FEEL_LABELS: dict[int, str] = {0: "muy flojo", 25: "flojo", 50: "normal", 75: "fuerte", 100: "muy fuerte"}

_FEEDBACK_PHRASES: dict[str, str] = {}

_RECORD_TYPE_LABELS: dict[int, str] = {}


def _field(activity: dict[str, Any], name: str) -> Any:
    """Las dos formas en que llega una actividad no ponen los mismos campos en
    el mismo sitio, comprobado contra la cuenta real: get_activity (detalle)
    los lleva en summaryDTO (ahí viven duration y directWorkoutRpe/Feel, y no
    hay hrTimeInZone_*), mientras que get_activities_by_date (lista) los lleva
    en el nivel de arriba (ahí viven duration y hrTimeInZone_1..5, y no hay
    RPE/Feel). Se mira primero summaryDTO y luego arriba, para que los
    helpers funcionen con cualquiera de las dos (y con la mezcla de ambas que
    arma carga())."""
    summary = activity.get("summaryDTO")
    if isinstance(summary, dict) and summary.get(name) is not None:
        return summary[name]
    return activity.get(name)


def _rpe_feel(activity: dict[str, Any]) -> dict[str, Any]:
    """RPE normalizado (Garmin lo guarda ×10, nunca se devuelve 0 si falta el
    campo), feel traducido a etiqueta, y sRPE = RPE × minutos — la única carga
    fiable en natación, donde la FC en el agua no sirve."""
    raw_rpe = _field(activity, "directWorkoutRpe")
    rpe = raw_rpe / 10 if raw_rpe is not None else None
    raw_feel = _field(activity, "directWorkoutFeel")
    feel = _FEEL_LABELS.get(raw_feel) if raw_feel is not None else None
    duration = _field(activity, "duration")
    srpe = rpe * (duration / 60) if rpe is not None and duration is not None else None
    return {"rpe": rpe, "feel": feel, "srpe": srpe}


def _soft_time_seconds(activity: dict[str, Any]) -> float | None:
    """duración − (suma de las 5 zonas de FC) + zona1 + zona2. La suma de las
    cinco zonas nunca llega a la duración total: todo lo que queda por debajo
    del suelo de zona 1 no se registra en ninguna zona, así que sin esta
    corrección el porcentaje de trabajo suave sale falseado a la baja.

    Devuelve None si la actividad viene solo del detalle (get_activity), que
    no trae hrTimeInZone_* — las zonas solo están en get_activities_by_date."""
    duration = _field(activity, "duration")
    raw_zones = [activity.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    if duration is None or any(z is None for z in raw_zones):
        return None
    zones: list[float] = raw_zones  # type: ignore[assignment]  # narrowed by the check above
    return duration - sum(zones) + zones[0] + zones[1]


# Lo que Garmin mete en cada actividad y no dice nada de entrenamiento. Pesaba
# el 60% de carga(): metadataDTO son metadatos de subida del fichero (versión
# de la app, formato, URLs de la foto de perfil) y splitSummaries es el mismo
# agregado por tramo que el spec ya descarta por calculable desde los splits
# (y para una sesión concreta ya está get_activity_splits en sesion()). El
# resto son duplicados que aparecen al fusionar lista y detalle, o datos del
# dueño de la cuenta, que se sabe de sobra: es quien llama.
# NO se quita activityType/activityTypeDTO: son la misma cosa con nombres
# distintos según la respuesta venga de la lista o del detalle, y sin ellos
# una actividad se queda sin deporte.
_ACTIVITY_NOISE = frozenset(
    {
        "userRoles",
        "ownerId",
        "ownerDisplayName",
        "ownerFullName",
        "ownerProfileImageUrlLarge",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlSmall",
        "userPro",
        "metadataDTO",
        "splitSummaries",
        "summarizedDiveInfo",
        "timeZoneUnitDTO",
        "privacy",
        "eventType",
        "activityUUID",
    }
)


def _sin_claves(dic: dict[str, Any], claves: frozenset[str] | set[str]) -> dict[str, Any]:
    """Quita claves de un dict. Se usa para hacer cumplir de verdad la regla de
    la cabecera de este módulo — no devolver series punto a punto —, que hasta
    ahora estaba escrita pero no aplicada: pasar las respuestas de Garmin tal
    cual colaba los arrays intradía (sueño minuto a minuto, body battery por
    época), que llegaron a ser el 75% del tamaño de estado()."""
    return {k: v for k, v in dic.items() if k not in claves}


def _ritmos(activity: dict[str, Any]) -> dict[str, Any]:
    """El ritmo de una sesión son DOS números distintos, y confundirlos es
    especialmente grave en natación. Comprobado contra la cuenta real:
    `averageSpeed` de Garmin se calcula sobre `movingDuration` (tiempo en
    movimiento), no sobre `duration` (tiempo total). En carrera y bici los dos
    coinciden, pero en una sesión de nado con descansos entre series difieren
    hasta un 80%: 1300 m en 2234 s de reloj con 1225 s nadando son 2:51/100m
    contando descansos o 1:34/100m sin contarlos. Se devuelven los dos, con
    nombre explícito, en vez de dejar que quien lea la respuesta elija sin
    saber que está eligiendo."""
    distancia = _field(activity, "distance")
    duracion = _field(activity, "duration")
    velocidad = _field(activity, "averageSpeed")
    if not distancia:
        return {}

    ritmos: dict[str, Any] = {}
    if duracion:
        ritmos["seg_por_km_total"] = duracion / (distancia / 1000)
        ritmos["seg_por_100m_total"] = duracion / (distancia / 100)
    if velocidad:
        ritmos["seg_por_km_en_movimiento"] = 1000 / velocidad
        ritmos["seg_por_100m_en_movimiento"] = 100 / velocidad
    return {"ritmo": ritmos} if ritmos else {}


def _enrich_activity(activity: dict[str, Any]) -> dict[str, Any]:
    limpia = _sin_claves(activity, _ACTIVITY_NOISE)
    return {
        **limpia,
        **_rpe_feel(activity),
        **_ritmos(activity),
        "soft_time_seconds": _soft_time_seconds(activity),
    }


def _del_dispositivo_principal(mapa: dict[str, Any] | None) -> dict[str, Any]:
    """Varias respuestas de Garmin cuelgan el dato de una clave que es el id
    del reloj (p.ej. latestTrainingStatusData["3461696276"]), imposible de
    adivinar para quien lea la respuesta. Devuelve la entrada del dispositivo
    marcado como principal, o la más reciente por calendarDate si ninguna lo
    marca (cuentas con varios relojes), o {} si no hay nada."""
    entradas = [v for v in (mapa or {}).values() if isinstance(v, dict)]
    if not entradas:
        return {}
    principales = [e for e in entradas if e.get("primaryTrainingDevice")]
    candidatas = principales or entradas
    return max(candidatas, key=lambda e: e.get("calendarDate") or "")


def _etiqueta_de_frase(frase: str | None) -> str | None:
    """"MAINTAINING_2" -> "MAINTAINING". Garmin manda la fase de entrenamiento
    como código numérico (trainingStatus: 4) y solo deja el nombre legible en
    la frase de feedback, con un sufijo numérico de variante. Se usa el prefijo
    de la frase en vez de una tabla propia de códigos: el número se devuelve
    igualmente tal cual, pero inventarle una tabla a partir de un solo valor
    observado arriesgaría etiquetar mal la fase, que es el dato que más pesa."""
    if not frase:
        return None
    cuerpo = frase.rsplit("_", 1)
    return cuerpo[0] if len(cuerpo) == 2 and cuerpo[1].isdigit() else frase


def _translate_feedback_phrases(obj: Any) -> Any:
    """Recorre obj recursivamente y traduce con _FEEDBACK_PHRASES cualquier
    valor bajo una clave 'feedbackPhrase', que termine en 'Feedback'
    (hrvFactorFeedback, recoveryTimeFactorFeedback...) o en 'FeedbackPhrase'
    (trainingStatusFeedbackPhrase, trainingBalanceFeedbackPhrase — las que de
    verdad trae get_training_status) — Garmin los devuelve como códigos tipo
    "HRV_BALANCED_5" sin traducir. Un código no listado en el diccionario se
    deja tal cual."""
    if isinstance(obj, dict):
        return {
            k: (
                _FEEDBACK_PHRASES.get(v, v)
                if isinstance(v, str)
                and (k == "feedbackPhrase" or k.endswith("Feedback") or k.endswith("FeedbackPhrase"))
                else _translate_feedback_phrases(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_translate_feedback_phrases(v) for v in obj]
    return obj


async def _reunir(llamadas: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Lanza en paralelo las llamadas de `llamadas` y devuelve lo que sí se
    obtuvo más una lista de advertencias por cada una que falló.

    Cada tool de lectura agrupa varias llamadas a Garmin, y que una falle es
    lo normal, no lo excepcional: basta con que el deportista no llevara el
    reloj esa noche, o que no haya sincronizado hoy todavía. Sin esto, un
    único fallo tumbaba la tool entera y la conversación se quedaba sin los
    otros seis datos que sí estaban.

    Si fallan TODAS se relanza la primera excepción: eso ya no es un fallo
    parcial, es que no hay nada que devolver, y disfrazarlo de respuesta
    vacía sería peor que fallar. Las excepciones que no son `Exception`
    (cancelación, cierre del proceso) se relanzan siempre: no son fallos de
    datos y tragárselas rompería el apagado del servidor."""
    nombres = list(llamadas)
    resultados = await asyncio.gather(*llamadas.values(), return_exceptions=True)

    datos: dict[str, Any] = {}
    advertencias: list[dict[str, str]] = []
    fallos = 0
    for nombre, resultado in zip(nombres, resultados):
        if isinstance(resultado, BaseException) and not isinstance(resultado, Exception):
            raise resultado
        if isinstance(resultado, Exception):
            fallos += 1
            datos[nombre] = None
            advertencias.append({"dato": nombre, "motivo": f"{type(resultado).__name__}: {resultado}"})
            continue
        datos[nombre] = resultado
        # Garmin distingue mal "falló la llamada" de "todavía no hay dato": a
        # get_morning_training_readiness de un día sin sincronizar responde
        # None, sin error. Sin avisar aquí, quien lea la respuesta ve un null
        # con advertencias vacías y no sabe si es un fallo o es que el reloj
        # aún no ha volcado los datos de hoy.
        if resultado is None:
            advertencias.append(
                {"dato": nombre, "motivo": "sin datos todavía (lo más habitual: el reloj no ha sincronizado aún)"}
            )

    if nombres and fallos == len(nombres):
        primera = next(r for r in resultados if isinstance(r, Exception))
        raise primera
    return datos, advertencias


def _entrenamiento(training_status: dict[str, Any] | None) -> dict[str, Any]:
    """Aplana get_training_status: fase de entrenamiento, reparto de carga
    mensual frente a objetivo y VO2max. Los dos primeros cuelgan de una clave
    que es el id del reloj (ver _del_dispositivo_principal), así que sin esto
    el dato está pero nadie lo encuentra."""
    training_status = training_status or {}
    estado_dev = _del_dispositivo_principal(
        (training_status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")
    )
    balance = _del_dispositivo_principal(
        (training_status.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap")
    )
    vo2 = training_status.get("mostRecentVO2Max") or {}

    return {
        "fase": _etiqueta_de_frase(estado_dev.get("trainingStatusFeedbackPhrase")),
        "fase_codigo": estado_dev.get("trainingStatus"),
        "fase_frase": estado_dev.get("trainingStatusFeedbackPhrase"),
        "deporte": estado_dev.get("sport"),
        "fitness_trend": estado_dev.get("fitnessTrend"),
        "pausado": estado_dev.get("trainingPaused"),
        "desde": estado_dev.get("sinceDate"),
        "carga_mensual": {
            "aerobico_bajo": balance.get("monthlyLoadAerobicLow"),
            "aerobico_bajo_objetivo": [
                balance.get("monthlyLoadAerobicLowTargetMin"),
                balance.get("monthlyLoadAerobicLowTargetMax"),
            ],
            "aerobico_alto": balance.get("monthlyLoadAerobicHigh"),
            "aerobico_alto_objetivo": [
                balance.get("monthlyLoadAerobicHighTargetMin"),
                balance.get("monthlyLoadAerobicHighTargetMax"),
            ],
            "anaerobico": balance.get("monthlyLoadAnaerobic"),
            "anaerobico_objetivo": [
                balance.get("monthlyLoadAnaerobicTargetMin"),
                balance.get("monthlyLoadAnaerobicTargetMax"),
            ],
            "feedback": balance.get("trainingBalanceFeedbackPhrase"),
        },
        "vo2max": {
            "carrera": (vo2.get("generic") or {}).get("vo2MaxPreciseValue"),
            "bici": (vo2.get("cycling") or {}).get("vo2MaxPreciseValue"),
        },
    }


# Lo que interesa de cada noche de get_sleep_daily, que lo entrega metido en
# un sub-dict "values" (nombre de Garmin -> nombre aquí).
_CAMPOS_NOCHE = {
    "sleepScore": "sleep_score",
    "sleepScoreQuality": "calidad",
    "totalSleepTimeInSeconds": "sueno_total_s",
    "deepTime": "profundo_s",
    "lightTime": "ligero_s",
    "remTime": "rem_s",
    "awakeTime": "despierto_s",
    "spO2": "spo2_medio",
    "avgOvernightHrv": "hrv",
    "hrv7dAverage": "hrv_media_7d",
    "hrvStatus": "hrv_estado",
    "restingHeartRate": "fc_reposo",
    "respiration": "respiracion",
    "skinTempC": "temp_piel_c",
    # OJO: sleepNeed viene en MINUTOS (540 = 9h), a diferencia de los tiempos
    # por fase de arriba, que vienen en segundos. Misma respuesta, dos
    # unidades distintas — de ahí el sufijo explícito en el nombre.
    "sleepNeed": "sueno_necesario_min",
    "bodyBatteryChange": "body_battery_cambio",
}


def _noches(sleep_daily: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Una fila por noche con los campos de _CAMPOS_NOCHE sacados de "values".
    Es la serie que permite distinguir un dato malo puntual de un patrón
    (SpO2, HRV, FC en reposo, temperatura de piel), y no cuesta ninguna
    llamada extra: get_sleep_daily ya se pide para el rango de estado()."""
    noches = []
    for fila in sleep_daily or []:
        valores = fila.get("values") or {}
        noche = {"fecha": fila.get("calendarDate")}
        noche.update({destino: valores.get(origen) for origen, destino in _CAMPOS_NOCHE.items()})
        noches.append(noche)
    return noches


_SLEEP_SERIES = frozenset(
    {
        "sleepLevels",
        "sleepMovement",
        "sleepHeartRate",
        "sleepStress",
        "sleepBodyBattery",
        "sleepRestlessMoments",
        "hrvData",
        "breathingDisruptionData",
        "remSleepData",
        "wellnessEpochRespirationAveragesList",
        "wellnessEpochRespirationDataDTOList",
        "wellnessEpochSPO2DataDTOList",
    }
)


def _sueno_anoche(sleep_data: dict[str, Any] | None) -> dict[str, Any]:
    """Resumen de la última noche: los escalares de dailySleepDTO (fases,
    SpO2 medio/mínimo/máximo, respiración) más HRV y FC en reposo. Fuera todas
    las series por época — eran 181 KB de los 241 KB que pesaba estado()."""
    sleep_data = sleep_data or {}
    diario = _sin_claves(sleep_data.get("dailySleepDTO") or {}, _SLEEP_SERIES)
    return {
        **diario,
        "avgOvernightHrv": sleep_data.get("avgOvernightHrv"),
        "restingHeartRate": sleep_data.get("restingHeartRate"),
        "hrvStatus": sleep_data.get("hrvStatus"),
        "bodyBatteryChange": sleep_data.get("bodyBatteryChange"),
        "restlessMomentsCount": sleep_data.get("restlessMomentsCount"),
    }


async def estado(client: Garmin, dias: int = 7, spo2_detalle: bool = False) -> dict[str, Any]:
    """Cómo está el deportista hoy y en los últimos `dias` días — primera
    llamada de casi cualquier conversación sobre si puede entrenar fuerte,
    cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin
    caché de larga duración: a diferencia de capacidad(), esto cambia día a
    día. Se combina con plan() cuando la pregunta es "qué entreno hoy", y con
    capacidad() + carga() en revisiones de bloque o de forma.

    Devuelve, entre otros: `entrenamiento` (fase de entrenamiento de Garmin
    —MAINTAINING, PRODUCTIVE, PEAKING...—, reparto de carga del mes frente a
    su objetivo, y VO2max), `noches` (una fila por noche con sueño, SpO2
    medio, HRV, FC en reposo y temperatura de piel: la serie que dice si un
    dato malo es puntual o un patrón) y `sueno_anoche`.

    spo2_detalle=True añade a cada noche el SpO2 mínimo y máximo, que exige
    una llamada por día — pídelo solo cuando estés investigando una bajada
    concreta, no por rutina."""
    if not 1 <= dias <= 31:
        raise ValueError("dias debe estar entre 1 y 31")

    end = date.today()
    start = end - timedelta(days=dias - 1)
    start_str, end_str, today_str = start.isoformat(), end.isoformat(), end.isoformat()

    calls = {
        "training_readiness": asyncio.to_thread(client.get_morning_training_readiness, today_str),
        "body_battery": asyncio.to_thread(client.get_body_battery, start_str, end_str),
        "sleep_last_night": asyncio.to_thread(client.get_sleep_data, today_str),
        "sleep_daily": asyncio.to_thread(client.get_sleep_daily, start_str, end_str),
        "stats_today": asyncio.to_thread(client.get_stats, today_str),
        "training_status": asyncio.to_thread(client.get_training_status, today_str),
        "activities": asyncio.to_thread(client.get_activities_by_date, start_str, end_str),
    }
    out, advertencias = await _reunir(calls)

    noches = _noches(out["sleep_daily"])
    if spo2_detalle:
        detalles = await asyncio.gather(
            *(asyncio.to_thread(client.get_spo2_data, n["fecha"]) for n in noches),
            return_exceptions=True,
        )
        fallidas = 0
        for noche, detalle in zip(noches, detalles):
            if isinstance(detalle, BaseException) and not isinstance(detalle, Exception):
                raise detalle  # cancelación/apagado: no es un fallo de datos
            if isinstance(detalle, Exception):
                fallidas += 1
            elif detalle:
                noche["spo2_minimo"] = detalle.get("lowestSpO2")
                noche["spo2_medio_dia"] = detalle.get("averageSpO2")
        if fallidas:
            advertencias.append(
                {"dato": "spo2_detalle", "motivo": f"{fallidas} de {len(noches)} noches sin detalle de SpO2"}
            )

    entrenamiento = _entrenamiento(_translate_feedback_phrases(out["training_status"]))
    # get_training_status puede responder el sobre entero con
    # latestTrainingStatusData a None: la llamada no falla, pero no hay fase.
    if out["training_status"] is not None and entrenamiento["fase"] is None:
        advertencias.append(
            {"dato": "entrenamiento.fase", "motivo": "Garmin devolvió el estado de entrenamiento sin datos de dispositivo"}
        )

    return {
        "range": {"start": start_str, "end": end_str},
        "advertencias": advertencias,
        "entrenamiento": entrenamiento,
        "training_readiness": _translate_feedback_phrases(out["training_readiness"]),
        "noches": noches,
        "sueno_anoche": _sueno_anoche(out["sleep_last_night"]),
        # charged/drained por día; el array intradía de body battery no vuelve
        # (regla de la cabecera del módulo). El máximo y mínimo del día de hoy
        # siguen estando en stats_today.
        "body_battery": [
            {"fecha": d.get("date"), "cargado": d.get("charged"), "gastado": d.get("drained")}
            for d in out["body_battery"] or []
        ],
        "stats_today": _sin_claves(out["stats_today"] or {}, {"bodyBatteryActivityEventList"}),
        "activities": [_enrich_activity(a) for a in out["activities"] or []],
    }


_CAPACIDAD_EXTRAS = {"ftp_progresion", "records", "hill_score", "edad_forma", "dispositivo"}


async def capacidad(client: Garmin, extras: list[str] | None = None) -> dict[str, Any]:
    """De qué es capaz el deportista ahora mismo: umbrales, zonas, FTP,
    VO2max, predicciones de carrera. Todo esto cambia en semanas o meses, no
    en minutos — pídela una vez por conversación y reutilízala durante toda
    ella; no tiene sentido volver a llamarla porque haya pasado un rato.

    Incluye `perfil`, con lo que hace falta para prescribir: edad, sexo, peso
    en kg, altura, VO2max de carrera y bici, y FC y ritmo de umbral.

    extras (opcional, bajo demanda, solo si la pregunta concreta lo pide):
    "ftp_progresion" (progresión de FTP en los últimos 6 meses, running y
    cycling — útil en revisión de bloque), "records" (récords personales),
    "hill_score" (capacidad en terreno con subidas — solo si hay desnivel
    relevante en la pregunta), "edad_forma" (edad de forma física, solo si se
    pregunta por ella), "dispositivo" (capacidades del reloj — más de 250
    campos; se pide una vez al conectar la cuenta, no en cada conversación,
    para saber qué endpoints tienen sentido para ese dispositivo concreto)."""
    extras = extras or []
    unknown = set(extras) - _CAPACIDAD_EXTRAS
    if unknown:
        raise ValueError(f"extras desconocidos: {sorted(unknown)}. Usa alguno de {sorted(_CAPACIDAD_EXTRAS)}")

    today = date.today().isoformat()
    six_months_ago = (date.today() - timedelta(days=180)).isoformat()
    ninety_days_ago = (date.today() - timedelta(days=90)).isoformat()

    base_calls = {
        "lactate_threshold": asyncio.to_thread(client.get_lactate_threshold),
        "cycling_ftp": asyncio.to_thread(client.get_cycling_ftp),
        "heart_rate_zones": asyncio.to_thread(client.get_heart_rate_zones),
        "running_power_zones": asyncio.to_thread(client.get_power_zones_for_sport, "running"),
        "cycling_power_zones": asyncio.to_thread(client.get_power_zones_for_sport, "cycling"),
        "max_metrics_trend": asyncio.to_thread(client.get_max_metrics_range, ninety_days_ago, today),
        "endurance_score": asyncio.to_thread(client.get_endurance_score, today),
        "race_predictions": asyncio.to_thread(client.get_race_predictions),
        "user_profile": asyncio.to_thread(client.get_user_profile),
    }
    if "ftp_progresion" in extras:
        base_calls["ftp_progresion_running"] = asyncio.to_thread(
            client.get_functional_threshold_power_range, six_months_ago, today, sport="RUNNING"
        )
        base_calls["ftp_progresion_cycling"] = asyncio.to_thread(
            client.get_functional_threshold_power_range, six_months_ago, today, sport="CYCLING"
        )
    if "records" in extras:
        base_calls["personal_records"] = asyncio.to_thread(client.get_personal_record)
    if "hill_score" in extras:
        base_calls["hill_score"] = asyncio.to_thread(client.get_hill_score, today)
    if "edad_forma" in extras:
        base_calls["fitness_age"] = asyncio.to_thread(client.get_fitnessage_data, today)
    if "dispositivo" in extras:
        base_calls["devices"] = asyncio.to_thread(client.get_devices)

    out, advertencias = await _reunir(base_calls)
    out["advertencias"] = advertencias

    # birthDate cuelga de userData, no del nivel de arriba del perfil.
    user_profile = out.get("user_profile")
    user_data = (user_profile or {}).get("userData") or {} if isinstance(user_profile, dict) else {}
    birth_date = user_data.get("birthDate")
    if birth_date:
        born = date.fromisoformat(birth_date)
        today_date = date.today()
        out["age"] = (
            today_date.year - born.year - ((today_date.month, today_date.day) < (born.month, born.day))
        )
    else:
        out["age"] = None

    # El "speed" de get_lactate_threshold no está en m/s sino en m/s ÷ 10
    # (decámetros por segundo): 0.369 son 3,69 m/s ≈ 4:31 min/km, no 45
    # min/km. Confirmado con la codificación que usa la propia Garmin en los
    # targets de ritmo de un entreno (0.274 ↔ 6:05/km, 0.2545 ↔ 6:33/km).
    lactate = out.get("lactate_threshold")
    speed = (lactate or {}).get("speed_and_heart_rate", {}).get("speed") if isinstance(lactate, dict) else None
    out["lactate_threshold_pace_min_per_km"] = (1000 / (speed * 10) / 60) if speed else None

    # Perfil aplanado: todo esto vive dentro de userData, y el peso viene en
    # gramos (67000 = 67 kg) — misma trampa de unidades que el speed de arriba.
    peso_g = user_data.get("weight")
    out["perfil"] = {
        "edad": out["age"],
        "sexo": user_data.get("gender"),
        "peso_kg": peso_g / 1000 if peso_g else None,
        "altura_cm": user_data.get("height"),
        "vo2max_carrera": user_data.get("vo2MaxRunning"),
        "vo2max_bici": user_data.get("vo2MaxCycling"),
        "fc_umbral": user_data.get("lactateThresholdHeartRate"),
        "ritmo_umbral_min_km": out["lactate_threshold_pace_min_per_km"],
    }

    # Las predicciones llegan como segundos pelados ("time5K": 1302), que sin
    # contexto no dicen nada. Se añade el mismo dato en minutos y el ritmo
    # medio que implica cada una, que es lo que se usa para prescribir.
    predicciones = out.get("race_predictions")
    if isinstance(predicciones, dict):
        out["race_predictions_ritmo"] = {
            nombre: {
                "segundos": predicciones[clave],
                "min_por_km": predicciones[clave] / km / 60,
            }
            for clave, nombre, km in (
                ("time5K", "5k", 5),
                ("time10K", "10k", 10),
                ("timeHalfMarathon", "media_maraton", 21.0975),
                ("timeMarathon", "maraton", 42.195),
            )
            if predicciones.get(clave)
        }

    if "records" in extras and isinstance(out.get("personal_records"), list):
        out["personal_records"] = [
            {**r, "type_label": _RECORD_TYPE_LABELS.get(r.get("typeId"), r.get("prTypeLabelKey"))}
            for r in out["personal_records"]
        ]

    return out


async def carga(client: Garmin, inicio: str, fin: str) -> dict[str, Any]:
    """Qué se ha entrenado en [inicio, fin]: volumen por disciplina, reparto
    de intensidad, progresión semanal. La llamada de la revisión de bloque o
    de mesociclo — pídela después de estado() y capacidad(), no antes (hace
    falta saber cómo está el deportista y de qué es capaz para interpretar el
    volumen que se ha hecho)."""
    _validate_range(inicio, fin)

    base, advertencias = await _reunir(
        {
            "activities": asyncio.to_thread(client.get_activities_by_date, inicio, fin),
            "weekly_stress": asyncio.to_thread(client.get_weekly_stress, fin),
        }
    )
    activities = base["activities"] or []
    weekly_stress = base["weekly_stress"]

    # get_activity por sesión para el RPE/feel, que no vienen en la lista —
    # pero fusionando, no reemplazando: la lista es la única que trae
    # hrTimeInZone_* (ver _field), así que quedarse solo con el detalle
    # perdería el tiempo en zona y con él el tiempo suave real.
    details = await asyncio.gather(
        *(asyncio.to_thread(client.get_activity, str(a["activityId"])) for a in activities),
        return_exceptions=True,
    )
    enriched = []
    sin_detalle = 0
    for activity, detail in zip(activities, details):
        if isinstance(detail, BaseException):
            sin_detalle += 1
            merged = activity
        else:
            merged = {**activity, **detail}
        enriched.append(_enrich_activity(merged))
    if sin_detalle:
        advertencias.append(
            {
                "dato": "detalle_de_actividad",
                "motivo": f"{sin_detalle} de {len(activities)} sesiones sin detalle: van sin RPE ni feel",
            }
        )

    return {
        "range": {"start": inicio, "end": fin},
        "advertencias": advertencias,
        "activities": enriched,
        "weekly_stress": weekly_stress,
    }


async def sesion(
    client: Garmin, activity_id: int, detalle: bool = False, potencia_por_zona: bool = False
) -> dict[str, Any]:
    """Qué pasó en una sesión concreta. Nunca se llama sin un activity_id ya
    obtenido antes de estado() o de plan() — esta tool no busca actividades,
    solo detalla una que ya se conoce.

    detalle=True trae además la serie punto a punto (hasta 2000 puntos) —
    pídelo solo cuando la pregunta sea de deriva cardiaca o desacople, nunca
    por defecto. potencia_por_zona=True trae el reparto de potencia por zona
    — solo tiene sentido en bici (se ignora en cualquier otro deporte) y solo
    si se pregunta específicamente por ese reparto."""
    activity = await asyncio.to_thread(client.get_activity, str(activity_id))
    sport = ((activity.get("activityTypeDTO") or {}).get("typeKey") or "").lower()

    calls: dict[str, Any] = {
        "splits": asyncio.to_thread(client.get_activity_splits, str(activity_id)),
        "gear": asyncio.to_thread(client.get_activity_gear, activity_id),
    }
    if "swim" in sport:
        calls["typed_splits"] = asyncio.to_thread(client.get_activity_typed_splits, str(activity_id))
    if detalle:
        calls["point_series"] = asyncio.to_thread(client.get_activity_details, str(activity_id))
    if potencia_por_zona and ("cycling" in sport or "biking" in sport):
        calls["power_in_timezones"] = asyncio.to_thread(client.get_activity_power_in_timezones, str(activity_id))

    # get_activity ya se pidió arriba sin tolerancia a fallo, a propósito: de él
    # sale el deporte del que dependen las demás llamadas, y sin él no hay
    # sesión que detallar. Lo de aquí abajo sí es complementario.
    extra, advertencias = await _reunir(calls)

    return {"activity": _enrich_activity(activity), "advertencias": advertencias, **extra}


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _validate_range(start_date: str, end_date: str) -> tuple[date, date]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start or (end - start).days > 120:
        raise ValueError("El rango debe ser start <= end y no superar 120 días")
    return start, end


async def plan(client: Garmin, inicio: str, fin: str, workout_id: int | None = None) -> dict[str, Any]:
    """Qué hay agendado en [inicio, fin] y con qué construirlo — junta el
    calendario y la biblioteca de plantillas, para no crear una plantilla
    nueva cuando ya existe una parecida. Siempre antes de cualquier
    escritura (crear_entreno, modificar_entreno, agendar, desagendar,
    borrar_entreno): ninguna se hace sin haber leído antes plan(), y
    cualquier escritura invalida lo leído aquí para esa fecha — si vas a
    encadenar otro cambio en el mismo rango, vuelve a llamar a plan() en vez
    de reutilizar esta lectura.

    workout_id (opcional): además, la estructura completa de esa plantilla
    concreta — pásalo cuando vayas a leerla, clonarla o modificarla."""
    llamadas: dict[str, Any] = {
        "scheduled": find_scheduled_in_range(client, inicio, fin),
        "templates": list_workout_templates(client),
    }
    if workout_id is not None:
        llamadas["workout_detail"] = asyncio.to_thread(client.get_workout_by_id, workout_id)

    out, advertencias = await _reunir(llamadas)

    return {
        "range": {"start": inicio, "end": fin},
        "advertencias": advertencias,
        "scheduled": out["scheduled"] or [],
        "templates": out["templates"] or [],
        "workout_detail": out.get("workout_detail"),
    }


async def find_scheduled_in_range(client: Garmin, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Entradas itemType == "workout" agendadas en [start_date, end_date]:
    scheduled_workout_id (normalizado desde "id" — el mismo dato se llama
    workoutScheduleId en la respuesta de schedule_workout y "id" dentro de
    cada calendarItem de get_scheduled_workouts; se normaliza aquí a un único
    nombre para que quien lo use no tenga que adivinar la clave según de
    dónde venga), fecha y título. calendarItems mezcla varios itemType
    ("nap", "activity", "workout", ...); solo "workout" son entrenamientos
    agendados de verdad.

    Usado por plan() y por workout_builder.unschedule_workouts_in_range."""
    start, end = _validate_range(start_date, end_date)
    months = _months_between(start, end)

    scheduled_by_month = await asyncio.gather(
        *(asyncio.to_thread(client.get_scheduled_workouts, y, m) for y, m in months)
    )

    found = []
    for month_data in scheduled_by_month:
        for item in month_data.get("calendarItems", []):
            item_date = item.get("date")
            if item.get("itemType") != "workout" or item_date is None:
                continue
            if start_date <= item_date <= end_date:
                found.append({"scheduled_workout_id": item["id"], "date": item_date, "title": item.get("title")})
    return found


async def list_workout_templates(client: Garmin) -> list[dict[str, Any]]:
    """Todas las plantillas de la librería de entrenamientos de Garmin, con
    quién las creó/editó ("prod_athletedata", "Shape"...) — para no duplicar
    una plantilla casi idéntica al crear una nueva (plan()), y para poder
    filtrar por origen al borrar en bloque (workout_builder.delete_workouts,
    modo "source").

    Paginación: get_workouts(start, limit) no trae un total ni un cursor, así
    que se sigue pidiendo con start += limit mientras la última página venga
    llena (una página corta significa que ya no queda nada).

    El nombre legible del origen no viene en get_workouts — esa lista solo
    trae workoutProvider (clave corta, p.ej. "athletedata") y consumer (un
    UUID); consumerName (el nombre legible) solo aparece en
    get_workout_by_id, confirmado contra una cuenta real antes de escribir
    esto (no hay documentación oficial del shape, es API no pública). Pedir
    el detalle de cada plantilla sería un get_workout_by_id por plantilla;
    en su lugar se pide una sola vez por cada combinación única de
    (workoutProvider, consumer) vista en la lista — todas las plantillas del
    mismo proveedor comparten el mismo consumer, así que en la práctica son
    muchas menos combinaciones que plantillas. Las plantillas sin proveedor
    (creadas a mano o con crear_entreno) caen en la combinación (None, None),
    con source=None."""
    templates: list[dict[str, Any]] = []
    start = 0
    limit = 100
    while True:
        page = await asyncio.to_thread(client.get_workouts, start, limit)
        templates.extend(page)
        if len(page) < limit:
            break
        start += limit

    keys = {(w.get("workoutProvider"), w.get("consumer")) for w in templates}
    first_by_key = {key: next(w for w in templates if (w.get("workoutProvider"), w.get("consumer")) == key) for key in keys}
    details = await asyncio.gather(
        *(asyncio.to_thread(client.get_workout_by_id, w["workoutId"]) for w in first_by_key.values())
    )
    source_by_key = {key: detail.get("consumerName") for key, detail in zip(first_by_key, details)}

    return [
        {
            "workout_id": w["workoutId"],
            "name": w.get("workoutName"),
            "sport": (w.get("sportType") or {}).get("sportTypeKey"),
            "source": source_by_key[(w.get("workoutProvider"), w.get("consumer"))],
        }
        for w in templates
    ]
