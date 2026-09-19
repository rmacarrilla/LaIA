"""Servidor MCP multiusuario: cada persona se autentica una vez vía OAuth (con su
propio login de Garmin, resuelto en oauth_provider.py) y a partir de ahí cada
llamada usa la sesión de Garmin de quien la hizo —
mcp.server.auth.middleware.auth_context.get_access_token() da el AccessToken de
la petición en curso, cuyo `subject` es el id de ese usuario en Postgres."""

import asyncio
import functools
import html
import os
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from dotenv import load_dotenv
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import db
import garmin_client
import training_data
import workout_builder
from oauth_provider import FlowExpiredError, GarminOAuthProvider
from rate_limit import RateLimiter, RateLimitMiddleware

load_dotenv()

_public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
_issuer_url = f"https://{_public_domain}" if _public_domain else "http://localhost:8000"

_provider = GarminOAuthProvider(_issuer_url)

# Instrucciones a nivel de servidor: viajan con el conector, así que valen
# para cualquiera que lo use sin que tenga que configurar nada en su Claude.
# Hacen falta porque la mayor parte del payload es la respuesta cruda de
# Garmin, en inglés y con códigos internos: quien pregunta es un deportista,
# no un desarrollador.
_INSTRUCCIONES = """Este conector da acceso a los datos de Garmin Connect de
la persona que te habla, para ejercer de entrenador de triatlón.

Habla siempre en español y para un deportista, no para un técnico. Buena
parte de lo que devuelven estas herramientas es la respuesta cruda de Garmin:
viene en inglés y con códigos internos (`AEROBIC_BASE`, `MAINTAINING_2`,
`RIDER_POSITION`). Tradúcelos y explícalos cuando su significado sea claro;
no hace falta que le enseñes el código al deportista.

Pero **no inventes el significado de un código que no entiendas**. Algunos son
opacos de verdad (`MOD_RT_LOW_SS_MOD_AWAKE_NEG`, `IMPACTING_TEMPO_22`) y una
traducción inventada es peor que no traducir: parece una interpretación
fundada y no lo es. Si no estás seguro, dilo — "Garmin lo etiqueta con un
código que no sé interpretar" — en vez de adivinar.

Lo que el conector calcula por su cuenta ya viene en español y con nombres
explícitos (`ritmo`, `rpe`, `srpe`, `noches`, `entrenamiento`, `advertencias`,
`rtss_estimado`). Eso sí puedes darlo por bueno: está verificado contra la
API real y documentado. `rtss_estimado` es la excepción que conviene
mencionar al darlo, porque lo calcula LaIA y no Garmin.

Mira siempre `advertencias` antes de responder: ahí se dice qué datos no se
pudieron traer y por qué (lo más habitual, que el reloj no haya sincronizado
aún). Un dato ausente no es un dato malo, y confundirlos cambia el consejo."""

