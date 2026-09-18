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
  Las tools son `async def` porque necesitan `await` para leer la sesión de
  Postgres. Esto tiene una trampa: el framework MCP solo despacha a un hilo
  las tools **síncronas** (`anyio.to_thread.run_sync`, confirmado leyendo
  `mcp/server/mcpserver/utilities/func_metadata.py`); una tool `async def` se
  ejecuta directamente en el event loop, así que las llamadas bloqueantes
  dentro (métodos de `garminconnect`, reconstruir el cliente desde el blob)
  hay que envolverlas explícitamente en `asyncio.to_thread` — si no, se
  reintroduce el mismo bug de bloquear el servidor entero que ya se corrigió una
  vez en la versión anterior (monousuario) de `internal_login`. La ruta
  `/login` (GET muestra el formulario, POST lo procesa) es pública a propósito
  — es la puerta de entrada, no puede exigir un token que aún no existe.
- **Tools organizadas por workflow de coaching, no por método de
  `garminconnect`**: la librería expone ~150 métodos; envolver cada uno 1:1
  habría hecho perder de vista para qué sirve cada llamada y habría disparado
  el consumo de tokens si alguna tool devolviera una respuesta punto a punto
  (FC/potencia por segundo, sueño minuto a minuto, body battery intradía).
  La selección y agrupación exactas — qué 31 de los ~150 métodos se usan, por
  qué, y qué cálculos aplicar antes de devolver los datos — están
  especificadas y verificadas campo a campo contra una cuenta real en
  `docs/LaIA-MCP-metodos-garmin.md`; este apartado resume cómo se llevó esa
  especificación al código, no la repite entera.

  **Cambio de convención**: a diferencia de una versión anterior de este
  proyecto, aquí no se usa `client.typed` (el namespace Pydantic de la propia
  librería) — la especificación se verificó contra los dicts crudos de
  `connectapi`, con las claves exactas documentadas en su sección 2, así que
  el código adopta ese mismo estilo en vez de mezclar dos formas de leer la
  misma API.

  10 tools — 5 de lectura, 5 de escritura:
  - **Lectura** (`training_data.py`): `estado(dias=7)` (fisiología + carga
    reciente del día, se refresca siempre), `capacidad(extras=None)` (FTP,
    zonas, VO2max... cambia en semanas o meses, no se repite dentro de una
    misma conversación — `extras` trae bajo demanda progresión de FTP,
    récords, hill score, edad de forma física o las capacidades del
    dispositivo, `get_devices`, que no tenía hueco natural en ninguna otra
    tool), `carga(inicio, fin)` (volumen/intensidad de un rango, para
    revisión de bloque), `sesion(activity_id, detalle=False,
    potencia_por_zona=False)` (detalle de una sesión conocida — nunca busca
    actividades, `detalle`/`potencia_por_zona` solo bajo demanda) y
    `plan(inicio, fin, workout_id=None)` (calendario + biblioteca de
    plantillas juntos, siempre antes de cualquier escritura).
  - **Escritura** (`workout_builder.py`): `crear_entreno` (sube una plantilla
    estructurada — nadar/bici/correr/fuerza — a partir de un esquema JSON
    genérico de pasos, usando los modelos Pydantic tipados de
    `garminconnect.workout`; el spec solo lista running/cycling/swimming pero
    se mantiene también `strength`, que ya existía), `modificar_entreno`
    (reconstruye la plantilla con `build_workout` —la misma función que usa
    `crear_entreno`— y la manda con `client.update_workout`, que fuerza el
    `workoutId` del cuerpo a coincidir con el de la URL: lo ya agendado no se
    rompe), `agendar`, `desagendar` y `borrar_entreno`. Importante:
    **desagendar no es borrar** — `unschedule_workout` (lo que usa
    `desagendar` por debajo) solo quita la entrada del calendario; la
    plantilla sigue en la librería de Garmin. `borrar_entreno` es la única
    que la borra de verdad (`client.delete_workout`).
    `desagendar` y `borrar_entreno` aceptan cada una dos modos mutuamente
    excluyentes, validados en la propia tool (no expresables en el JSON
    Schema) — decisión explícita de esta actualización, más allá de lo que
    dice el spec literalmente, para no repetir el problema real de 48
    plantillas huérfanas que motivó la fase anterior:
    - `desagendar`: por `scheduled_workout_ids` explícitos
      (`workout_builder.unschedule_workouts_by_id`) o por rango
      `start_date`/`end_date` con `exclude_ids` opcional
      (`workout_builder.unschedule_workouts_in_range`, que encuentra
      candidatos con `training_data.find_scheduled_in_range` — filtra
      `calendarItems` por `itemType == "workout"`, ya que ese endpoint de
      Garmin también devuelve `"nap"`, `"activity"`, etc.).
    - `borrar_entreno`: por `workout_ids` explícitos
      (`workout_builder.delete_workouts`) o por `source` — el origen de la
      plantilla ("prod_athletedata", "Shape"...) resuelto por
      `training_data.list_workout_templates` (`workout_builder.
      delete_workouts_by_source`), para limpiar de golpe lo que deja una
      integración de terceros sin conocer cada `workout_id`.
  - **Cálculos aplicados antes de devolver los datos** (sección 4 del spec),
    todos en `training_data.py`: `_rpe_feel` (RPE = `directWorkoutRpe / 10`,
    nunca `0` si falta; feel traducido; sRPE = RPE × minutos — usado por
    `estado`, `carga` y `sesion`), `_soft_time_seconds` (tiempo suave real:
    duración − suma de las 5 zonas de FC + zona1 + zona2, porque lo que cae
    por debajo del suelo de zona 1 no se registra en ninguna), edad (dentro
    de `capacidad`, desde `birthDate`), velocidad de umbral en min/km (dentro
    de `capacidad`, desde el `speed` en m/s de `get_lactate_threshold`),
    normalización del id de instancia agendada a `scheduled_workout_id`
    (`find_scheduled_in_range`, mismo dato que Garmin llama
    `workoutScheduleId` en la respuesta de `agendar`/`schedule_workout` y
    `"id"` dentro de cada `calendarItem`). Dos traducciones (`typeId` de
    récords personales, códigos de `feedbackPhrase`) usan un diccionario
    deliberadamente incompleto (`_RECORD_TYPE_LABELS`, `_FEEDBACK_PHRASES`,
    vacíos de partida) — el spec documenta el mecanismo pero no enumera cada
    código de Garmin; un código no listado se devuelve tal cual, nunca hace
    fallar una lectura, y se completa según se vaya viendo en uso real.
  - **Aplanar lo que Garmin entierra, y no devolver series punto a punto**:
    las dos cosas salieron del mismo feedback real — un modelo usando el
    conector dijo que le faltaban el estado de entrenamiento y el histórico de
    SpO2 nocturno, y ninguno de los dos faltaba. Estaban en respuestas que ya
    se pedían, pero ilegibles: la fase de entrenamiento cuelga de una clave
    que es el **id del reloj** (`latestTrainingStatusData["3461696276"]`) y
    llega como código numérico (`trainingStatus: 4`), con el nombre legible
    solo en `trainingStatusFeedbackPhrase` (`"MAINTAINING_2"`); el SpO2 por
    noche vive dentro del `values` de cada fila de `get_sleep_daily`. Y sobre
    todo, estaban ahogados: `estado(7)` pesaba 241 KB, de los que 181 KB eran
    arrays minuto a minuto del sueño, justo lo que la regla de la cabecera de
    `training_data.py` prohíbe y que se colaba al pasar las respuestas tal
    cual. Ahora:
    - `_del_dispositivo_principal` desanida lo que cuelga de un id de reloj
      (el marcado `primaryTrainingDevice`, o el más reciente si hay varios) y
      `_etiqueta_de_frase` saca la etiqueta del prefijo de la frase de
      feedback. **No hay tabla propia de códigos numéricos de
      `trainingStatus`**: solo se ha observado un valor (4 ↔ MAINTAINING) y
      adivinar el resto etiquetaría mal la fase, que es el dato que más pesa;
      el número se devuelve igualmente en `fase_codigo`.
    - `estado()` expone `entrenamiento` (fase, reparto de carga del mes frente
      a objetivo con su `feedback`, VO2max), `noches` (una fila por noche con
      sueño, SpO2 medio, HRV, FC en reposo y temperatura de piel — la serie
      que distingue un dato malo puntual de un patrón, a coste cero porque
      `get_sleep_daily` ya se pedía) y `sueno_anoche`. Con
      `spo2_detalle=True` añade el mínimo y máximo por noche vía
      `get_spo2_data`, que es una llamada por día y por eso es opcional.
    - `capacidad()` expone `perfil` (edad, sexo, **peso en kg** —
      `userData.weight` viene en gramos, 67000 → 67, misma trampa de unidades
      que el `speed` del umbral—, altura, VO2max y umbrales).
    - `_sin_claves` hace cumplir la regla del módulo: fuera las series por
      época del sueño, el array intradía de body battery, y en las actividades
      el `metadataDTO` (metadatos de subida del fichero) y `splitSummaries`
      (el mismo agregado por tramo que el spec ya descarta; para una sesión
      concreta está `get_activity_splits` en `sesion()`). Resultado medido:
      `estado(7)` 241 → 28 KB, `carga(10 días)` 121 → 57 KB, `sesion()` 28 →
      21 KB. Ojo al tocar esa lista: `activityType`/`activityTypeDTO` son el
      mismo dato con nombre distinto según venga de la lista o del detalle, y
      sin ellos una actividad se queda sin deporte (y `sesion()` deja de
      detectar natación y bici).
  - **Fallos parciales (`advertencias`)**: cada tool de lectura agrupa varias
    llamadas a Garmin, y que una falle es lo normal, no lo excepcional — basta
    con que el reloj no se haya sincronizado hoy. `_reunir` lanza el grupo con
    `return_exceptions=True` y devuelve lo que sí llegó más una lista
    `advertencias` con qué faltó y por qué; solo si fallan **todas** se
    relanza la excepción, porque entonces no hay respuesta que dar. Las
    excepciones que no son `Exception` (cancelación, apagado) se relanzan
    siempre: tragárselas rompería el cierre del servidor.
    Hay un segundo caso, menos obvio y más frecuente: la llamada **funciona
    pero no trae nada**. `get_morning_training_readiness` responde `None` en
    un día sin sincronizar, y `get_training_status` puede devolver el sobre
    entero con `latestTrainingStatusData` a `None`. Sin avisar, quien lea la
    respuesta ve un `null` con `advertencias: []` y no sabe si es un fallo o
    es que aún no hay dato — así que ambos casos generan también advertencia.
  - **Trampas de unidades y escalas, todas verificadas contra la cuenta real**
    (auditoría acotada a unidades, a raíz de que dos de los tres bugs de la
    fase anterior fueran de este tipo). Importan porque no fallan: devuelven
    un número plausible pero equivocado, y un modelo usándolo no tiene forma
    de detectarlo — a diferencia de un dato que falta, que sí reporta.
    - `directWorkoutRpe` viene **×10** (30 = RPE 3, escala 0-10 del reloj).
    - `userData.weight` viene en **gramos** (67000 = 67 kg).
    - `speed` de `get_lactate_threshold` y `lactateThresholdSpeed` vienen en
      **m/s ÷ 10** (0.369 = 3,69 m/s ≈ 4:31 min/km, no 45 min/km). Misma
      codificación que usa Garmin en los targets de ritmo de un entreno
      (0.274 ↔ 6:05/km), que es con lo que se confirmó.
    - `averageSpeed` se calcula sobre **`movingDuration`**, no sobre
      `duration`. En carrera y bici coinciden; en natación no, porque los
      descansos entre series no cuentan como movimiento: 1300 m en 2234 s de
      reloj con 1225 s nadando son 2:51/100m o 1:34/100m según cuál uses.
      Por eso `_ritmos` devuelve los dos con nombre explícito
      (`seg_por_100m_total` y `seg_por_100m_en_movimiento`) en vez de dejar
      elegir a ciegas.
    - `sleepNeed` viene en **minutos** (540 = 9h) mientras que los tiempos por
      fase de la misma respuesta (`deepTime`, `lightTime`...) vienen en
      **segundos** — de ahí el sufijo en `sueno_necesario_min`.
    - `sueno_total_s` **excluye** el tiempo despierto: deep + light + rem
      cuadra exacto con el total, sumarle `despierto_s` no.
    - `race_predictions` son **segundos** pelados (`time5K: 1302`); se añade
      `race_predictions_ritmo` con el min/km que implica cada una.
    - `beginTimestamp` es epoch en **milisegundos**.
    Comprobadas y correctas, sin necesidad de conversión: distancias en
    metros, duraciones y `hrTimeInZone_*` en segundos (la suma de zonas nunca
    supera la duración), zonas de FC en bpm y de potencia en vatios,
    `weekly_stress` y body battery en 0-100, training effect en 0-5.
  - **Las descripciones de las tools son el protocolo de encadenado**: la
    sección 5 del spec ("qué llamar en qué orden para cada tipo de
    conversación") no se implementa como caché ni máquina de estados en el
    servidor — vive en el docstring de cada tool, para que un modelo que las
    use por primera vez entienda solo leyéndolas cómo combinarlas (p.ej. el
    docstring de `capacidad` dice explícitamente que no se repite dentro de
    la misma conversación; el de `sesion` dice que nunca se llama sin un
    `activity_id` ya obtenido antes).
  - **Pendiente**: `crear_entreno`/`modificar_entreno` construyen pasos por
    tiempo/distancia sin target de zona (FC/ritmo/potencia) — la librería no
    trae helper para eso y Garmin no documenta el shape exacto del dict de
    target con zona (API no oficial). Antes de añadirlo hay que verificarlo
    contra un entrenamiento real: crearlo a mano en la app de Garmin y leer
    su JSON con `plan(workout_id=...)`.
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
- **Canje de código atómico**: `db.delete_object` devuelve si de verdad borró
  algo. `exchange_authorization_code` lo comprueba y lanza `TokenError` si no
  — sin esto, dos canjes concurrentes del mismo `authorization_code` (el
  `load_authorization_code` del SDK y nuestro `delete_object` son dos
  llamadas async separadas, no una transacción) podrían colarse ambos antes
  de que ninguno viera el borrado del otro, emitiendo dos pares de tokens
  desde un code que se supone de un solo uso.
- **Mensajes de login genéricos**: `garminconnect` tiene mensajes de error
  distintos según el motivo exacto del fallo (credenciales, cuenta
  inexistente, MFA...). `complete_login`/`delete_account` nunca relanzan
  `str(err)` tal cual al usuario — siempre un mensaje fijo — porque hacerlo
  permitiría enumerar qué cuentas de Garmin existen de verdad.
  `FlowExpiredError` es la excepción aparte para "el flow_id no existe",
  que sí es información segura de mostrar (no depende de ninguna cuenta).
- **`/account/delete`** (`oauth_provider.py`: `delete_account`): autoservicio
  de borrado (GDPR art. 17). Reautentica con Garmin en vez de exigir un
  access token — así solo quien sabe la contraseña puede borrar esa cuenta, y
  no depende de si `get_access_token()` propaga la identidad a rutas fuera de
  `/mcp` (no lo comprobamos, así que no construimos la ruta sobre esa
  suposición). `db.delete_user` borra la fila de `users` y, filtrando por
  `data->>'subject'` en el JSONB, sus tokens de `oauth_objects` — esas dos
  tablas no comparten una clave foránea real, el `subject` vive dentro del
  JSON, no en una columna propia.
- **Rate limiting por (método, ruta)**, no solo por ruta: `RateLimitMiddleware`
  distingue `GET /login` (ver el formulario) de `POST /login` (intentar
  autenticar), para que lo primero no consuma el cupo de lo segundo.
  `GET /authorize` también está limitado — con cualquier `client_id` válido
  (trivial de conseguir, `/register` es público) se podían generar
  `pending_authorize` sin límite si solo se cubría `/register`.

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
