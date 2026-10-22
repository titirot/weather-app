"""Resolve the signed-in user.

In Azure, App Service built-in authentication (Easy Auth) signs users in with
Microsoft Entra ID and injects trusted X-MS-CLIENT-PRINCIPAL-* headers. Locally,
an explicit opt-in mode simulates one developer user.
"""

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass


class AuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class User:
    external_id: str
    display_name: str


VALID_MODES = ("easyauth", "local")
LOGIN_URL = "/.auth/login/aad?post_login_redirect_uri=/"
LOGOUT_URL = "/.auth/logout?post_logout_redirect_uri=/"


def load_auth_mode(environ: Mapping[str, str]) -> str:
    # Default to Easy Auth so a missing setting fails closed instead of
    # silently enabling the simulated local user.
    mode = environ.get("AUTH_MODE", "easyauth").strip().lower()
    if mode not in VALID_MODES:
        raise AuthConfigurationError("AUTH_MODE must be 'easyauth' or 'local'.")
    if mode == "local" and environ.get("WEBSITE_SITE_NAME"):
        raise AuthConfigurationError("AUTH_MODE=local is not allowed on App Service.")
    return mode


def clean_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = "".join(char for char in value.strip() if char.isprintable())[:100]
    return name or None


def _name_from_principal(encoded: str | None) -> str | None:
    if not encoded or len(encoded) > 16384:
        return None
    try:
        principal = json.loads(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(principal, dict) or not isinstance(principal.get("claims"), list):
        return None
    claims = {
        claim.get("typ"): claim.get("val")
        for claim in principal["claims"]
        if isinstance(claim, dict)
    }
    return clean_display_name(claims.get("name"))


def current_user(
    mode: str, headers: Mapping[str, str], environ: Mapping[str, str]
) -> User | None:
    if mode == "local":
        name = clean_display_name(environ.get("LOCAL_DEV_USER_NAME")) or "Local Developer"
        return User("local:developer", name)

    # App Service strips client-supplied X-MS-CLIENT-PRINCIPAL-* headers only
    # while authentication is enabled, so never trust them otherwise.
    if environ.get("WEBSITE_AUTH_ENABLED", "").strip().lower() != "true":
        return None

    principal_id = headers.get("X-MS-CLIENT-PRINCIPAL-ID", "").strip()
    provider = headers.get("X-MS-CLIENT-PRINCIPAL-IDP", "").strip()
    if not principal_id or not provider or len(principal_id) > 200 or len(provider) > 50:
        return None

    name = (
        _name_from_principal(headers.get("X-MS-CLIENT-PRINCIPAL"))
        or clean_display_name(headers.get("X-MS-CLIENT-PRINCIPAL-NAME"))
        or "User"
    )
    return User(f"{provider}:{principal_id}", name)


def auth_links(mode: str) -> dict:
    if mode == "local":
        return {"login_url": None, "logout_url": None}
    return {"login_url": LOGIN_URL, "logout_url": LOGOUT_URL}
