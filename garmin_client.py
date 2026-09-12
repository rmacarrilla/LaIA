from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)


def login(email: str, password: str) -> str:
    """Login real (sin tokenstore, así que nunca reutiliza una sesión cacheada de
    otra cuenta) y devuelve la sesión resultante como string JSON — Garmin.dumps()
    serializa en memoria, sin tocar disco. Ese string es lo que se guarda en
    Postgres para esta cuenta."""
    try:
        client = Garmin(email=email, password=password)
        client.login()
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as err:
        raise RuntimeError(f"No se pudo conectar con Garmin: {err}") from err

    return client.client.dumps()


def client_from_session(session_blob: str) -> Garmin:
    """Reconstruye un cliente ya autenticado a partir de un session_blob guardado
    (ver login()). garminconnect reconoce que el string es JSON inline (no una
    ruta de fichero) y no vuelve a pedir credenciales."""
    client = Garmin(email=None, password=None)
    client.login(session_blob)
    return client
