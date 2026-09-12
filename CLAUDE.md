# LaIA

Servidor MCP multiusuario en Python que expone las actividades de Garmin Connect
a Claude. Cada persona se conecta con su propia cuenta vía OAuth; no hay cuenta
"por defecto" ni token compartido.

## Comandos

```bash
source venv/bin/activate         # activar entorno virtual
python mcp_server.py             # servidor MCP en local (stdio, sin OAuth)
MCP_TRANSPORT=http python mcp_server.py  # servidor MCP remoto (HTTP + OAuth)
pip install -r requirements.txt  # instalar/actualizar dependencias
```

## Arquitectura

- **`garmin_client.py`**: sin estado salvo un caché corto en memoria.
  `login(email, password)` hace un login real (nunca con tokenstore, para no
  reutilizar por accidente la sesión de otra cuenta) y devuelve
  `client.client.dumps()` — la sesión serializada a JSON, en memoria, sin
  tocar disco (ojo: `dumps()`/`loads()` viven en `Garmin.client`, no en el
  propio objeto `Garmin`). Lanza `RuntimeError` en fallo, nunca `sys.exit()`
  — esta función corre dentro de un servidor de larga duración; `sys.exit()`
  lanza `SystemExit`, que ni el framework MCP ni el manejo de excepciones de
  Starlette capturan como una `Exception` normal, así que tumbaría el proceso
  entero por un solo login fallido (nos pasó de verdad en una versión
  anterior de este proyecto, single usuario). `client_from_session(blob)`
  reconstruye un cliente autenticado a partir de ese blob — esto también hace
  una llamada de red (Garmin recarga el perfil), así que
  `client_from_session_cached(user_id, blob)` guarda el resultado 5 minutos
  por `(user_id, blob)`: si cambia el blob (nuevo login), la clave cambia y el
  caché se invalida solo, sin coordinar un `invalidate()` entre módulos.
- **`crypto_utils.py`**: `SESSION_ENCRYPTION_KEY` cifra (Fernet) el
  `session_blob` y sirve de clave de un HMAC-SHA256 para el email — nunca se
  guarda el email en claro, solo su hash con clave (no se necesita para nada
  más que comprobar "¿ya existe este usuario?", así que no hace falta poder
  recuperarlo). Es la única pieza de PII/credenciales en Postgres, y por eso
  es la única que se cifra/hashea.
- **`rate_limit.py`**: `RateLimiter` (ventana deslizante en memoria, con
  desalojo LRU acotado) y `RateLimitMiddleware`, que lo aplica por ruta a las
  peticiones POST. Usado en `/login` (fuerza bruta de Garmin) y `/register`
  (alta de clientes OAuth sin autenticación previa por diseño — RFC 7591 — y
  sin caducidad).
- **`db.py`**: pool de `asyncpg` sobre `DATABASE_URL` (`min_size=1,
  max_size=5`: esta app no necesita el pool de 10 conexiones por defecto).
  Tabla `users` (`garmin_email_hash`, `session_blob_encrypted` — nunca en
  claro) y tabla genérica `oauth_objects` (`kind`, `key`, `data` JSONB,
  `expires_at`) para todo lo demás — clientes OAuth registrados, authorization
  codes, access/refresh tokens. Son ya modelos Pydantic del propio SDK de MCP
  (`OAuthClientInformationFull`, `AuthorizationCode`, `AccessToken`,
  `RefreshToken`), así que guardarlos como JSON evita diseñar un esquema
  propio para cada uno. `purge_expired_objects()` borra en bloque lo caducado
  — sin esto, un `pending_authorize`/`code` que nadie completa se queda para
  siempre (la limpieza de `load_object` solo se dispara al leer esa fila
  concreta). `_migrate_legacy_rows()` es la migración, idempotente y de un
  solo uso, desde el esquema anterior (email/sesión en claro) al actual.
- **`oauth_provider.py`** (`GarminOAuthProvider`): implementa
  `mcp.server.auth.provider.OAuthAuthorizationServerProvider`. El SDK ya trae
  hechas las rutas `/authorize`, `/token`, `/register`, `/revoke` y los
  `.well-known/...` (`mcp/server/auth/routes.py`) — este fichero es la única
  pieza que hay que escribir. `authorize()` no redirige a un IdP de terceros
  (no existe: Garmin no tiene OAuth público) — redirige a nuestra propia
  `/login` (en `mcp_server.py`), guardando los parámetros originales de la
  petición (`AuthorizationParams`) bajo `kind='pending_authorize'` mientras
  tanto. `complete_login()` es lo que `/login` llama al recibir el formulario:
  hace el login de Garmin, da de alta/actualiza el usuario (el email de Garmin
  es la identidad duradera — volver a loguearse con el mismo email siempre da
  el mismo `user_id`, y por tanto el mismo `subject` en los tokens), emite el
  `AuthorizationCode` y devuelve la URL de vuelta a Claude
  (`construct_redirect_uri`, ya provisto por el SDK). El resto de métodos
  (`exchange_authorization_code`, `exchange_refresh_token`, `load_access_token`,
  `revoke_token`) son operaciones CRUD directas sobre `db.py`. El SDK valida
  PKCE, expiración y que el `redirect_uri` no cambie entre `/authorize` y
  `/token` — no hay que reimplementar nada de eso.
