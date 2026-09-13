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
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import db
import garmin_client
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
        issuer_url=_issuer_url,
        resource_server_url=f"{_issuer_url}/mcp",
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
async def list_activities(limit: int = 5) -> list[dict]:
    """Devuelve las últimas actividades registradas en Garmin Connect."""
    client = await _client_for_current_user()
    activities = await asyncio.to_thread(client.get_activities, 0, limit)

    return [
        {
            "activity_id": activity["activityId"],
            "date": activity["startTimeLocal"],
            "name": activity["activityName"],
        }
        for activity in activities
    ]


@mcp.tool()
async def get_activity_detail(activity_id: int) -> dict:
    """Devuelve el detalle de una actividad de Garmin Connect (duración, distancia,
    calorías, frecuencia cardíaca, velocidad media y desnivel). El activity_id se
    obtiene de list_activities."""
    client = await _client_for_current_user()
    activity = await asyncio.to_thread(client.get_activity, str(activity_id))
    summary = activity["summaryDTO"]

    return {
        "name": activity["activityName"],
        "type": activity["activityTypeDTO"]["typeKey"],
        "date": summary["startTimeLocal"],
        "duration_seconds": summary.get("duration"),
        "distance_meters": summary.get("distance"),
        "calories": summary.get("calories"),
        "average_hr": summary.get("averageHR"),
        "max_hr": summary.get("maxHR"),
        "average_speed_mps": summary.get("averageSpeed"),
        "elevation_gain_meters": summary.get("elevationGain"),
    }


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
  <p><strong>{safe_client_label}</strong> solicita acceso a tus actividades y datos
     de entrenamiento de Garmin Connect (nombre, fecha, duración, distancia,
     frecuencia cardíaca y similares). Solo continúa si reconoces y confías en
     esta aplicación.</p>
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
