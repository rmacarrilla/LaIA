"""Servidor de autorización OAuth cuyo "login de terceros" es, en realidad,
nuestro propio formulario de Garmin: authorize() no redirige a un IdP externo,
sino a /login (ver mcp_server.py), y complete_login() termina el flujo cuando esa
credencial de Garmin queda verificada. Todo el estado (clientes registrados,
authorization codes, access/refresh tokens) vive en Postgres vía db.py."""

import asyncio
import secrets
import time
from datetime import datetime, timedelta, timezone

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel

import db
import garmin_client

ACCESS_TOKEN_TTL_SECONDS = 3600
# Los refresh no caducaban: un token filtrado servía para siempre y su fila
# —que lleva dentro el id de usuario— se quedaba en la tabla indefinidamente,
# sin política de retención. Con caducidad, purge_expired_objects los recoge
# solo, igual que al resto. 30 días es largo para que nadie tenga que volver
# a autorizar por rutina, y corto para que un token perdido no sea eterno.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 600  # tiempo para rellenar el formulario de login
LOGIN_FLOW_TTL_SECONDS = 600


class FlowExpiredError(RuntimeError):
    """El flow_id no existe o caducó — a diferencia de un login de Garmin
    rechazado, esto no es sensible: no hace falta un mensaje genérico."""


class PendingAuthorization(BaseModel):
    """Lo que hay que recordar entre el /authorize inicial y que el usuario
    termine de rellenar /login: a qué client_id pertenece y los parámetros
    originales de la petición (redirect_uri, code_challenge, state, ...)."""

    client_id: str
    params: AuthorizationParams


def _expires_at_dt(seconds_from_now: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)


class GarminOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, issuer_url: str) -> None:
        self._issuer_url = issuer_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await db.load_object("client", client_id, OAuthClientInformationFull)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await db.save_object("client", client_info.client_id, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        flow_id = secrets.token_urlsafe(16)
        pending = PendingAuthorization(client_id=client.client_id, params=params)
        await db.save_object("pending_authorize", flow_id, pending, _expires_at_dt(LOGIN_FLOW_TTL_SECONDS))
        return f"{self._issuer_url}/login?flow={flow_id}"

    async def load_pending_authorization(self, flow_id: str) -> PendingAuthorization | None:
        """Usado por GET /login para saber qué mostrar; ver mcp_server.py."""
        return await db.load_object("pending_authorize", flow_id, PendingAuthorization)

    async def complete_login(self, flow_id: str, email: str, password: str) -> str:
        """Verifica las credenciales de Garmin y, si son válidas, termina el
        flujo OAuth: da de alta (o actualiza) el usuario, emite un authorization
        code y devuelve la URL a la que redirigir de vuelta a Claude.

        Lanza FlowExpiredError si el flow_id no existe/caducó, o RuntimeError
        (mensaje genérico) si Garmin rechaza las credenciales."""
        pending = await self.load_pending_authorization(flow_id)
        if pending is None:
            raise FlowExpiredError("Este enlace de conexión ha caducado. Vuelve a intentarlo desde Claude.")

        # garmin_client.login() es una llamada de red bloqueante (10-20s de espera
        # anti-bot deliberada de Garmin) y esto es una corrutina: sin to_thread
        # bloquearía el event loop entero para cualquier otra petición concurrente.
        try:
            session_blob = await asyncio.to_thread(garmin_client.login, email, password)
        except RuntimeError as err:
            # Nunca relanzar str(err) tal cual: garminconnect usa mensajes
            # distintos según el motivo exacto del fallo (credenciales
            # incorrectas, cuenta inexistente, MFA, ...), y reenviarlos al
            # usuario permitiría enumerar qué cuentas de Garmin existen.
            raise RuntimeError("No se pudo verificar tu email y contraseña de Garmin.") from err

        user_id = await db.upsert_user(email, session_blob)

        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=str(user_id),
        )
        await db.save_object("code", code.code, code, _expires_at_dt(AUTH_CODE_TTL_SECONDS))
        await db.delete_object("pending_authorize", flow_id)

        return construct_redirect_uri(str(pending.params.redirect_uri), code=code.code, state=pending.params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await db.load_object("code", authorization_code, AuthorizationCode)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # El SDK ya validó el code (existe, no caducado, PKCE correcto) con un
        # load_authorization_code() previo — pero entre ese load y este delete
        # otra petición concurrente con el mismo code podría colarse. Que
        # delete_object confirme que de verdad borró algo hace el canje
        # atómico: solo quien gana la carrera por borrar la fila recibe
        # tokens (RFC 6749 §10.5: un code reutilizado se trata como inválido).
        consumed = await db.delete_object("code", authorization_code.code)
        if not consumed:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        return await self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return await db.load_object("refresh", refresh_token, RefreshToken)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Mismo canje atómico que el authorization code, por el mismo motivo:
        # entre el load_refresh_token() del SDK y este borrado, otra petición
        # con el mismo token podría colarse y llevarse un segundo par de
        # tokens válido. Que delete_object confirme que de verdad borró la
        # fila hace que solo gane uno.
        rotated = await db.delete_object("refresh", refresh_token.token)
        if not rotated:
            raise TokenError(error="invalid_grant", error_description="refresh token already used")
        return await self._issue_tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await db.load_object("access", token, AccessToken)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        await db.delete_object(kind, token.token)

    async def delete_account(self, email: str, password: str) -> None:
        """Reautentica contra Garmin (mismo nivel de confianza que /login) y
        borra la cuenta y todos sus tokens — derecho al olvido, autoservicio.
        Reautenticar en vez de fiarse de un access token evita que alguien
        borre la cuenta de otra persona con solo conocer su user_id."""
        await asyncio.to_thread(garmin_client.login, email, password)  # lanza RuntimeError si falla

        user_id = await db.get_user_id_by_email(email)
        if user_id is None:
            return  # login válido pero nunca se registró en LaIA: nada que borrar

        await db.delete_user(user_id)

    async def _issue_tokens(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time() + ACCESS_TOKEN_TTL_SECONDS),
            subject=subject,
        )
        refresh = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes, subject=subject)
        await db.save_object("access", access.token, access, _expires_at_dt(ACCESS_TOKEN_TTL_SECONDS))
        await db.save_object("refresh", refresh.token, refresh, _expires_at_dt(REFRESH_TOKEN_TTL_SECONDS))

        return OAuthToken(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) if scopes else None,
        )
