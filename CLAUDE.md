# Garmin MCP Server

Servidor MCP en Python que consulta las últimas actividades de una cuenta de Garmin Connect vía la librería no oficial `garminconnect`, para uso personal, y las expone a Claude (local o remoto).

## Comandos

```bash
source venv/bin/activate         # activar entorno virtual
python mcp_server.py             # servidor MCP en local (stdio)
MCP_TRANSPORT=http python mcp_server.py  # servidor MCP remoto (HTTP)
pip install -r requirements.txt  # instalar/actualizar dependencias
```

## Convenciones del proyecto

- Las credenciales viven en `.env` (nunca en el código ni en commits). `.env.example` documenta las claves esperadas.
- `requirements.txt` fija versiones exactas (`==`), no rangos, para reproducibilidad.
- El login a Garmin está centralizado en `garmin_client.py` (`get_client()`), reutilizado por `mcp_server.py`.
- El login usa caché de tokens (por defecto en `~/.garminconnect`, configurable con `GARMIN_TOKENSTORE`) para evitar reautenticar con usuario/contraseña en cada ejecución y reducir rate limiting (errores 429) de Garmin. En Railway esto requiere un volumen persistente, ya que el filesystem es efímero.
- Errores de login/conexión se capturan explícitamente (`GarminConnectAuthenticationError`, `GarminConnectConnectionError`) y terminan el programa con `sys.exit(mensaje)` en vez de un traceback crudo.
- `mcp_server.py` arranca en modo `stdio` por defecto y en modo HTTP si `MCP_TRANSPORT=http`. En modo HTTP, el acceso está protegido con una clave compartida (`MCP_AUTH_TOKEN`), aceptada por cabecera `Authorization: Bearer` o por `?apiKey=` en la URL (para enlaces de instalación de un clic). No hay gestión de usuarios: es un servidor de un único usuario, pensado para uso personal.
- Despliegue en Railway: un servicio web para `mcp_server.py` (con dominio público) con un volumen montado para la caché de tokens.
