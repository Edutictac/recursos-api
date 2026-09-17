"""Integración OIDC con Authentik (EduTicTac Commons).

Thin wrapper sobre `edutictac_community.oidc.OIDCClient` que conserva la
superficie usada por `main.py` (enabled / build_login_url / handle_callback /
is_admin_sub / OIDC_STATE_COOKIE). El flujo es authorization code + PKCE.
"""
from __future__ import annotations

import os

import httpx
from authlib.jose import jwt

from edutictac_community.oidc import OIDCClient


class ContainerAwareOIDCClient(OIDCClient):
    """Use private container endpoints while keeping a public OIDC issuer."""

    def __init__(self, *args, discovery_url: str = "", authorization_url: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.discovery_url = discovery_url
        self.authorization_url = authorization_url

    def _metadata(self) -> dict:
        if self._meta is None:
            url = self.discovery_url or (self.issuer + "/.well-known/openid-configuration")
            response = httpx.get(url, timeout=20, headers={"User-Agent": self.user_agent})
            response.raise_for_status()
            metadata = response.json()
            if self.discovery_url:
                public_root = self.issuer.split("/application/o/", 1)[0]
                internal_root = self.discovery_url.split("/application/o/", 1)[0]
                app_path = "/application/o/" + self.issuer.split("/application/o/", 1)[1]
                metadata.update({
                    "issuer": self.issuer + "/",
                    "authorization_endpoint": self.authorization_url or f"{public_root}/application/o/authorize/",
                    "token_endpoint": f"{internal_root}/application/o/token/",
                    "userinfo_endpoint": f"{internal_root}/application/o/userinfo/",
                    "jwks_uri": f"{internal_root}{app_path}/jwks/",
                })
            self._meta = metadata
        return self._meta

    def _userinfo_from_id_token(self, token: dict, meta: dict) -> dict:
        """Validate Authentik's issuer with or without its trailing slash."""
        id_token = token.get("id_token") if isinstance(token, dict) else None
        if not id_token:
            return {}
        accepted = {meta["issuer"], meta["issuer"].rstrip("/")}
        if self.discovery_url:
            internal_issuer = self.discovery_url.split("/.well-known/", 1)[0].rstrip("/")
            accepted.update({internal_issuer, f"{internal_issuer}/"})
        claims = jwt.decode(
            id_token,
            self._jwks(meta),
            claims_options={
                "iss": {"essential": True, "values": sorted(accepted)},
                "aud": {"essential": True, "value": self.client_id},
                "sub": {"essential": True},
                "exp": {"essential": True},
            },
        )
        claims.validate(leeway=60)
        return dict(claims)

OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_AUTHORIZATION_URL = os.environ.get("OIDC_AUTHORIZATION_URL", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPE = os.environ.get("OIDC_SCOPE", "openid")
OIDC_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("OIDC_ADMIN_EMAILS", "").split(",")
    if e.strip()
}
OIDC_ADMIN_SUBS = {
    s.strip()
    for s in os.environ.get("OIDC_ADMIN_SUBS", "").split(",")
    if s.strip()
}

SESSION_SECRET = os.environ.get("RECURSOS_SECRET", "")
OIDC_STATE_COOKIE = "recursos_oidc_state"

_client = ContainerAwareOIDCClient(
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_ISSUER,
    OIDC_REDIRECT_URI,
    scope=OIDC_SCOPE,
    session_secret=SESSION_SECRET,
    state_cookie_name=OIDC_STATE_COOKIE,
    user_agent="EduTicTac-Resources/0.1",
    admin_subjects=OIDC_ADMIN_SUBS,
    admin_emails=OIDC_ADMIN_EMAILS,
    discovery_url=OIDC_DISCOVERY_URL,
    authorization_url=OIDC_AUTHORIZATION_URL,
)


def enabled() -> bool:
    return _client.enabled()


def is_admin_sub(sub: str) -> bool:
    return sub in OIDC_ADMIN_SUBS


def build_login_url() -> tuple[str, str]:
    return _client.build_login_url()


def handle_callback(state: str, code: str, state_cookie: str | None) -> dict:
    return _client.handle_callback(state, code, state_cookie)
