"""Rate limiting en memoria, sin dependencias externas. Pensado como barrera
barata contra fuerza bruta casual y abuso de registro de clientes OAuth — no
es un WAF ni sirve contra un ataque distribuido de verdad (cada réplica del
proceso llevaría su propio contador; a partir de ahí hace falta un almacén
compartido tipo Redis)."""

import time
from collections import OrderedDict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimiter:
    """Ventana deslizante por clave (normalmente "ruta:ip"). Acotada en memoria
    con desalojo LRU: ante muchas claves distintas (p.ej. un ataque desde
    muchas IPs), las menos usadas recientemente se descartan en vez de crecer
    sin límite."""

    def __init__(self, max_requests: int, window_seconds: float, max_tracked_keys: int = 10_000) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._max_tracked_keys = max_tracked_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        self._hits.move_to_end(key)

        while hits and hits[0] <= now - self._window_seconds:
            hits.popleft()

        if len(hits) >= self._max_requests:
            return False

        hits.append(now)
        while len(self._hits) > self._max_tracked_keys:
            self._hits.popitem(last=False)
        return True


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Aplica un RateLimiter distinto por (método, ruta), identificando al
    llamante por IP. Combinaciones sin límite configurado pasan sin tocar.

    Deliberadamente por (método, ruta) y no solo por ruta: así un GET que solo
    lee (p.ej. ver el formulario de /login varias veces) no consume el mismo
    cupo que el POST que de verdad intenta autenticar."""

    def __init__(self, app, limiters: dict[tuple[str, str], RateLimiter]) -> None:
        super().__init__(app)
        self._limiters = limiters

    async def dispatch(self, request: Request, call_next):
        limiter = self._limiters.get((request.method, request.url.path))
        if limiter is not None and not limiter.allow(client_ip(request)):
            return JSONResponse({"error": "too_many_requests"}, status_code=429)
        return await call_next(request)
