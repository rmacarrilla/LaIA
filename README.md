# LaIA

Servidor MCP multiusuario en Python que expone las últimas actividades de Garmin
Connect como herramienta para Claude, usando la librería no oficial
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect). Cada
persona se conecta con su propia cuenta de Garmin mediante un flujo OAuth — no
hay contraseñas ni tokens que copiar y pegar a mano, ni una URL secreta que
compartir.

## Requisitos

- Python 3.10+
- Una cuenta de Garmin Connect
- Una base de datos Postgres (para el registro de usuarios y el estado OAuth)

## Instalación

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuración

Copia la plantilla de variables de entorno y rellénala:

```bash
cp .env.example .env
```

```
DATABASE_URL=postgres://usuario:contraseña@host:5432/basededatos
SESSION_ENCRYPTION_KEY=genera_un_valor_aleatorio
```

`.env` está en `.gitignore` y nunca se sube al repositorio.

`SESSION_ENCRYPTION_KEY` cifra en reposo la sesión de Garmin de cada usuario y
sirve de clave para el hash de su email (ver `crypto_utils.py`) — no se guarda
ningún dato personal en claro en Postgres. No la cambies una vez haya usuarios
reales: perderías la capacidad de descifrar sus sesiones guardadas y tendrían
que volver a conectarse.

## Uso

### Servidor MCP en local

```bash
claude mcp add laia -- "$(pwd)/venv/bin/python" "$(pwd)/mcp_server.py"
```

Registra el servidor en Claude Code (modo `stdio`, sin OAuth — pensado para
desarrollo con tu propia cuenta, no para servir a otras personas). Expone diez
herramientas, agrupadas por el ritmo al que cambia cada dato y por el tipo de
pregunta que resuelven (la selección de métodos de Garmin y los cálculos que
aplica cada una están especificados en
[`docs/LaIA-MCP-metodos-garmin.md`](docs/LaIA-MCP-metodos-garmin.md)):

Lectura:

- `estado(dias=7)`: cómo está el deportista hoy y esta semana — training
  readiness, body battery, sueño, FC en reposo, estado de entrenamiento y
  actividades del rango.
- `capacidad(extras=None)`: de qué es capaz ahora — umbrales, zonas, FTP,
  VO2max, predicciones de carrera. Bajo demanda: progresión de FTP, récords,
  hill score, edad de forma física, capacidades del dispositivo.
- `carga(inicio, fin)`: qué se ha entrenado en un rango — volumen, reparto de
  intensidad, RPE/sRPE y progresión semanal.
- `sesion(activity_id, detalle=False, potencia_por_zona=False)`: detalle de una
  sesión concreta, con splits (y largos con SWOLF en natación).
- `plan(inicio, fin, workout_id=None)`: qué hay agendado y con qué construirlo
  — calendario y biblioteca de plantillas juntos.

Escritura:

- `crear_entreno(sport, name, steps)`: sube una plantilla estructurada
  (correr/bici/nadar/fuerza) sin agendarla.
- `modificar_entreno(workout_id, sport, name, steps)`: reemplaza la estructura
  conservando el `workout_id`, así lo ya agendado no se rompe.
- `agendar(workout_id, date)` / `desagendar(...)`: pone y quita plantillas del
  calendario (`desagendar` acepta lista de ids o rango de fechas).
- `borrar_entreno(workout_ids=None, source=None)`: borra plantillas de la
  biblioteca de verdad, por ids o por origen ("prod_athletedata", "Shape"...).

### Servidor MCP remoto (HTTP + OAuth)

```bash
MCP_TRANSPORT=http python mcp_server.py
```

Arranca el mismo servidor escuchando en `PORT` (por defecto 8000), con
autenticación OAuth 2.1 completa (Dynamic Client Registration, authorization
code + PKCE, refresh tokens) usando el soporte nativo del SDK de MCP
(`mcp.server.auth`). No hay un IdP externo: el propio `/authorize` redirige a
`/login`, un formulario donde la persona introduce su email y contraseña de
Garmin — ese login **es** la autenticación, no hace falta ningún sistema de
usuarios/contraseñas propio.

Flujo para una persona nueva:

1. Añade el conector en Claude apuntando a `https://tu-servidor/mcp` (sin nada
   más en la URL).
2. Claude se autorregistra como cliente OAuth y redirige al navegador a
   `/authorize` → `/login`.
3. La persona mete su email y contraseña de Garmin. Si son válidas,
   `oauth_provider.py` da de alta (o actualiza) su usuario en Postgres, emite un
   authorization code y redirige de vuelta a Claude.
