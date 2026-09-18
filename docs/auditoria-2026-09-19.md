# Auditoría técnica LaIA — 2026-09-19

Ejecutada con `docs/prompt-auditoria-laia.md` contra el código real del repo
(no de memoria), commit base `73b6eb0`. **Esta es la primera auditoría de la
que queda informe escrito**: úsala como línea base y en la siguiente divide
los hallazgos en "ya conocidos y sin resolver", "resueltos desde entonces" y
"nuevos".

## Resumen ejecutivo

El conector ha crecido mucho (10 tools, ~2.700 líneas) y las correcciones de
rondas anteriores siguen en pie y verificadas en el código real: PKCE
obligatorio, canje atómico del authorization code, mensajes de login
genéricos, borrado de cuenta autoservicio y rate limiting por método+ruta.
Sin CVEs en ninguna dependencia de runtime. Nada bloqueaba el despliegue.
Se encontraron dos fallos en el ciclo de vida del refresh token —**ambos
corregidos en esta misma pasada**— y queda como riesgo estructural principal
la ausencia total de tests y CI.

## Hallazgos

| Sev. | Archivo | Descripción | Estado |
|---|---|---|---|
| Alto | `oauth_provider.py` | Rotación del refresh token no atómica: se ignoraba el valor de retorno de `delete_object`, así que dos canjes concurrentes del mismo token recibían ambos un par válido. Era el mismo fallo ya corregido para el authorization code, sin aplicar aquí. | **Resuelto** en `2026-09-19` |
| Alto | `oauth_provider.py`, `db.py` | Los refresh tokens no caducaban: sin `expires_at`, y `purge_expired_objects` solo borra filas que lo tengan. Un token filtrado servía indefinidamente y las filas —con el id de usuario dentro— se acumulaban sin retención. | **Resuelto**: TTL de 30 días |
| Alto | (repo) | Sin tests ni CI: no existen `tests/` ni `.github/`. En un proyecto con credenciales de terceros y datos de salud, cualquier regresión llega a producción sin red. | **Abierto** |
| Medio | `mcp_server.py` | Suplantación en la pantalla de consentimiento: `/register` es abierto por diseño y el `client_name` lo elige quien registra. Se escapa bien (sin XSS), pero un cliente malicioso puede llamarse "Claude". Mitigación propuesta: mostrar también el host del `redirect_uri`, mucho más difícil de disfrazar. | **Abierto** |
| Medio | `oauth_provider.py` | Los clientes OAuth registrados no caducan. El rate limit (20/h por IP) frena el ritmo, no el total acumulado. | **Abierto** |
| Bajo | `crypto_utils.py` | La clave Fernet se deriva con `sha256()` simple, sin KDF ni sal. Correcto con un valor aleatorio largo (lo que indica `.env.example`); débil si el operador pone una frase corta. | **Aceptado** |
| Bajo | `.env.example` | Deriva: el código lee `MCP_TRANSPORT`, `PORT` y `RAILWAY_PUBLIC_DOMAIN` y ninguna está documentada. Ningún valor de ejemplo es un secreto real. | **Abierto** |
| Bajo | `requirements.txt` | `garminconnect` en 0.3.15, disponible 0.3.16. No es CVE, es deriva en una librería no oficial. | **Abierto** |

## Verificado y correcto — no volver a marcarlo como hallazgo

Comprobado leyendo el código y el SDK instalado, no dado por bueno:

- **PKCE obligatorio y solo S256**: `code_challenge: str = Field(...)` sin
  defecto en el handler de `/authorize`; `/token` compara el SHA-256 del
  `code_verifier` contra el challenge. No es "se acepta si lo mandan".
- **`redirect_uri`** validado por igualdad exacta contra las registradas
  (`redirect_uri not in self.redirect_uris`), con rechazo si no hay ninguna.
- **Authorization code** de un solo uso, atómico vía el booleano de
  `delete_object`.
- **Contraseña de Garmin**: no se registra en ningún log ni excepción, no se
  persiste, y no queda en el caché de clientes (que se construyen con
  `password=None`). El error de login es un mensaje fijo tanto en `/login`
  como en `/account/delete`, así que no permite enumerar cuentas.
- **Aislamiento entre usuarios**: el caché de clientes va por
  `(user_id, session_blob)` y cada tool resuelve al usuario desde el token de
  la petición en curso (`get_access_token().subject`), nunca de un global.
- **Rate limiter en memoria**: sigue siendo válido porque Railway está en
  **1 réplica** (confirmado contra la API de Railway el día de la auditoría).
  Si eso cambia, deja de proteger: es el primer punto a revisar.
- **Fernet** con IV aleatorio por cifrado, sin reutilización de nonce.
- **Borrado de cuenta** (RGPD art. 17) operativo y autoservicio, arrastrando
  access/refresh/code filtrando por `subject` en el JSONB.
- **Dependencias de runtime sin CVEs** (`pip-audit`: solo aparece `pip`, que
  es herramienta de build).
- **asyncpg**: todo pasa por el pool; los dos `acquire()` usan `async with`.

## Limitaciones conocidas aceptadas

No se van a arreglar ahora, a propósito:

- `/register` abierto sin autenticación previa (RFC 7591), mitigado con rate
  limit y con la pantalla de consentimiento.
- Rate limiter en memoria y de un solo proceso (válido mientras haya una
  réplica).
- Sin rotación de `SESSION_ENCRYPTION_KEY`: cambiarla invalida todas las
  sesiones guardadas.
- Dependencia de `garminconnect`, librería no oficial sujeta a que Garmin no
  cambie su API interna.
- `_RECORD_TYPE_LABELS` y `_FEEDBACK_PHRASES` vacíos: el código devuelve el
  valor crudo en vez de adivinar una traducción.
- En carrera no se usan objetivos con alerta sonora (decisión de producto).

## Lo mínimo que haría falta para cerrar el hallazgo de tests

No cobertura por cobertura: los tres invariantes que ya han fallado alguna vez
en este proyecto.

1. Un code o un refresh reutilizado se rechaza, incluso en carrera.
2. El error de login no varía según la cuenta exista o no.
3. Las tools resuelven el usuario desde el token, nunca desde un global.

Con `pytest` sobre cliente falso (basta el patrón que ya se usa para verificar
`advertencias` y la rotación de tokens) y un workflow que corra `mypy` y
`pytest` en cada push.
