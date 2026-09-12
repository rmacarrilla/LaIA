"""Servidor MCP multiusuario: cada persona se autentica una vez vía OAuth (con su
propio login de Garmin, resuelto en oauth_provider.py) y a partir de ahí cada
llamada usa la sesión de Garmin de quien la hizo —
mcp.server.auth.middleware.auth_context.get_access_token() da el AccessToken de
la petición en curso, cuyo `subject` es el id de ese usuario en Postgres."""

import asyncio
import os

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import db
import garmin_client
from oauth_provider import GarminOAuthProvider

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
    tools de abajo."""
    token = get_access_token()
    if token is None or token.subject is None:
        raise RuntimeError("No hay ningún usuario autenticado en esta petición")

    session_blob = await db.get_user_session(int(token.subject))
    if session_blob is None:
        raise RuntimeError("Usuario no encontrado")

    return await asyncio.to_thread(garmin_client.client_from_session, session_blob)


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


def _login_form(flow_id: str, error: str | None = None) -> HTMLResponse:
    error_html = f'<p class="error">{error}</p>' if error else ""
    return _page(f"""<h1>Conectar tu Garmin con Claude</h1>
  <p>Introduce tu email y contraseña de Garmin Connect para que Claude pueda
     consultar tus actividades.</p>
  {error_html}
  <form method="post" action="/login">
    <input type="hidden" name="flow" value="{flow_id}">
    <label>Email
      <input type="email" name="email" required>
    </label>
    <label>Contraseña
      <input type="password" name="password" required>
    </label>
    <button type="submit">Conectar</button>
  </form>""")


@mcp.custom_route("/login", methods=["GET"])
async def login_form(request: Request) -> HTMLResponse:
    flow_id = request.query_params.get("flow", "")
    pending = await _provider.load_pending_authorization(flow_id)
    if pending is None:
        return _page(
            "<h1>Enlace caducado</h1><p>Vuelve a intentarlo desde Claude.</p>", status_code=400
        )
    return _login_form(flow_id)


@mcp.custom_route("/login", methods=["POST"])
async def login_submit(request: Request) -> Response:
    form = await request.form()
    flow_id = str(form.get("flow", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    try:
        redirect_url = await _provider.complete_login(flow_id, email, password)
    except RuntimeError as err:
        return _login_form(flow_id, error=str(err))

    return RedirectResponse(url=redirect_url, status_code=302)


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
            await db.connect(os.environ["DATABASE_URL"])
            app = mcp.streamable_http_app(transport_security=transport_security)
            config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
            server = uvicorn.Server(config)
            try:
                await server.serve()
            finally:
                await db.disconnect()

        asyncio.run(_main())
    else:
        mcp.run()