4. Claude canjea el code por un access token + refresh token. A partir de ahí,
   cada llamada a una tool usa la sesión de Garmin de esa persona — nunca la de
   otra — y el token se refresca solo cuando caduca, sin volver a pedir la
   contraseña.

### Base de datos

`db.py` crea/actualiza el esquema (`CREATE`/`ALTER TABLE IF NOT EXISTS`) la
primera vez que se conecta — no hay migraciones que ejecutar a mano. Dos tablas:

- `users`: una fila por cuenta de Garmin. `garmin_email_hash` (nunca el email en
  claro) identifica a la persona; `session_blob_encrypted` es su sesión de
  Garmin cifrada con `SESSION_ENCRYPTION_KEY`.
- `oauth_objects`: clientes OAuth registrados, authorization codes y access/
  refresh tokens — todo genérico (`kind`, `key`, `data` JSONB), porque son
  justo los modelos que ya define el SDK de MCP. Las filas caducadas se
  limpian solas (al leerlas, y además con una pasada periódica en segundo
  plano — ver `_cleanup_loop` en `mcp_server.py`).

### Seguridad

- **Consentimiento**: la pantalla de `/login` muestra qué aplicación está
  pidiendo acceso (`client_name` del cliente OAuth registrado) — sin esto,
  cualquiera podría registrar su propio cliente y enviar un enlace a nuestra
  pantalla de login real para phishear credenciales de Garmin.
- **Rate limiting**: `POST /login`, `POST /account/delete`, `POST /register`
  y `GET /authorize` están limitados por IP en memoria (`rate_limit.py`,
  por (método, ruta) — así ver un formulario no consume el mismo cupo que
  enviarlo) — suficiente para uso personal/small-scale, no sustituye un WAF
  si esto creciera de verdad.
- **PII y credenciales**: nunca en claro en Postgres (ver `crypto_utils.py`).
  Ni el email ni la contraseña de Garmin se registran jamás en logs. Los
  mensajes de error de login (`/login`, `/account/delete`) son siempre
  genéricos de cara al usuario, para no filtrar por qué falló exactamente
  (evita enumerar qué cuentas de Garmin existen).
- **Autorización de código de un solo uso**: `exchange_authorization_code`
  confirma que de verdad borró el code (no solo que lo leyó) antes de emitir
  tokens — dos canjes concurrentes del mismo code no pueden emitir dos pares
  de tokens.
- **Borrado de cuenta**: `GET/POST /account/delete` — reautentica con Garmin
  (igual que `/login`) y borra la fila de `users` y sus tokens de acceso/
  refresco. Autoservicio real, no un borrado manual en la base de datos.

## Despliegue en Railway

Un único servicio (`laia-mcp-server`) más un plugin de Postgres gestionado por
Railway (variable `DATABASE_URL`, referenciable por el servicio como
`${{Postgres.DATABASE_URL}}`). No hace falta ningún volumen: las sesiones viven
en la base de datos, no en el filesystem.

## Estructura del proyecto

```
.
├── garmin_client.py     # login/serialización de sesión de Garmin + caché corto
├── crypto_utils.py      # hash del email y cifrado de la sesión (PII en reposo)
├── rate_limit.py        # limitador en memoria para /login y /register
├── db.py                # capa mínima sobre Postgres (asyncpg)
├── oauth_provider.py    # servidor de autorización OAuth (login de Garmin como "IdP")
├── mcp_server.py        # servidor MCP + rutas OAuth + /login
├── requirements.txt     # dependencias con versiones fijadas
├── .env.example         # plantilla de variables de entorno
├── .env                 # credenciales reales (no versionado)
└── venv/                # entorno virtual (no versionado)
```

## Limitaciones conocidas

- El rate limiting es en memoria de un solo proceso: no protege de un ataque
  distribuido de verdad ni se comparte entre réplicas (hoy el servicio corre
  con una única réplica en Railway, así que protege de verdad).
- Sin rotación de `SESSION_ENCRYPTION_KEY` — aceptable mientras el número de
  usuarios reales sea pequeño; reconsiderar (reencriptado por lotes) a partir
  de unas 20-30 cuentas.
- Sin tests automáticos ni CI — cualquier regresión llega a producción sin
  red de seguridad automática.
- `/register` es público y sin caducidad por diseño (RFC 7591 / Dynamic
  Client Registration) — mitigado con rate limiting, pero cualquiera puede
  registrar un `client_name` engañoso; la pantalla de consentimiento avisa,
  pero no hay verificación de identidad de clientes.

## Licencia

Ver [LICENSE](LICENSE).
