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

mcp = MCPServer("garmin-activities")


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


@mcp.custom_route(INTERNAL_LOGIN_PATH, methods=["POST"])
async def internal_login(request: Request) -> JSONResponse:
    """Cambia la cuenta de Garmin activa para get_client(): hace login con las
    credenciales recibidas en un directorio temporal (así garminconnect no puede
    reutilizar ningún token cacheado y autentica de verdad) y solo si tiene éxito
    sustituye la sesión cacheada real por la nueva — un intento fallido no deja el
    MCP sin sesión utilizable. Llamado únicamente por connector_web.py, con un
    secreto distinto (INTERNAL_LOGIN_TOKEN) al de los clientes MCP normales."""
    body = await request.json()
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
        return JSONResponse({"error": "email and password are required"}, status_code=400)

    tokenstore = get_tokenstore()
    final_token_path = token_file_path(tokenstore)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            Garmin(email=email, password=password).login(tmp_dir)
        except (
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        ) as err:
            return JSONResponse({"error": f"login failed: {err}"}, status_code=401)

        final_token_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(token_file_path(tmp_dir)), str(final_token_path))

    return JSONResponse({"status": "ok"})


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rechaza cualquier petición que no traiga la clave compartida correcta: los
    clientes MCP normales (cabecera Authorization o ?apiKey=) usan MCP_AUTH_TOKEN;
    la web de conexión, al llamar a INTERNAL_LOGIN_PATH, usa INTERNAL_LOGIN_TOKEN."""

    async def dispatch(self, request: Request, call_next):
        expected = (
            os.environ["INTERNAL_LOGIN_TOKEN"]
            if request.url.path == INTERNAL_LOGIN_PATH
            else os.environ["MCP_AUTH_TOKEN"]
        )
        authorized = (
            request.headers.get("authorization") == f"Bearer {expected}"
            or request.query_params.get("apiKey") == expected
        )
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT") == "http":
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

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
        app.add_middleware(BearerTokenMiddleware)
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    else:
        mcp.run()
