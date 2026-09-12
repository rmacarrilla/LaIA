import os

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_client import get_client

load_dotenv()

mcp = MCPServer("garmin-activities")


@mcp.tool()
def list_activities(limit: int = 5) -> list[dict]:
    """Devuelve las últimas actividades registradas en Garmin Connect."""
    client = get_client()
    activities = client.get_activities(0, limit)
    assert isinstance(activities, list)

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


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rechaza cualquier petición que no traiga la clave compartida, ya sea en la
    cabecera Authorization (clientes MCP normales) o en el parámetro ?apiKey=
    (enlaces de un clic, que no permiten configurar cabeceras)."""

    async def dispatch(self, request: Request, call_next):
        expected = os.environ["MCP_AUTH_TOKEN"]
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