- **`mcp_server.py`**: construye `MCPServer("laia", auth_server_provider=...,
  auth=AuthSettings(...))` — con eso el SDK monta solo todas las rutas OAuth.
  Las tools (`list_activities`, `get_activity_detail`) son `async def` (antes no
  lo eran) porque necesitan `await` para leer la sesión de Postgres. Esto tiene
  una trampa: el framework MCP solo despacha a un hilo las tools **síncronas**
  (`anyio.to_thread.run_sync`, confirmado leyendo
  `mcp/server/mcpserver/utilities/func_metadata.py`); una tool `async def` se
  ejecuta directamente en el event loop, así que las llamadas bloqueantes
  dentro (`client.get_activities(...)`, reconstruir el cliente desde el blob)
  hay que envolverlas explícitamente en `asyncio.to_thread` — si no, se
  reintroduce el mismo bug de bloquear el servidor entero que ya se corrigió una
  vez en la versión anterior (monousuario) de `internal_login`. La ruta
  `/login` (GET muestra el formulario, POST lo procesa) es pública a propósito
  — es la puerta de entrada, no puede exigir un token que aún no existe.
- **Identidad de quien llama**: dentro de una tool,
  `mcp.server.auth.middleware.auth_context.get_access_token()` devuelve el
  `AccessToken` validado de la petición en curso; `.subject` es el `user_id` de
  Postgres. No hace falta cambiar la firma de las tools para acceder a esto —
  es un contextvar que rellena el propio middleware del SDK.
- **Pantalla de consentimiento**: `/login` muestra el `client_name` (o
  `client_id` si no hay nombre) del cliente OAuth que pide acceso
  (`_client_label_for_flow` en `mcp_server.py`). Sin esto, cualquiera podría
  registrar su propio cliente OAuth (DCR es público, sin autenticación) y
  mandar un enlace a nuestra pantalla de login *real* para hacer phishing de
  credenciales de Garmin — la víctima no tendría forma de notar que está
  autorizando a una aplicación desconocida.
- **XSS**: todo lo que se interpola en el HTML de `/login` (`flow_id`,
  `client_label`, `error`) pasa por `html.escape()`. `flow_id` en particular
  puede llegar de un POST directo con cualquier contenido (no solo del
  formulario que servimos nosotros) — sin escapar, un `flow` tipo `"><script>`
  se reflejaría tal cual en la respuesta.
- **Arranque**: `_connect_db_with_retry` reintenta con backoff exponencial si
  Postgres no responde a la primera (p.ej. una carrera de arranque con el
  propio plugin de Postgres en Railway), en vez de morir directamente.

## Historia relevante

- Versión anterior (single-usuario): una única cuenta "activa" compartida,
  cambiada vía `POST /internal/login` protegido con un token fijo, servida por
  dos servicios Railway (`laia-mcp-server` + `laia-connector-web`) y un volumen
  para cachear la sesión en disco. Se retiró por completo al pasar a OAuth
  multiusuario: `connector_web.py` y `shared_config.py` desaparecieron (su HTML
  se reaprovechó en la ruta `/login`), y con ellos el volumen y las variables
  `GARMIN_EMAIL`/`GARMIN_PASSWORD`/`MCP_AUTH_TOKEN`/`INTERNAL_LOGIN_TOKEN` (ya
  no hay cuenta por defecto ni token compartido).
- Despliegue en Railway: un único servicio (`laia-mcp-server`) más un plugin de
  Postgres gestionado (`DATABASE_URL`). Proyecto de Railway: `garmin-activities`.
  Repo en GitHub: `rmacarrilla/LaIA` (renombrado desde `garmin_mcp_server`, y
  antes `garmin-activities`). Cada rename de repo rompe la conexión
  GitHub↔Railway para auto-deploy en push — hay que reconectar el source de
  cada servicio (`connect-service-source`) tras renombrar, y además actualizar
  manualmente en GitHub el acceso de la GitHub App de Railway al repo con su
  nombre nuevo si no aparece en el selector.
