import time

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

_CLIENT_CACHE_TTL_SECONDS = 300  # 5 min: suficiente para varias tool calls seguidas
_client_cache: dict[tuple[int, str], tuple[Garmin, float]] = {}


def login(email: str, password: str) -> str:
    """Login real (sin tokenstore, así que nunca reutiliza una sesión cacheada de
    otra cuenta) y devuelve la sesión resultante como string JSON — Garmin.dumps()
    serializa en memoria, sin tocar disco. Ese string es lo que se guarda en
    Postgres (cifrado, ver crypto_utils.py) para esta cuenta."""
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


def client_from_session_cached(user_id: int, session_blob: str) -> Garmin:
    """Como client_from_session, pero cachea el resultado un rato corto por
    (user_id, session_blob) para no repetir la llamada de red a Garmin
    (client_from_session recarga el perfil aunque la sesión ya sea válida) en
    cada tool call. Usar el blob como parte de la clave invalida el caché solo
    con que cambie (p.ej. tras un nuevo login) — sin tener que coordinar un
    invalidate() explícito entre módulos."""
    key = (user_id, session_blob)
    now = time.monotonic()

    cached = _client_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]

    client = client_from_session(session_blob)
    _client_cache[key] = (client, now + _CLIENT_CACHE_TTL_SECONDS)
    _prune_expired_cache_entries(now)
    return client


def _prune_expired_cache_entries(now: float) -> None:
    expired_keys = [key for key, (_, expires_at) in _client_cache.items() if expires_at <= now]
    for key in expired_keys:
        del _client_cache[key]
