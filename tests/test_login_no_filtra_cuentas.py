"""Invariante 2: el error de login es siempre el mismo, exista la cuenta de
Garmin o no.

Por qué está aquí: `garminconnect` devuelve mensajes distintos según el motivo
exacto del fallo (contraseña incorrecta, cuenta inexistente, MFA...). Si esos
mensajes llegan al navegador, cualquiera puede probar emails contra el
formulario y averiguar cuáles tienen cuenta en Garmin — enumeración de
cuentas. El mensaje tiene que ser fijo, y el texto original no puede aparecer
ni siquiera parcialmente.
"""

import pytest

import oauth_provider


class _GarminFalso:
    """Reproduce lo que hace garmin_client.login: envolver el error de la
    librería en un RuntimeError cuyo texto SÍ varía según el motivo."""

    def __init__(self, motivo: str):
        self.motivo = motivo

    def __call__(self, email, password):
        raise RuntimeError(f"No se pudo conectar con Garmin: {self.motivo}")


MOTIVOS = [
    "Invalid password for user",          # la cuenta existe, la contraseña no
    "Account not found: no such user",    # la cuenta no existe
    "MFA required for this account",      # existe y además tiene 2FA
]


@pytest.mark.parametrize("motivo", MOTIVOS)
async def test_el_mensaje_no_depende_del_motivo(db_falso, monkeypatch, motivo):
    monkeypatch.setattr(oauth_provider.garmin_client, "login", _GarminFalso(motivo))
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")

    from mcp.server.auth.provider import AuthorizationParams
    from pydantic import AnyUrl

    pendiente = oauth_provider.PendingAuthorization(
        client_id="c1",
        params=AuthorizationParams(
            state=None, scopes=["activities"], code_challenge="reto",
            redirect_uri=AnyUrl("https://ejemplo.test/cb"), redirect_uri_provided_explicitly=True,
        ),
    )
    db_falso.filas[("pending_authorize", "F1")] = pendiente

    with pytest.raises(RuntimeError) as err:
        await prov.complete_login("F1", "quien@sea.test", "lo-que-sea")

    mensaje = str(err.value)
    assert mensaje == "No se pudo verificar tu email y contraseña de Garmin."
    # Lo importante no es solo que el mensaje sea fijo, sino que no se escape
    # ningún fragmento del original por el que deducir el motivo.
    for pista in ("password", "not found", "MFA", "Garmin:"):
        assert pista not in mensaje


async def test_todos_los_motivos_dan_el_mismo_texto(db_falso, monkeypatch):
    """Si algún día alguien añade una rama por motivo, esto lo caza."""
    from mcp.server.auth.provider import AuthorizationParams
    from pydantic import AnyUrl

    prov = oauth_provider.GarminOAuthProvider("https://laia.test")
    mensajes = set()

    for i, motivo in enumerate(MOTIVOS):
        monkeypatch.setattr(oauth_provider.garmin_client, "login", _GarminFalso(motivo))
        db_falso.filas[(f"pending_authorize", f"F{i}")] = oauth_provider.PendingAuthorization(
            client_id="c1",
            params=AuthorizationParams(
                state=None, scopes=[], code_challenge="reto",
                redirect_uri=AnyUrl("https://ejemplo.test/cb"), redirect_uri_provided_explicitly=True,
            ),
        )
        with pytest.raises(RuntimeError) as err:
            await prov.complete_login(f"F{i}", "quien@sea.test", "x")
        mensajes.add(str(err.value))

    assert len(mensajes) == 1, f"el mensaje varía según el motivo: {mensajes}"


async def test_enlace_caducado_es_un_error_distinto(db_falso):
    """Un flow_id inexistente SÍ puede decirse: no depende de ninguna cuenta,
    y confundirlo con un fallo de credenciales manda al usuario a reintentar
    un formulario que nunca va a funcionar."""
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")

    with pytest.raises(oauth_provider.FlowExpiredError):
        await prov.complete_login("no-existe", "quien@sea.test", "x")
