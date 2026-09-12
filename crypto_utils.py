"""Cifrado y hashing de los dos datos sensibles que persistimos: el email de
Garmin (PII) y la sesión de Garmin (equivale a una contraseña — da acceso
directo a datos de salud/actividad física, categoría especial bajo GDPR).

Una única clave (SESSION_ENCRYPTION_KEY) sirve para ambos usos: cifrado
simétrico del blob de sesión, y como clave de un HMAC para el hash del email
(evita depender de un segundo secreto, y el HMAC evita que alguien con la base
de datos pero sin la clave pueda precalcular un diccionario de hashes de
emails comunes)."""

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken


def _key() -> bytes:
    # Fallo rápido y explícito si falta la clave, en vez de un error opaco en
    # el primer login real.
    return os.environ["SESSION_ENCRYPTION_KEY"].encode()


def hash_email(email: str) -> str:
    """Hash determinista (para poder buscar por email) pero con clave (para que
    no sea precalculable sin conocer SESSION_ENCRYPTION_KEY)."""
    normalized = email.strip().lower().encode()
    return hmac.new(_key(), normalized, hashlib.sha256).hexdigest()


def encrypt(plaintext: str) -> bytes:
    return Fernet(_fernet_key()).encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    try:
        return Fernet(_fernet_key()).decrypt(ciphertext).decode()
    except InvalidToken as err:
        # No debería pasar salvo que SESSION_ENCRYPTION_KEY haya cambiado o los
        # datos estén corruptos; un error claro es mejor que uno críptico de Fernet.
        raise RuntimeError("No se pudo descifrar la sesión guardada (¿cambió SESSION_ENCRYPTION_KEY?)") from err


def _fernet_key() -> bytes:
    # Fernet exige una clave de 32 bytes en base64 urlsafe; derivamos una a
    # partir de la clave "cruda" configurada para no exigir que el usuario
    # genere el formato exacto de Fernet a mano.
    raw = hashlib.sha256(_key()).digest()
    return base64.urlsafe_b64encode(raw)
