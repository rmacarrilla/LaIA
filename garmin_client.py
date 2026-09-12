import os
import sys

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

DEFAULT_TOKENSTORE = os.path.expanduser("~/.garminconnect")


def get_tokenstore() -> str:
    return os.getenv("GARMIN_TOKENSTORE", DEFAULT_TOKENSTORE)


def get_client() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    try:
        client = Garmin(email=email, password=password)
        client.login(get_tokenstore())
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as err:
        sys.exit(f"No se pudo conectar con Garmin: {err}")

    return client
