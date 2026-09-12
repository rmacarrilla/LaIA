import os

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

# Ruta por defecto de garminconnect cuando no se fija GARMIN_TOKENSTORE.
DEFAULT_TOKENSTORE = os.path.expanduser("~/.garminconnect")


def get_tokenstore() -> str:
    return os.getenv("GARMIN_TOKENSTORE", DEFAULT_TOKENSTORE)


def get_client() -> Garmin:
    """Login contra Garmin Connect usando la sesión cacheada en get_tokenstore() si
    es válida, o las credenciales de entorno si no. Se llama en cada tool call del
    servidor MCP: un fallo aquí debe romper solo esa llamada, nunca el proceso
    entero, así que se relanza como una excepción normal (no sys.exit) para que el
    framework MCP la convierta en un error de herramienta."""
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    try:
        client = Garmin(email=email, password=password)
        client.login(get_tokenstore())
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as err:
        raise RuntimeError(f"No se pudo conectar con Garmin: {err}") from err

    return client
