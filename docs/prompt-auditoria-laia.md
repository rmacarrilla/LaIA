# Prompt de auditoría técnica — LaIA

Pensado para pegarlo en una sesión nueva de Claude (o Claude Code, con acceso
al repo) cada vez que subas una versión nueva de
https://github.com/rmacarrilla/LaIA. Antes de auditar, sustituye los
placeholders `{{...}}` si los usas.

---

## Prompt

Eres un auditor senior de seguridad, calidad de código y protección de datos.
Vas a auditar la versión actual del repositorio
`https://github.com/rmacarrilla/LaIA`, un servidor MCP en Python que da
acceso a datos de Garmin Connect de varios usuarios mediante OAuth 2.1, con
Postgres como almacén de sesiones y tokens.

No asumas nada de memoria sobre versiones anteriores: lee el contenido real
de estos archivos tal como están ahora — `mcp_server.py`, `oauth_provider.py`,
`garmin_client.py`, `crypto_utils.py`, `rate_limit.py`, `db.py`,
`requirements.txt`, `.env.example` y `README.md` — y cualquier archivo nuevo
que no estuviera en la última auditoría.

{{Si tienes el informe de la auditoría anterior, pégalo aquí. Si lo pegas,
divide los hallazgos en "ya conocidos y sin resolver", "resueltos desde
entonces" y "nuevos en esta versión". Si no lo pegas, trata todo como
primera auditoría.}}

Revisa cada bloque siguiente. Donde el repo ya documenta una limitación
conocida (README, sección "Limitaciones conocidas"), compruébala en el
código real en vez de darla por buena, y di si sigue igual, ha empeorado o
se ha corregido.

### 1. Autenticación y flujo OAuth

- PKCE: confirma que se exige y se valida en cada intercambio de código, no
  solo que se acepte si el cliente lo manda.
- `redirect_uri`: validación exacta contra la registrada, sin coincidencias
  parciales ni wildcards no intencionados.
- Expiración y de un solo uso de authorization codes, access tokens y
  refresh tokens; revisa qué pasa si se reutiliza un code ya canjeado.
- `/register` está descrito en el README como "abierta por diseño y sin
  caducidad". Evalúa el riesgo real de eso hoy: ¿puede alguien registrar
  clientes OAuth ilimitados y usarlos para abusar del rate limiting o para
  montar una pantalla de phishing con un `client_name` engañoso? ¿La
  pantalla de consentimiento deja claro contra qué está autenticándose la
  persona?
- El punto más sensible de todo el sistema: `/login` pide el email y la
  contraseña reales de Garmin de la persona, que `oauth_provider.py` reenvía
  para autenticar contra Garmin. Verifica explícitamente, línea por línea:
  que la contraseña no se registra en ningún log, métrica, traza de error o
  mensaje de excepción; que no se guarda en ninguna variable de vida larga
  ni en la base de datos; que el tráfico va cifrado de extremo a extremo;
  y que un fallo de login no revela por el mensaje de error si el email
  existe o no (evita enumeración de cuentas).

### 2. Secretos y cifrado

- `SESSION_ENCRYPTION_KEY`: qué algoritmo usa `crypto_utils.py` para cifrar
  la sesión (debe ser una construcción probada tipo AES-GCM o Fernet, no
  algo casero), si reutiliza IV/nonce entre usuarios o entre cifrados
  sucesivos, y qué pasa exactamente si la clave se filtra.
- El README avisa de que cambiar la clave rompe todas las sesiones
  guardadas. Valora si merece la pena un esquema de rotación (aunque sea
  manual, con reencriptado por lotes) antes de que el número de usuarios
  reales lo haga costoso.
- Repasa `.env.example` contra las variables que el código realmente lee:
  que no falte ninguna ni sobre ninguna, y que ningún valor de ejemplo sea
  un secreto real olvidado.

### 3. Datos personales y RGPD

- `garmin_email_hash`: qué función hash usa. Un hash simple de un email sin
  sal secreta (pepper) es vulnerable a diccionario, porque el espacio de
  emails plausibles es pequeño comparado con una contraseña. Confirma si
  hay pepper y dónde vive.
