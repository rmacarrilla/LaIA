"""Servidor MCP que expone las actividades de Garmin Connect de una única cuenta
activa. Cada tool llama a get_client() en el momento, sin cachear el cliente, para
que un cambio de cuenta vía /internal/login (ver internal_login) se refleje en la
siguiente llamada sin reiniciar el proceso."""

import asyncio
import hmac
import os
import shutil
import tempfile

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import token_file_path
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_client import get_client, get_tokenstore
from shared_config import INTERNAL_LOGIN_PATH

load_dotenv()

mcp = MCPServer("laia")


@mcp.tool()
def list_activities(limit: int = 5) -> list[dict]:
    """Devuelve las últimas actividades registradas en Garmin Connect."""
    client = get_client()
    activities = client.get_activities(0, limit)

    return [
        {
            "activity_id": activity["activityId"],
            "date": activity["startTimeLocal"],
            "name": activity["activityName"],
        }
        for activity in activities
    ]


@mcp.tool()
def get_activity_detail(activity_id: int) -> dict:
    """Devuelve el detalle de una actividad de Garmin Connect (duración, distancia,
    calorías, frecuencia cardíaca, velocidad media y desnivel). El activity_id se
    obtiene de list_activities."""
    client = get_client()
    activity = client.get_activity(str(activity_id))
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


def _is_credential(value: object) -> bool:
    """True para un string no vacío. request.json() puede devolver cualquier tipo
    JSON, así que hay que comprobarlo antes de pasarlo a Garmin(...)."""
    return isinstance(value, str) and bool(value)


def _switch_active_account(email: str, password: str) -> None:
    """Parte bloqueante de internal_login (login de red + E/S de disco): login en un
    directorio temporal y, solo si tiene éxito, sustituye la sesión cacheada real —
    un intento fallido no deja el MCP sin sesión utilizable. Se ejecuta en un hilo
    aparte (ver internal_login) porque el login de Garmin puede tardar 10-20s
    (esperas anti-bot deliberadas de la propia API) y esto es una función síncrona
    normal: bloquearía el event loop entero si se llamara directamente desde una
    función async, dejando el servidor sin responder a cualquier otra petición
    mientras tanto."""
    tokenstore = get_tokenstore()
    final_token_path = token_file_path(tokenstore)

    with tempfile.TemporaryDirectory() as tmp_dir:
        Garmin(email=email, password=password).login(tmp_dir)  # puede lanzar GarminConnect*Error
        final_token_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(token_file_path(tmp_dir)), str(final_token_path))
        final_token_path.chmod(0o600)  # contiene un refresh token; nada de permisos por defecto


@mcp.custom_route(INTERNAL_LOGIN_PATH, methods=["POST"])
async def internal_login(request: Request) -> JSONResponse:
    """Cambia la cuenta de Garmin activa para get_client(). Llamado únicamente por
    connector_web.py, con un secreto distinto (INTERNAL_LOGIN_TOKEN) al de los
    clientes MCP normales."""
    body = await request.json()
    email = body.get("email")
    password = body.get("password")
    if not _is_credential(email) or not _is_credential(password):
        return JSONResponse({"error": "email and password are required"}, status_code=400)

    try:
        await asyncio.to_thread(_switch_active_account, email, password)
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    ) as err:
        return JSONResponse({"error": f"login failed: {err}"}, status_code=401)

    return JSONResponse({"status": "ok"})


def _matches(received: str | None, expected: str) -> bool:
    """Compara un secreto en tiempo constante: `==` sobre strings compara byte a
    byte y corta en el primer fallo, lo que en teoría permite adivinar un token por
    el tiempo de respuesta. hmac.compare_digest no tiene ese problema."""
    return received is not None and hmac.compare_digest(received, expected)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rechaza cualquier petición que no traiga la clave compartida correcta: los
    clientes MCP normales (cabecera Authorization o ?apiKey=) usan mcp_auth_token;
    la web de conexión, al llamar a INTERNAL_LOGIN_PATH, usa internal_login_token.
    Los tokens se reciben ya resueltos (en vez de leerlos de os.environ en cada
    petición) para que un despliegue sin las variables configuradas falle al
    arrancar, no en la primera petición real."""

    def __init__(self, app, mcp_auth_token: str, internal_login_token: str) -> None:
        super().__init__(app)
        self._mcp_auth_token = mcp_auth_token
        self._internal_login_token = internal_login_token

    async def dispatch(self, request: Request, call_next):
        expected = (
            self._internal_login_token
            if request.url.path == INTERNAL_LOGIN_PATH
            else self._mcp_auth_token
        )
        header_ok = _matches(request.headers.get("authorization"), f"Bearer {expected}")
        query_ok = _matches(request.query_params.get("apiKey"), expected)
        authorized = header_ok or query_ok
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT") == "http":
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        # El SDK de MCP rechaza por defecto cualquier Host/Origin distinto de
        # localhost (protección anti DNS-rebinding). Hay que autorizar
        # explícitamente el dominio público que Railway inyecta en runtime.
        public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        transport_security = (
            TransportSecuritySettings(
                allowed_hosts=[public_domain],
                allowed_origins=[f"https://{public_domain}"],
            )
            if public_domain
            else None
        )

        app = mcp.streamable_http_app(transport_security=transport_security)
        app.add_middleware(
            BearerTokenMiddleware,
            mcp_auth_token=os.environ["MCP_AUTH_TOKEN"],
            internal_login_token=os.environ["INTERNAL_LOGIN_TOKEN"],
        )
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    else:
        mcp.run()
