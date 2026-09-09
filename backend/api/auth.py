"""Virtual API keys -> tenant/application.

Keys live in config (never in the frontend). A key maps to a tenant and a
default application; the caller may name a different application it is
authorised for. Unset keys mean an open local demo, and `/api/config` says so
plainly rather than pretending the deployment is secured.
"""
import hmac
import os
from dataclasses import dataclass

from fastapi import Header, HTTPException

ENV_PREFIX = "OPTIMIZER_KEY_"          # OPTIMIZER_KEY_ACME=sk-...:acme:ops-assistant
ADMIN_ENV = "OPTIMIZER_ADMIN_KEY"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    application_id: str
    key_id: str
    admin: bool = False
    authenticated: bool = True

    def may_use(self, application_id: str | None) -> bool:
        return True if self.admin else (not application_id or application_id == self.application_id)


def _keys() -> dict:
    out = {}
    for name, value in os.environ.items():
        if not name.startswith(ENV_PREFIX) or not value:
            continue
        parts = value.split(":")
        if len(parts) < 2:
            continue
        secret, tenant = parts[0], parts[1]
        app = parts[2] if len(parts) > 2 else "ops-assistant"
        out[secret] = Principal(tenant, app, name[len(ENV_PREFIX):].lower())
    admin = os.getenv(ADMIN_ENV)
    if admin:
        out[admin] = Principal("*", "*", "admin", admin=True)
    return out


def auth_enabled() -> bool:
    return bool(_keys())


def open_principal() -> Principal:
    return Principal("acme", "ops-assistant", "local", admin=True, authenticated=False)


def resolve(authorization: str | None, x_api_key: str | None = None) -> Principal:
    keys = _keys()
    if not keys:
        # No keys configured: local demo. Never silently claim to be secured.
        return open_principal()
    token = x_api_key or ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    for secret, principal in keys.items():
        if hmac.compare_digest(secret, token):
            return principal
    raise HTTPException(401, "invalid or missing API key")


async def principal(authorization: str | None = Header(None),
                    x_api_key: str | None = Header(None)) -> Principal:
    return resolve(authorization, x_api_key)


async def admin(authorization: str | None = Header(None),
                x_api_key: str | None = Header(None)) -> Principal:
    p = resolve(authorization, x_api_key)
    if not p.admin:
        raise HTTPException(403, "this endpoint requires the admin key")
    return p