- Los datos de entrenamiento (frecuencia cardíaca, actividad, calorías) son
  datos de salud, categoría especial bajo el RGPD (art. 9). Revisa si el
  tratamiento actual (qué se guarda, cuánto tiempo, con qué base legal)
  sería defendible si alguien lo preguntara.
- El README ya reconoce que falta borrado de cuenta autoservicio (derecho
  al olvido, art. 17). Compruébalo en cada versión: ¿sigue siendo manual?
  ¿existe ya un endpoint o comando para borrar `users` y todo lo que cuelga
  de esa persona en `oauth_objects`?
- Política de retención: ¿hay algo que borre sesiones o tokens caducados
  más allá de la limpieza puntual que ya existe (`_cleanup_loop`), o se
  acumulan indefinidamente filas muertas con datos personales?

### 4. Rate limiting y abuso

- Confirma si sigue siendo en memoria y de un solo proceso, como dice el
  README. Si el despliegue en Railway ha pasado a más de una réplica, ese
  limitador ya no protege nada de verdad — es el primer punto a revisar si
  cambia la infraestructura.
- Cobertura: ¿sigue limitado solo `/login` y `/register`, o hay ya otras
  rutas (nuevas tools, endpoints de administración) que necesitarían lo
  mismo?

### 5. Aislamiton entre usuarios

- En cada tool del MCP (`list_activities`, `get_activity_detail` y
  cualquier tool nueva), confirma que el alcance de datos se obtiene
  siempre de la sesión autenticada de la petición en curso, nunca de una
  variable global, caché compartida o estado de proceso que pueda
  mezclarse entre peticiones concurrentes de dos personas distintas.

### 6. Calidad y mantenibilidad del código

- Duplicación real entre archivos, funciones que hacen más de una cosa,
  nombres inconsistentes entre `garmin_client.py` y el resto.
- Cobertura de type hints y si mypy (o similar) pasaría sin errores.
- Uso de `async`/`await`: busca llamadas bloqueantes dentro de rutas
  async (especialmente en `garmin_client.py`, si la librería
  `garminconnect` no es async nativa), y conexiones a Postgres que no se
  liberen correctamente del pool de `asyncpg`.
- Tests: si no hay carpeta de tests, dilo como hallazgo, no lo des por
  hecho como algo normal en un proyecto con datos de salud y credenciales
  de por medio.
- CI: si no hay GitHub Actions ni equivalente corriendo lint/tests en cada
  push, señálalo — hoy cualquier regresión llega a producción sin red.

### 7. Dependencias

- Revisa `requirements.txt`: versiones fijadas o rangos abiertos, y si
  alguna tiene CVEs conocidas (usa `pip-audit` o equivalente si puedes
  ejecutarlo).
- `garminconnect` es una librería no oficial que depende de que Garmin no
  cambie su API interna. Anota si hay alguna señal de que esté
  desactualizada o sin mantenimiento reciente.

### 8. Despliegue

- Railway: variables de entorno de producción, si el servicio fuerza HTTPS
  de extremo a extremo, si las cookies de sesión (si las hay) llevan
  `Secure` y `HttpOnly`.
- Qué pasa si el proceso se reinicia a mitad de un flujo OAuth en curso
  (¿el estado vive solo en Postgres, o hay algo en memoria que se perdería?).

## Formato del informe

Pide como salida:

1. Resumen ejecutivo de 4-6 líneas: qué ha cambiado desde la última
   versión y si hay algo que bloquearía un despliegue a más usuarios.
2. Tabla de hallazgos con columnas: severidad (crítico / alto / medio /
   bajo), archivo y línea aproximada, descripción, y si es nuevo o ya
   conocido.
3. Para cada hallazgo crítico o alto, un parche concreto (diff o fragmento
   de código), no solo la descripción del problema.
4. Lista actualizada de "limitaciones conocidas aceptadas" — las que no se
   van a arreglar ahora a propósito — para no volver a marcarlas como
   nuevas en la siguiente auditoría.
