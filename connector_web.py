import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

load_dotenv()

app = FastAPI()

# Debe coincidir con INTERNAL_LOGIN_PATH en mcp_server.py.
INTERNAL_LOGIN_PATH = "/internal/login"

PAGE_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
  input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; box-sizing: border-box; }
  button { margin-top: 1.5rem; padding: 0.6rem 1.2rem; cursor: pointer; }
  .error { color: #b00020; margin-top: 1rem; }
  .url-box { display: flex; gap: 0.5rem; margin-top: 1rem; }
  .url-box input { flex: 1; }
  ol { padding-left: 1.2rem; }
</style>
"""

FORM_PAGE = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Conectar Garmin con Claude</title>
  {PAGE_STYLE}
</head>
<body>
  <h1>Conectar tu Garmin con Claude</h1>
  <p>Introduce tu email y contraseña de Garmin Connect para obtener la URL del
     conector MCP que tienes que pegar en Claude.</p>
  <form method="post" action="/connect">
    <label>Email
      <input type="email" name="email" required>
    </label>
    <label>Contraseña
      <input type="password" name="password" required>
    </label>
    <button type="submit">Conectar</button>
  </form>
</body>
</html>"""


def render_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Conectar Garmin con Claude</title>{PAGE_STYLE}</head>
<body>
  <h1>Conectar tu Garmin con Claude</h1>
  <p class="error">{message}</p>
  <p><a href="/">Volver a intentarlo</a></p>
</body>
</html>""",
        status_code=400,
    )


def render_success(connector_url: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Conectar Garmin con Claude</title>{PAGE_STYLE}</head>
<body>
  <h1>¡Listo!</h1>
  <p>Copia esta URL y pégala en Claude para añadir el conector de Garmin:</p>
  <div class="url-box">
    <input id="url" type="text" value="{connector_url}" readonly>
    <button onclick="navigator.clipboard.writeText(document.getElementById('url').value)">Copiar</button>
  </div>
  <h2>Cómo añadirlo en Claude</h2>
  <ol>
    <li>Ve a Configuración → Conectores → Añadir conector personalizado.</li>
    <li>Pega la URL de arriba.</li>
    <li>Dale un nombre, por ejemplo "Garmin".</li>
    <li>Guarda. Ya puedes pedirle a Claude tus actividades de Garmin.</li>
  </ol>
</body>
</html>""")


@app.get("/", response_class=HTMLResponse)
def form() -> str:
    return FORM_PAGE


@app.post("/connect", response_class=HTMLResponse)
def connect(email: str = Form(...), password: str = Form(...)) -> HTMLResponse:
    mcp_url = os.environ["MCP_PUBLIC_URL"]

    try:
        response = requests.post(
            f"{mcp_url}{INTERNAL_LOGIN_PATH}",
            json={"email": email, "password": password},
            headers={"Authorization": f"Bearer {os.environ['INTERNAL_LOGIN_TOKEN']}"},
            timeout=60,
        )
    except requests.RequestException:
        return render_error(
            "No se pudo contactar con el servidor MCP. Inténtalo de nuevo más tarde."
        )

    if response.status_code != 200:
        return render_error("No se pudo verificar tu usuario y contraseña de Garmin.")

    connector_url = f"{mcp_url}/?apiKey={os.environ['MCP_AUTH_TOKEN']}"
    return render_success(connector_url)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
