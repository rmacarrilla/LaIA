"""Invariante 1: un authorization code o un refresh token ya canjeado se
rechaza, incluso si dos peticiones llegan a la vez.

Por qué está aquí: este fallo ya ocurrió dos veces en el proyecto. Primero
con el authorization code —el `load` del SDK y nuestro `delete` son dos
llamadas async separadas, no una transacción, así que dos canjes concurrentes
podían colarse ambos— y después, idéntico, con el refresh token, donde se
ignoraba el valor de retorno de `delete_object`. Las dos veces el síntoma
sería el mismo: dos pares de tokens válidos salidos de una credencial de un
solo uso.
"""

import asyncio
import time

import pytest
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import oauth_provider


def _cliente():
    return OAuthClientInformationFull(client_id="c1", redirect_uris=[AnyUrl("https://ejemplo.test/cb")])


def _code(valor="CODE1"):
    return AuthorizationCode(
        code=valor,
        scopes=["activities"],
        expires_at=time.time() + 600,
        client_id="c1",
        code_challenge="reto",
        redirect_uri=AnyUrl("https://ejemplo.test/cb"),
        redirect_uri_provided_explicitly=True,
        subject="7",
    )


async def test_el_code_solo_sirve_una_vez(db_falso):
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")
    code = _code()
    db_falso.filas[("code", code.code)] = code

    par = await prov.exchange_authorization_code(_cliente(), code)
    assert par.access_token and par.refresh_token

    with pytest.raises(TokenError) as err:
        await prov.exchange_authorization_code(_cliente(), code)
    assert err.value.error == "invalid_grant"


async def test_el_refresh_solo_sirve_una_vez(db_falso):
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")
    refresh = RefreshToken(token="R1", client_id="c1", scopes=["activities"], subject="7")
    db_falso.filas[("refresh", refresh.token)] = refresh

    await prov.exchange_refresh_token(_cliente(), refresh, [])

    with pytest.raises(TokenError) as err:
        await prov.exchange_refresh_token(_cliente(), refresh, [])
    assert err.value.error == "invalid_grant"


@pytest.mark.parametrize("tipo", ["code", "refresh"])
async def test_en_carrera_solo_gana_uno(db_falso, tipo):
    """Lo que de verdad protege el borrado atómico: dos canjes simultáneos."""
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")
    cliente = _cliente()

    if tipo == "code":
        credencial = _code("CARRERA")
        db_falso.filas[("code", credencial.code)] = credencial
        canje = lambda: prov.exchange_authorization_code(cliente, credencial)
    else:
        credencial = RefreshToken(token="CARRERA", client_id="c1", scopes=[], subject="7")
        db_falso.filas[("refresh", credencial.token)] = credencial
        canje = lambda: prov.exchange_refresh_token(cliente, credencial, [])

    resultados = await asyncio.gather(canje(), canje(), return_exceptions=True)
    exitos = [r for r in resultados if not isinstance(r, Exception)]
    rechazos = [r for r in resultados if isinstance(r, TokenError)]

    assert len(exitos) == 1, "dos peticiones concurrentes se llevaron tokens del mismo code/refresh"
    assert len(rechazos) == 1


async def test_los_tokens_emitidos_caducan(db_falso):
    """Un refresh sin caducidad sirve para siempre si se filtra, y su fila
    —que lleva dentro el id de usuario— se queda en la tabla sin retención."""
    prov = oauth_provider.GarminOAuthProvider("https://laia.test")
    code = _code("CADUCA")
    db_falso.filas[("code", code.code)] = code

    await prov.exchange_authorization_code(_cliente(), code)

    for kind, _clave, expires_at in db_falso.guardados:
        if kind in ("access", "refresh"):
            assert expires_at is not None, f"{kind} guardado sin caducidad"
