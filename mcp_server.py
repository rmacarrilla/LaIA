"""Servidor MCP multiusuario: cada persona se autentica una vez vía OAuth (con su
propio login de Garmin, resuelto en oauth_provider.py) y a partir de ahí cada
llamada usa la sesión de Garmin de quien la hizo —
mcp.server.auth.middleware.auth_context.get_access_token() da el AccessToken de
la petición en curso, cuyo `subject` es el id de ese usuario en Postgres."""

import asyncio
import html
import os

from dotenv import load_dotenv
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

mcp = MCPServer(
    "laia",
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


@mcp.tool()
async def estado(dias: int = 7) -> dict:
    """Cómo está el deportista hoy y en los últimos `dias` días — primera
    llamada de casi cualquier conversación sobre si puede entrenar fuerte,
    cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin
    caché de larga duración: a diferencia de capacidad(), esto cambia día a
    día. Se combina con plan() cuando la pregunta es "qué entreno hoy", y con
    capacidad() + carga() en revisiones de bloque o de forma."""
    client = await _client_for_current_user()
    return await training_data.estado(client, dias)


@mcp.tool()
async def capacidad(extras: list[str] | None = None) -> dict:
    """De qué es capaz el deportista ahora mismo: umbrales, zonas, FTP,
    VO2max, predicciones de carrera. Todo esto cambia en semanas o meses, no
    en minutos — pídela una vez por conversación y reutilízala durante toda
    ella; no tiene sentido volver a llamarla porque haya pasado un rato.

    extras (opcional, bajo demanda, solo si la pregunta concreta lo pide):
    "ftp_progresion" (progresión de FTP en los últimos 6 meses, running y
    cycling — útil en revisión de bloque), "records" (récords personales),
    "hill_score" (capacidad en terreno con subidas — solo si hay desnivel
    relevante en la pregunta), "edad_forma" (edad de forma física, solo si se
    pregunta por ella), "dispositivo" (capacidades del reloj — respuesta
    grande; se pide una vez al conectar la cuenta, no en cada conversación)."""
    client = await _client_for_current_user()
    return await training_data.capacidad(client, extras)


@mcp.tool()
async def carga(inicio: str, fin: str) -> dict:
    """Qué se ha entrenado en [inicio, fin] (YYYY-MM-DD): volumen por
    disciplina, reparto de intensidad, progresión semanal. La llamada de la
    revisión de bloque o de mesociclo — pídela después de estado() y
    capacidad(), no antes (hace falta saber cómo está el deportista y de qué
    es capaz para interpretar el volumen que se ha hecho)."""
    client = await _client_for_current_user()
    return await training_data.carga(client, inicio, fin)


@mcp.tool()
async def sesion(activity_id: int, detalle: bool = False, potencia_por_zona: bool = False) -> dict:
    """Qué pasó en una sesión concreta. Nunca se llama sin un activity_id ya
    obtenido antes de estado() o de plan() — esta tool no busca actividades,
    solo detalla una que ya se conoce.

    detalle=True trae además la serie punto a punto (hasta 2000 puntos) —
    pídelo solo cuando la pregunta sea de deriva cardiaca o desacople, nunca
    por defecto. potencia_por_zona=True trae el reparto de potencia por zona
    — solo tiene sentido en bici (se ignora en cualquier otro deporte) y solo
    si se pregunta específicamente por ese reparto."""
    client = await _client_for_current_user()
    return await training_data.sesion(client, activity_id, detalle, potencia_por_zona)


@mcp.tool()
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
async def crear_entreno(sport: str, name: str, steps: list[dict]) -> dict:
    """Crea (sube a la librería de entrenamientos de Garmin) un entrenamiento
    estructurado por intervalos, sin agendarlo todavía — usa agendar() para
    ponerlo en una fecha del calendario. Llama antes a plan() para no crear
    una plantilla casi idéntica a otra que ya existe.

    sport: "running" | "cycling" | "swimming" | "strength".

    steps: lista de pasos, cada uno un dict con "kind":
      - "warmup" | "cooldown" | "recovery": {"kind": ..., "duration_seconds": N}
      - "interval": {"kind": "interval", "duration_seconds": N} o
        {"kind": "interval", "distance_meters": N}
      - "repeat": {"kind": "repeat", "count": N, "steps": [...]} (anidado)
      - "strength_set" (solo sport="strength"):
        {"kind": "strength_set", "category": "BENCH_PRESS", "sets": N,
         "reps": N, "rest_seconds": N, "exercise_name": "" , "weight_kg": N}

    Primera versión sin objetivo de zona (FC/ritmo/potencia) por tramo — solo
    estructura por tiempo/distancia.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    client = await _client_for_current_user()
    return await asyncio.to_thread(workout_builder.upload_workout, client, sport, name, steps)


@mcp.tool()
async def modificar_entreno(workout_id: int, sport: str, name: str, steps: list[dict]) -> dict:
    """Reemplaza la estructura completa de una plantilla ya existente (mismo
    esquema de pasos que crear_entreno) conservando su workout_id — lo ya
    agendado con ella no se rompe. Llama antes a plan(workout_id=...) para
    partir de la estructura real de la plantilla, no de una suposición.

    steps: lista de pasos, cada uno un dict con "kind":
      - "warmup" | "cooldown" | "recovery": {"kind": ..., "duration_seconds": N}
      - "interval": {"kind": "interval", "duration_seconds": N} o
        {"kind": "interval", "distance_meters": N}
      - "repeat": {"kind": "repeat", "count": N, "steps": [...]} (anidado)
      - "strength_set" (solo sport="strength"):
        {"kind": "strength_set", "category": "BENCH_PRESS", "sets": N,
         "reps": N, "rest_seconds": N, "exercise_name": "" , "weight_kg": N}

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    client = await _client_for_current_user()
    return await asyncio.to_thread(workout_builder.update_workout, client, workout_id, sport, name, steps)


@mcp.tool()
async def agendar(workout_id: int, date: str) -> dict:
    """Agenda en el calendario de Garmin, en la fecha dada (YYYY-MM-DD), un
    entrenamiento ya creado con crear_entreno. Se puede llamar varias veces
    con el mismo workout_id para repetir la misma sesión en distintas fechas.
    Llama antes a plan() para comprobar que la fecha está libre. Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    client = await _client_for_current_user()
    return await asyncio.to_thread(client.schedule_workout, workout_id, date)


@mcp.tool()
async def desagendar(
    scheduled_workout_ids: list[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_ids: list[int] | None = None,
) -> dict:
    """Desagenda del calendario de Garmin (sin borrar plantillas — usa
    borrar_entreno para eso) uno o varios entrenamientos agendados. Llama
    antes a plan() para saber qué scheduled_workout_id tocar.

    Dos modos, uno u otro (no combinar):
      - Por ids: scheduled_workout_ids=[...] (los que da plan()).
      - Por rango: start_date + end_date (YYYY-MM-DD, inclusive; máximo 120
        días); exclude_ids opcional para no tocar algunas entradas del rango.

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
    return await workout_builder.unschedule_workouts_in_range(client, start_date, end_date, exclude_ids or [])


@mcp.tool()
async def borrar_entreno(workout_ids: list[int] | None = None, source: str | None = None) -> dict:
    """Borra de verdad una o varias plantillas de la librería de Garmin — a
    diferencia de desagendar, que solo quita la entrada del calendario, esto
    las elimina y ya no se pueden volver a agendar. Si siguen agendadas,
    desagéndalas primero.

    Dos modos, uno u otro (no combinar):
      - Por ids: workout_ids=[...] (los que da plan(), campo workout_id).
      - Por origen: source="prod_athletedata" (o el valor que sea) — borra
        todas las plantillas de ese origen (campo "source" de plan()), para
        limpiar de golpe lo que deja una integración de terceros.

    Tras escribir, lo que devolvió plan() para esa fecha queda
    desactualizado: vuelve a llamarlo antes del siguiente cambio en el
    mismo rango."""
    if (workout_ids is None) == (source is None):
        raise ValueError("Pasa workout_ids O source, no ambos ni ninguno")

    client = await _client_for_current_user()
    if workout_ids is not None:
        return await workout_builder.delete_workouts(client, workout_ids)
    assert source is not None
    return await workout_builder.delete_workouts_by_source(client, source)


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
