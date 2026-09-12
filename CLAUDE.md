# Garmin MCP Server

Servidor MCP en Python que consulta las últimas actividades de una cuenta de Garmin Connect vía la librería no oficial `garminconnect`, para uso personal, y las expone a Claude (local o remoto). Incluye una web (`connector_web.py`) para cambiar qué cuenta de Garmin sirve el MCP y obtener la URL del conector.

## Comandos

```bash
source venv/bin/activate         # activar entorno virtual
python mcp_server.py             # servidor MCP en local (stdio)
MCP_TRANSPORT=http python mcp_server.py  # servidor MCP remoto (HTTP)
python connector_web.py          # web de conexión (necesita MCP_PUBLIC_URL, MCP_AUTH_TOKEN, INTERNAL_LOGIN_TOKEN)
pip install -r requirements.txt  # instalar/actualizar dependencias
```

## Convenciones del proyecto

- Las credenciales viven en `.env` (nunca en el código ni en commits). `.env.example` documenta las claves esperadas.
- `requirements.txt` fija versiones exactas (`==`), no rangos, para reproducibilidad.
- El login a Garmin está centralizado en `garmin_client.py` (`get_client()`), reutilizado por `mcp_server.py`.
- El login usa caché de tokens (por defecto en `~/.garminconnect`, configurable con `GARMIN_TOKENSTORE`) para evitar reautenticar con usuario/contraseña en cada ejecución y reducir rate limiting (errores 429) de Garmin. En Railway esto requiere un volumen persistente, ya que el filesystem es efímero.
- Errores de login/conexión se capturan explícitamente (`GarminConnectAuthenticationError`, `GarminConnectConnectionError`) y terminan el programa con `sys.exit(mensaje)` en vez de un traceback crudo.
- `mcp_server.py` arranca en modo `stdio` por defecto y en modo HTTP si `MCP_TRANSPORT=http`. En modo HTTP, el acceso está protegido con una clave compartida (`MCP_AUTH_TOKEN`), aceptada por cabecera `Authorization: Bearer` o por `?apiKey=` en la URL (para enlaces de instalación de un clic). No hay gestión de usuarios: es un servidor de un único usuario, pensado para uso personal.
- `mcp_server.py` también expone `POST /internal/login` (vía `@mcp.custom_route`), protegido con una clave distinta (`INTERNAL_LOGIN_TOKEN`). Recibe email/password, hace login en un directorio temporal (para no arriesgar la sesión ya cacheada si falla) y solo si tiene éxito sustituye el token cacheado (`GARMIN_TOKENSTORE`) por el nuevo — así `get_client()` sirve esa cuenta en las siguientes llamadas, sin redeploy. No verificar primero en un directorio aislado dejaría el MCP sin sesión utilizable ante un intento fallido (ya ocurrió en desarrollo).
- `connector_web.py` es la interfaz para lo anterior: un formulario que reenvía las credenciales a `/internal/login` y, si el login es válido, muestra la URL del conector (`{MCP_PUBLIC_URL}/?apiKey={MCP_AUTH_TOKEN}`) lista para pegar en Claude. No importa `garminconnect` directamente, es solo UI + proxy HTTP.
- Limitación conocida y aceptada: no hay clave que proteja el formulario de `connector_web.py`, así que cualquiera con la URL y una cuenta de Garmin propia puede cambiar qué cuenta sirve el MCP. Solo hay una cuenta activa a la vez (no es multiusuario real). A revisar cuando se diseñe multiusuario.
- Despliegue en Railway: dos servicios en el mismo proyecto — `mcp_server.py` (con volumen montado para la caché de tokens) y `connector_web.py` (sin volumen), este último con `MCP_AUTH_TOKEN`/`INTERNAL_LOGIN_TOKEN` como referencias cruzadas a las variables del primero para no duplicar secretos.
