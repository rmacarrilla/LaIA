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
```

`.env` está en `.gitignore` y nunca se sube al repositorio.

## Uso

### Servidor MCP en local

```bash
claude mcp add laia -- "$(pwd)/venv/bin/python" "$(pwd)/mcp_server.py"
```

Registra el servidor en Claude Code (modo `stdio`, sin OAuth — pensado para
desarrollo con tu propia cuenta, no para servir a otras personas). Expone dos
herramientas:

- `list_activities`: últimas actividades (id, fecha y nombre) de quien está
  autenticado.
- `get_activity_detail`: detalle de una actividad (duración, distancia,
  calorías, frecuencia cardíaca, velocidad media, desnivel), a partir del
  `activity_id` devuelto por `list_activities`.

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

`db.py` crea el esquema (`CREATE TABLE IF NOT EXISTS`) la primera vez que se
conecta — no hay migraciones que ejecutar a mano. Dos tablas:

- `users`: una fila por cuenta de Garmin (email + sesión serializada).
- `oauth_objects`: clientes OAuth registrados, authorization codes y access/
  refresh tokens — todo genérico (`kind`, `key`, `data` JSONB), porque son
  justo los modelos que ya define el SDK de MCP.

## Despliegue en Railway

Un único servicio (`laia-mcp-server`) más un plugin de Postgres gestionado por
Railway (variable `DATABASE_URL`, referenciable por el servicio como
`${{Postgres.DATABASE_URL}}`). No hace falta ningún volumen: las sesiones viven
en la base de datos, no en el filesystem.

## Estructura del proyecto

```
.
├── garmin_client.py     # login/serialización de sesión de Garmin (sin estado)
├── db.py                # capa mínima sobre Postgres (asyncpg)
├── oauth_provider.py    # servidor de autorización OAuth (login de Garmin como "IdP")
├── mcp_server.py        # servidor MCP + rutas OAuth + /login
├── requirements.txt     # dependencias con versiones fijadas
├── .env.example         # plantilla de variables de entorno
├── .env                 # credenciales reales (no versionado)
└── venv/                # entorno virtual (no versionado)
```

## Licencia

Ver [LICENSE](LICENSE).
