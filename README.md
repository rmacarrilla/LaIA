# LaIA

Servidor MCP en Python que expone tus últimas actividades de Garmin Connect como herramienta para Claude, usando la librería no oficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) — en local (`stdio`) o desplegado como servicio remoto con URL pública (HTTP). Incluye una web de conexión (`connector_web.py`) para obtener esa URL sin tener que copiarla a mano.

## Requisitos

- Python 3.10+
- Una cuenta de Garmin Connect

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

Edita `.env`:

```
GARMIN_EMAIL=tu_correo@ejemplo.com
GARMIN_PASSWORD=tu_contraseña

# Solo necesaria si vas a exponer el servidor MCP por HTTP
MCP_AUTH_TOKEN=genera_un_valor_aleatorio

# Solo necesaria si vas a usar connector_web.py (clave distinta de MCP_AUTH_TOKEN)
INTERNAL_LOGIN_TOKEN=genera_otro_valor_aleatorio
```

`.env` está en `.gitignore` y nunca se sube al repositorio.

En el primer login exitoso, la librería guarda un token de sesión (por defecto en `~/.garminconnect`, configurable con `GARMIN_TOKENSTORE`), que se reutiliza en ejecuciones posteriores para evitar volver a autenticar con usuario/contraseña cada vez y reducir el riesgo de rate limiting (429) de Garmin.

## Uso

### Servidor MCP en local

```bash
claude mcp add laia -- "$(pwd)/venv/bin/python" "$(pwd)/mcp_server.py"
```

Registra el servidor en Claude Code (modo `stdio`). Expone dos herramientas que Claude puede invocar directamente en el chat:

- `list_activities`: últimas actividades (id, fecha y nombre).
- `get_activity_detail`: detalle de una actividad (duración, distancia, calorías, frecuencia cardíaca, velocidad media, desnivel), a partir del `activity_id` devuelto por `list_activities`.

### Servidor MCP remoto (HTTP)

```bash
MCP_TRANSPORT=http python mcp_server.py
```

Arranca el mismo servidor escuchando en `PORT` (por defecto 8000) en vez de por `stdio`. Pensado para desplegarse como servicio siempre activo (p. ej. en Railway) con una URL pública.

El acceso está protegido con una clave compartida (`MCP_AUTH_TOKEN`), que el cliente debe enviar de una de estas dos formas:

- Cabecera `Authorization: Bearer <token>` (clientes MCP estándar).
- Parámetro `?apiKey=<token>` en la URL (para enlaces de instalación de un clic, que no permiten configurar cabeceras).

Además expone `POST /internal/login`, protegido con una clave distinta
(`INTERNAL_LOGIN_TOKEN`), pensada únicamente para que la web de conexión
(`connector_web.py`) cambie qué cuenta de Garmin sirve el MCP (ver más abajo).

### Web de conexión (`connector_web.py`)

```bash
MCP_PUBLIC_URL=https://tu-mcp-server... INTERNAL_LOGIN_TOKEN=... MCP_AUTH_TOKEN=... python connector_web.py
```

Sirve un formulario donde cualquiera con acceso a la URL introduce un email y
contraseña de Garmin. Al enviarlo:

1. Llama a `POST /internal/login` en `mcp_server.py` con esas credenciales.
2. Si el login es válido, `mcp_server.py` sustituye la sesión cacheada (ver
   `GARMIN_TOKENSTORE`) por la de esa cuenta — a partir de ahí, `list_activities` y
   `get_activity_detail` sirven los datos de esa persona, sin redeploy.
3. La web devuelve la URL del conector (`{MCP_PUBLIC_URL}/mcp?apiKey={MCP_AUTH_TOKEN}`)
   lista para copiar y pegar en Claude, con instrucciones.

**Importante**: solo hay una cuenta activa a la vez, compartiendo la misma URL de
conector para todo el mundo — no es multiusuario real, es "quién inició sesión por
última vez". Tampoco hay ninguna clave que proteja el propio formulario: cualquiera
que conozca la URL de esta web y tenga una cuenta de Garmin válida (la suya propia)
puede cambiar qué cuenta sirve el MCP. Aceptado como limitación conocida mientras sea
un proyecto personal; a resolver cuando se diseñe un multiusuario real.

## Despliegue en Railway

El proyecto se despliega como dos servicios dentro del mismo proyecto de Railway,
ambos con dominio público:

- **`mcp_server.py`** (`MCP_TRANSPORT=http`): necesita un volumen persistente montado
  (p. ej. en `/data`) con `GARMIN_TOKENSTORE` apuntando a él — el filesystem de
  Railway es efímero, así que sin volumen cada ejecución reautenticaría con
  usuario/contraseña, aumentando el riesgo de bloqueo por rate limiting.
- **`connector_web.py`**: sin volumen (no cachea nada). Sus variables
  `MCP_AUTH_TOKEN` e `INTERNAL_LOGIN_TOKEN` se configuran como referencias a las del
  servicio anterior (`${{laia-mcp-server.MCP_AUTH_TOKEN}}`, etc.) para no duplicar
  los secretos.

Los dos servicios en Railway se llaman `laia-mcp-server` y `laia-connector-web`.

## Estructura del proyecto

```
.
├── garmin_client.py       # login a Garmin (usado por mcp_server.py)
├── mcp_server.py          # servidor MCP (local stdio / remoto HTTP)
├── connector_web.py       # web para obtener la URL del conector
├── requirements.txt       # dependencias con versiones fijadas
├── .env.example            # plantilla de variables de entorno
├── .env                    # credenciales reales (no versionado)
└── venv/                   # entorno virtual (no versionado)
```

## Licencia

Ver [LICENSE](LICENSE).