mcp = MCPServer(
    "laia",
    instructions=_INSTRUCCIONES,
    auth_server_provider=_provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(_issuer_url),
        resource_server_url=AnyHttpUrl(f"{_issuer_url}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["activities"], default_scopes=["activities"]
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


async def _client_for_current_user() -> garmin_client.Garmin:
    """Resuelve el cliente de Garmin de quien hizo la petición en curso. La
    lectura en Postgres es async; reconstruir el cliente desde el blob implica
    una llamada de red (Garmin recarga el perfil aunque la sesión ya esté
    autenticada), así que va en un hilo aparte igual que las llamadas de las
    tools de abajo. client_from_session_cached evita repetir esa llamada en
    ráfagas de tool calls seguidas (ver garmin_client.py)."""
    token = get_access_token()
    if token is None or token.subject is None:
        raise RuntimeError("No hay ningún usuario autenticado en esta petición")

    user_id = int(token.subject)
    session_blob = await db.get_user_session(user_id)
    if session_blob is None:
        raise RuntimeError("Usuario no encontrado")

    return await asyncio.to_thread(garmin_client.client_from_session_cached, user_id, session_blob)


_T = TypeVar("_T")

# Garmin no tiene una excepción para "esa actividad no es tuya": responde 404
# igual que si no existiera, así que el mensaje no puede prometer distinguirlas.
_ERRORES_GARMIN: list[tuple[type[Exception] | tuple[type[Exception], ...], str]] = [
    (
        GarminConnectTooManyRequestsError,
        "Garmin está limitando las peticiones de esta cuenta (429). Espera unos minutos antes de volver a intentarlo; no es un fallo del conector.",
    ),
    (
        GarminConnectAuthenticationError,
        "La sesión guardada de Garmin ya no vale. Vuelve a conectar el conector para autorizarlo de nuevo.",
    ),
    (
        GarminConnectConnectionError,
        "No se pudo contactar con Garmin. Puede ser un corte temporal de su API: reintenta en un momento.",
    ),
]


def errores_claros(
    fn: Callable[..., Coroutine[Any, Any, _T]],
) -> Callable[..., Coroutine[Any, Any, _T]]:
    """Traduce los fallos previsibles a un mensaje que el modelo pueda leer.

    El SDK solo deja pasar al cliente el texto de un `ToolError`; cualquier
    otra excepción se convierte en un escueto "Error executing tool <nombre>"
    y el motivo se queda en el log del servidor. Eso convertía un id
    inventado, un 429 de Garmin y un corte de red en el mismo mensaje inútil,
    sin forma de saber si reintentar, corregir el id o reconectar.

    `ValueError` incluido a propósito: son las validaciones de las propias
    tools (modos mutuamente excluyentes, rangos de más de 120 días), y decirle
    al modelo qué hizo mal es justo lo que le permite corregirlo solo."""

    @functools.wraps(fn)
    async def envoltorio(*args: Any, **kwargs: Any) -> _T:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except training_data.ActividadAjenaError as err:
            raise ToolError(str(err)) from err
        except ValueError as err:
            raise ToolError(str(err)) from err
        except Exception as err:
            nombre = type(err).__name__
            if nombre == "GarminConnectNotFoundError" or "404" in str(err):
                raise ToolError(
                    "Garmin no encuentra ese id. Comprueba que sea uno devuelto por estado(), "
                    "carga() o plan() y no uno inventado — Garmin responde lo mismo si el id no "
                    "existe que si es de otra cuenta, así que no se puede distinguir."
                ) from err
            for tipos, mensaje in _ERRORES_GARMIN:
                if isinstance(err, tipos):
                    raise ToolError(mensaje) from err
            raise

    return envoltorio


@mcp.tool()
@errores_claros
async def estado(dias: int = 7, spo2_detalle: bool = False) -> dict:
    """Cómo está el deportista hoy y en los últimos `dias` días — primera
    llamada de casi cualquier conversación sobre si puede entrenar fuerte,
    cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin
    caché de larga duración: a diferencia de capacidad(), esto cambia día a
    día. Se combina con plan() cuando la pregunta es "qué entreno hoy", y con
    capacidad() + carga() en revisiones de bloque o de forma.

    Devuelve, entre otros: `entrenamiento` (la fase de entrenamiento de
    Garmin —MAINTAINING, PRODUCTIVE, PEAKING...—, el reparto de carga del mes
    frente a su objetivo, y el VO2max), `noches` (una fila por noche con
    sueño, SpO2 medio, HRV, FC en reposo y temperatura de piel: la serie que
    dice si un dato malo es puntual o un patrón), `sueno_anoche`,
    `training_readiness`, body battery y las actividades del rango.

    spo2_detalle=True añade a cada noche el SpO2 mínimo y máximo, que exige
    una llamada por día — pídelo solo cuando estés investigando una bajada
    concreta. No hace falta pedirlo por rutina: si alguna noche baja del 92%
    de media, se añade solo y se avisa en `advertencias`."""
    client = await _client_for_current_user()
    return await training_data.estado(client, dias, spo2_detalle)


@mcp.tool()
@errores_claros
async def capacidad(extras: list[str] | None = None) -> dict:
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
    pregunta por ella), "peso" (evolución del peso en 90 días, para potencia
    relativa o impacto por zancada), "dispositivo" (qué sabe hacer el reloj,
    resumido en flags; con él se piden además, y solo si el reloj los
    soporta, tolerancia de carrera y datos solares)."""
    client = await _client_for_current_user()
    return await training_data.capacidad(client, extras)


@mcp.tool()
@errores_claros
async def carga(inicio: str, fin: str) -> dict:
    """Qué se ha entrenado en [inicio, fin] (YYYY-MM-DD): volumen por
    disciplina, reparto de intensidad, progresión semanal. La llamada de la
    revisión de bloque o de mesociclo — pídela después de estado() y
    capacidad(), no antes (hace falta saber cómo está el deportista y de qué
    es capaz para interpretar el volumen que se ha hecho)."""
    client = await _client_for_current_user()
    return await training_data.carga(client, inicio, fin)


@mcp.tool()
@errores_claros
async def sesion(activity_id: int, detalle: bool = False, potencia_por_zona: bool = False) -> dict:
    """Qué pasó en una sesión concreta. Nunca se llama sin un activity_id ya
    obtenido antes de estado() o de plan() — esta tool no busca actividades,
    solo detalla una que ya se conoce.

    detalle=True trae además la serie punto a punto (hasta 2000 puntos) —
    pídelo solo cuando la pregunta sea de deriva cardiaca o desacople, nunca
    por defecto. potencia_por_zona=True trae el reparto de potencia por zona
    — solo tiene sentido en bici (se ignora en cualquier otro deporte) y solo
    si se pregunta específicamente por ese reparto.

    Incluye el material usado y sus kilómetros acumulados (`gear_stats`),
    para poder cruzar una molestia con unas zapatillas gastadas."""
    client = await _client_for_current_user()
    return await training_data.sesion(client, activity_id, detalle, potencia_por_zona)


@mcp.tool()
@errores_claros
async def plan(inicio: str, fin: str, workout_id: int | None = None) -> dict:
    """Qué hay agendado en [inicio, fin] (YYYY-MM-DD) y con qué construirlo —
    junta el calendario y la biblioteca de plantillas, para no crear una
    plantilla nueva cuando ya existe una parecida. Siempre antes de cualquier
    escritura (crear_entreno, modificar_entreno, agendar, desagendar,
    borrar_entreno): ninguna se hace sin haber leído antes plan(), y
    cualquier escritura invalida lo leído aquí para esa fecha — si vas a
    encadenar otro cambio en el mismo rango, vuelve a llamar a plan() en vez
    de reutilizar esta lectura.

    workout_id (opcional): además, la estructura completa de esa plantilla
    concreta — pásalo cuando vayas a leerla, clonarla o modificarla."""
    client = await _client_for_current_user()
    return await training_data.plan(client, inicio, fin, workout_id)


@mcp.tool()
@errores_claros
async def crear_entreno(sport: str, name: str, steps: list[dict], agendar_fecha: str | None = None) -> dict:
    """Crea (sube a la librería de entrenamientos de Garmin) un entrenamiento
    estructurado por intervalos. Con `agendar_fecha` (YYYY-MM-DD) lo deja
    además puesto en esa fecha del calendario en la misma llamada, que es lo
    normal cuando el entreno es para un día concreto; sin ella se queda solo
    en la biblioteca y lo pones luego con agendar(). Llama antes a plan()
    para no crear una plantilla casi idéntica a otra que ya existe.

    sport: "running" | "cycling" | "swimming" | "strength".

    steps: lista de pasos, cada uno un dict con "kind":
      - "warmup" | "cooldown" | "recovery" | "interval"
      - "repeat": {"kind": "repeat", "count": N, "steps": [...]} (anidado)
      - "strength_set" (solo sport="strength"):
        {"kind": "strength_set", "category": "BENCH_PRESS", "sets": N,
         "reps": N, "rest_seconds": N, "exercise_name": "" , "weight_kg": N}

    Cómo termina cada paso — una de estas tres:
      - por tiempo: {"duration_seconds": N}
      - por distancia: {"distance_meters": N}
      - por botón de vuelta: {"lap_button": true} — dura hasta que el
        deportista pulsa el botón. Es lo que se usa en series de pista y
        salidas de grupo, donde no hay una duración fija que programar.

    Objetivo de potencia (bici/rodillo), opcional por paso:
      {"power_watts": 220, "power_margin_watts": 20} da un rango de 200-240 W.
      "power_avg": "3s" (por defecto) | "10s" | "30s" | "instantanea" elige
      contra qué potencia se compara; las promediadas evitan que el reloj pite
      por la oscilación de la lectura instantánea aunque la media del
      intervalo esté bien.

    Texto del paso, opcional: {"text": "Serie 1000m a 4:30/km"}. En carrera se
    pone así el ritmo objetivo, en vez de un objetivo con alerta sonora.

    Primera versión sin objetivo de zona (FC/ritmo/potencia) por tramo — solo
    estructura por tiempo/distancia.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    client = await _client_for_current_user()
    return await asyncio.to_thread(
        workout_builder.upload_workout, client, sport, name, steps, agendar_fecha
    )


@mcp.tool()
@errores_claros
async def modificar_entreno(
    workout_id: int,
    sport: str | None = None,
    name: str | None = None,
    steps: list[dict] | None = None,
) -> dict:
    """Modifica una plantilla ya existente conservando su workout_id — lo ya
    agendado con ella no se rompe.

    Pasa solo lo que quieras cambiar: `name` para renombrarla, `steps` para
    cambiar su contenido, o ambos. Lo que no pases se queda como está — parte
    de la estructura real que tiene la plantilla en Garmin, no de cero, así
    que no hace falta reenviar los pasos para cambiar el nombre.

    steps: lista de pasos, cada uno un dict con "kind":
      - "warmup" | "cooldown" | "recovery" | "interval"
      - "repeat": {"kind": "repeat", "count": N, "steps": [...]} (anidado)
      - "strength_set" (solo sport="strength"):
        {"kind": "strength_set", "category": "BENCH_PRESS", "sets": N,
         "reps": N, "rest_seconds": N, "exercise_name": "" , "weight_kg": N}

    Cómo termina cada paso — una de estas tres:
      - por tiempo: {"duration_seconds": N}
      - por distancia: {"distance_meters": N}
      - por botón de vuelta: {"lap_button": true} — dura hasta que el
        deportista pulsa el botón. Es lo que se usa en series de pista y
        salidas de grupo, donde no hay una duración fija que programar.

    Objetivo de potencia (bici/rodillo), opcional por paso:
      {"power_watts": 220, "power_margin_watts": 20} da un rango de 200-240 W.
      "power_avg": "3s" (por defecto) | "10s" | "30s" | "instantanea" elige
      contra qué potencia se compara; las promediadas evitan que el reloj pite
      por la oscilación de la lectura instantánea aunque la media del
      intervalo esté bien.

    Texto del paso, opcional: {"text": "Serie 1000m a 4:30/km"}. En carrera se
    pone así el ritmo objetivo, en vez de un objetivo con alerta sonora.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    if sport is None and name is None and steps is None:
        raise ValueError("Pasa al menos uno de: name, steps, sport")
    client = await _client_for_current_user()
    return await asyncio.to_thread(workout_builder.update_workout, client, workout_id, sport, name, steps)


@mcp.tool()
@errores_claros
async def agendar(workout_id: int, date: str) -> dict:
    """Agenda en el calendario de Garmin, en la fecha dada (YYYY-MM-DD), un
    entrenamiento ya creado con crear_entreno. Se puede llamar varias veces
    con el mismo workout_id para repetir la misma sesión en distintas fechas.

    Si ese día ya tenía algo agendado, lo agenda igual pero lo dice en
    `advertencias`, señalando si además es la misma plantilla (probable
    duplicado). No bloquea: dos sesiones en un día son legítimas y la
    decisión es del deportista. Pero no hace falta llamar a plan() antes solo
    para comprobarlo — si hay conflicto, esta tool te lo dice. Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    client = await _client_for_current_user()
    return await workout_builder.schedule_workout(client, workout_id, date)


@mcp.tool()
@errores_claros
async def desagendar(
    scheduled_workout_ids: list[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_ids: list[int] | None = None,
) -> dict:
    """Desagenda del calendario de Garmin (sin borrar plantillas — usa
    borrar_entreno para eso) uno o varios entrenamientos agendados.

    Dos modos, uno u otro (no combinar):
      - Por ids: scheduled_workout_ids=[...] — **esto sí desagenda**, esos y
        solo esos. Los ids salen de plan() o del modo rango.
      - Por rango: start_date + end_date (YYYY-MM-DD, inclusive; máximo 120
        días), con exclude_ids opcional — **esto NO desagenda nada**: devuelve
        la lista de lo que hay ahí para que se la enseñes al usuario. Si la
        confirma, vuelve a llamar con esos ids.

    El rango no desagenda por diseño: un rango de fechas no deja ver cuántas
    sesiones hay dentro, y quien aprueba la acción tiene derecho a ver la
    lista concreta antes de que desaparezca, no solo la regla.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango.
    """
    # Validar antes de resolver el cliente: una llamada mal formada no debe
    # gastar una lectura a Postgres ni reconstruir la sesión de Garmin.
    has_range = start_date is not None or end_date is not None
    if scheduled_workout_ids is not None:
        if has_range:
            raise ValueError("Pasa scheduled_workout_ids O start_date+end_date, no ambos ni ninguno")
        if exclude_ids:
            raise ValueError("exclude_ids solo aplica en modo rango")
    elif start_date is None or end_date is None:
        raise ValueError("Pasa scheduled_workout_ids O start_date+end_date, no ambos ni ninguno")

    client = await _client_for_current_user()
    if scheduled_workout_ids is not None:
        return await workout_builder.unschedule_workouts_by_id(client, scheduled_workout_ids)
    assert start_date is not None and end_date is not None
    return await workout_builder.candidatas_en_rango(client, start_date, end_date, exclude_ids or [])


@mcp.tool()
@errores_claros
async def borrar_entreno(workout_ids: list[int] | None = None, source: str | None = None) -> dict:
    """Borra de verdad una o varias plantillas de la librería de Garmin — a
    diferencia de desagendar, que solo quita la entrada del calendario, esto
    las elimina y ya no se pueden volver a agendar. Si siguen agendadas,
    desagéndalas primero.

    Dos modos, uno u otro (no combinar):
      - Por ids: workout_ids=[...] — **esto sí borra**, esas plantillas y solo
        esas. Los workout_id salen de plan() o del modo origen.
      - Por origen: source="prod_athletedata" (o el valor que sea) — **esto NO
        borra nada**: devuelve la lista de plantillas con ese origen, con sus
        nombres, para que se la enseñes al usuario. Si la confirma, vuelve a
        llamar con esos workout_ids.

    El origen no borra por diseño: "las de Shape" pueden ser 3 o 38 y eso no
    se ve al aprobar la llamada. Es irreversible, así que lo que se aprueba
    tiene que ser la lista concreta, no la regla que la genera. Si el usuario
    pide algo vago ("borra las viejas"), pídele que concrete en vez de
    decidirlo tú.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    if (workout_ids is None) == (source is None):
        raise ValueError("Pasa workout_ids O source, no ambos ni ninguno")

    client = await _client_for_current_user()
    if workout_ids is not None:
        return await workout_builder.delete_workouts(client, workout_ids)
    assert source is not None
    return await workout_builder.candidatas_por_origen(client, source)


PAGE_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
  input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; box-sizing: border-box; }
  button { margin-top: 1.5rem; padding: 0.6rem 1.2rem; cursor: pointer; }
  .error { color: #b00020; margin-top: 1rem; }
</style>
"""


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Conectar Garmin con Claude</title>{PAGE_STYLE}</head>
<body>
{body}
</body>
</html>""",
        status_code=status_code,
    )


def _login_form(flow_id: str, client_label: str, error: str | None = None) -> HTMLResponse:
    # flow_id y error pueden venir de un POST directo con contenido arbitrario
    # (no solo del formulario que servimos nosotros) — hay que escaparlos antes
    # de meterlos en HTML, o un flow_id como '"><script>...' se ejecutaría en
    # el navegador de quien reciba este enlace (XSS reflejado).
    safe_flow_id = html.escape(flow_id, quote=True)
    safe_client_label = html.escape(client_label)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _page(f"""<h1>Conectar tu Garmin con Claude</h1>
  <p><strong>{safe_client_label}</strong> solicita acceso a tu cuenta de Garmin
     Connect. Podrá <strong>leer</strong> tus actividades y métricas de
     entrenamiento y fisiología (frecuencia cardíaca, sueño, HRV y similares),
     y también <strong>crear, agendar y borrar entrenamientos</strong> en tu
     calendario de Garmin. Solo continúa si reconoces y confías en esta
     aplicación.</p>
  <p>Introduce tu email y contraseña de Garmin Connect para autorizar el acceso:</p>
  {error_html}
  <form method="post" action="/login">
    <input type="hidden" name="flow" value="{safe_flow_id}">
    <label>Email
      <input type="email" name="email" required>
    </label>
    <label>Contraseña
      <input type="password" name="password" required>
    </label>
    <button type="submit">Conectar</button>
  </form>""")


async def _client_label_for_flow(flow_id: str) -> str:
    """Nombre a mostrar en la pantalla de consentimiento: qué aplicación está
    pidiendo acceso. Sin esto, cualquiera podría registrar su propio cliente
    OAuth y mandar un enlace a nuestra pantalla de login real para hacer
    phishing de credenciales de Garmin sin que la víctima note nada raro."""
    pending = await _provider.load_pending_authorization(flow_id)
    if pending is None:
        return "una aplicación"
    client = await _provider.get_client(pending.client_id)
    if client is None:
        return "una aplicación"
    return client.client_name or client.client_id


@mcp.custom_route("/login", methods=["GET"])
async def login_form(request: Request) -> HTMLResponse:
    flow_id = request.query_params.get("flow", "")
    pending = await _provider.load_pending_authorization(flow_id)
    if pending is None:
        return _page(
            "<h1>Enlace caducado</h1><p>Vuelve a intentarlo desde Claude.</p>", status_code=400
        )
    return _login_form(flow_id, await _client_label_for_flow(flow_id))


@mcp.custom_route("/login", methods=["POST"])
async def login_submit(request: Request) -> Response:
    form = await request.form()
    flow_id = str(form.get("flow", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    try:
        redirect_url = await _provider.complete_login(flow_id, email, password)
    except FlowExpiredError:
        # El flow_id ya no existe: reintentar en el mismo formulario no sirve
        # de nada (nunca llegará a un redirect_uri válido), así que se trata
        # igual que el GET con un flow inválido, no como un error de login.
        return _page(
            "<h1>Enlace caducado</h1><p>Vuelve a intentarlo desde Claude.</p>", status_code=400
        )
    except RuntimeError as err:
        return _login_form(flow_id, await _client_label_for_flow(flow_id), error=str(err))

    return RedirectResponse(url=redirect_url, status_code=302)


@mcp.custom_route("/account/delete", methods=["GET", "POST"])
async def account_delete(request: Request) -> Response:
    """Autoservicio de borrado de cuenta (derecho al olvido). Reautentica con
    Garmin en vez de exigir un access token: así solo quien sabe la
    contraseña puede borrar esa cuenta, y no hace falta que este endpoint
    dependa de cómo el SDK propaga la identidad a rutas fuera de /mcp."""
    if request.method == "GET":
        return _page("""<h1>Borrar tu cuenta de LaIA</h1>
  <p>Esto borra tu sesión guardada y revoca todos los tokens de acceso
     emitidos a nombre de tu cuenta de Garmin. No se puede deshacer.</p>
  <form method="post" action="/account/delete">
    <label>Email
      <input type="email" name="email" required>
    </label>
    <label>Contraseña
      <input type="password" name="password" required>
    </label>
    <button type="submit">Borrar mi cuenta</button>
  </form>""")

    form = await request.form()
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    try:
        await _provider.delete_account(email, password)
    except RuntimeError:
        # Mensaje fijo, nunca el texto del error de Garmin: mismo motivo que
        # en /login, evita filtrar si una cuenta existe o no.
        return _page(
            '<h1>No se pudo verificar tu cuenta</h1>'
            '<p><a href="/account/delete">Volver a intentarlo</a></p>',
            status_code=400,
        )

    return _page("<h1>Cuenta borrada</h1><p>Ya no quedan datos tuyos en LaIA.</p>")


async def _connect_db_with_retry(database_url: str, attempts: int = 5) -> None:
    """Reintenta con backoff exponencial si Postgres no responde todavía al
    arrancar (p.ej. un redeploy simultáneo del plugin) — sin esto, una carrera
    de arranque tumba el proceso entero en vez de esperar unos segundos."""
    delay_seconds = 1.0
    for attempt in range(1, attempts + 1):
        try:
            await db.connect(database_url)
            return
        except Exception:
            if attempt == attempts:
                raise
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 30)


_CLEANUP_INTERVAL_SECONDS = 3600


async def _cleanup_loop() -> None:
    """Sin esto, los pending_authorize/authorization codes que nadie completa
    (alguien cierra el navegador a medias) y los tokens ya caducados se
    quedarían en la tabla para siempre — load_object solo limpia lo que
    alguien vuelve a leer, no lo que nadie vuelve a tocar."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            deleted = await db.purge_expired_objects()
            if deleted:
                print(f"[cleanup] borradas {deleted} filas caducadas de oauth_objects")
        except Exception as err:  # nunca debe tumbar el proceso por un fallo puntual
            print(f"[cleanup] fallo al limpiar oauth_objects (se reintenta en {_CLEANUP_INTERVAL_SECONDS}s): {err}")


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT") == "http":
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        # El SDK de MCP rechaza por defecto cualquier Host/Origin distinto de
        # localhost (protección anti DNS-rebinding). Hay que autorizar
        # explícitamente el dominio público que Railway inyecta en runtime.
        transport_security = (
            TransportSecuritySettings(
                allowed_hosts=[_public_domain],
                allowed_origins=[f"https://{_public_domain}"],
            )
            if _public_domain
            else None
        )

        async def _main() -> None:
            await _connect_db_with_retry(os.environ["DATABASE_URL"])
            cleanup_task = asyncio.create_task(_cleanup_loop())

            app = mcp.streamable_http_app(transport_security=transport_security)
            app.add_middleware(
                RateLimitMiddleware,
                limiters={
                    # /login y /account/delete: fuerza bruta de credenciales
                    # de Garmin (ambas terminan llamando a garmin_client.login).
                    ("POST", "/login"): RateLimiter(max_requests=10, window_seconds=900),
                    ("POST", "/account/delete"): RateLimiter(max_requests=10, window_seconds=900),
                    # /register: alta de clientes OAuth, sin autenticación previa
                    # por diseño (RFC 7591) y sin caducidad — limitar el ritmo
                    # evita que se llene la tabla de golpe.
                    ("POST", "/register"): RateLimiter(max_requests=20, window_seconds=3600),
                    # /authorize (GET): con cualquier client_id válido (fácil de
                    # conseguir, /register es público) se pueden generar
                    # pending_authorize sin límite si esto no se cubre aparte.
                    ("GET", "/authorize"): RateLimiter(max_requests=20, window_seconds=900),
                    # /mcp: red de seguridad basta (por IP, no por token) contra un
                    # bucle descontrolado de tool calls — algunas (estado, carga,
                    # plan) disparan decenas de llamadas a Garmin cada una: sin
                    # esto, nada limita cuántas veces se repiten por minuto.
                    ("POST", "/mcp"): RateLimiter(max_requests=60, window_seconds=60),
                },
            )

            config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
            server = uvicorn.Server(config)
            try:
                await server.serve()
            finally:
                cleanup_task.cancel()
                await db.disconnect()

        asyncio.run(_main())
    else:
        mcp.run()
