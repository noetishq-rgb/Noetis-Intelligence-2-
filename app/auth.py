import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.db import set_actor, transaction
from app.models import TokenPair

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
dummy_hash = hasher.hash("dummy-password-not-an-account")
bearer = HTTPBearer(auto_error=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str) -> str:
    return hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        valid = hasher.verify(stored or dummy_hash, password)
        return bool(stored and valid)
    except (VerificationError, InvalidHashError):
        return False


def subject_hash(value: str) -> str:
    return hmac.new(settings().signing_secret().encode(), value.encode(), hashlib.sha256).hexdigest()


def auth_event(connection, event: str, success: bool, actor_id=None, subject=None, session_id=None):
    connection.execute("INSERT INTO identity.auth_events(actor_id,event,subject_hash,session_id,success) VALUES(%s,%s,%s,%s,%s)",
                       (actor_id, event, subject_hash(subject) if subject else None, session_id, success))


def issue_tokens(connection, user_id: UUID, family_id: UUID | None = None) -> TokenPair:
    config = settings()
    session_id, refresh, family = uuid4(), secrets.token_urlsafe(48), family_id or uuid4()
    now = datetime.now(timezone.utc)
    set_actor(connection, user_id)
    connection.execute("INSERT INTO identity.sessions(id,user_id,refresh_hash,family_id,expires_at) VALUES(%s,%s,%s,%s,%s)",
                       (session_id, user_id, digest(refresh), family, now + timedelta(days=config.refresh_days)))
    encoded = jwt.encode({"sub": str(user_id), "sid": str(session_id), "jti": str(uuid4()), "iat": now,
                          "exp": now + timedelta(minutes=config.access_minutes), "iss": config.jwt_issuer,
                          "aud": config.jwt_audience}, config.signing_secret(), algorithm="HS256")
    return TokenPair(access_token=encoded, refresh_token=refresh, expires_in=config.access_minutes * 60)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    config = settings()
    try:
        claims = jwt.decode(credentials.credentials, config.signing_secret(), algorithms=["HS256"],
                            issuer=config.jwt_issuer, audience=config.jwt_audience,
                            options={"require": ["sub", "sid", "jti", "iat", "exp", "iss", "aud"]})
        actor, session_id = UUID(claims["sub"]), UUID(claims["sid"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired access token")
    with transaction(actor) as connection:
        session = connection.execute("SELECT id FROM identity.sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND consumed_at IS NULL AND expires_at>now()", (session_id, actor)).fetchone()
        user = connection.execute("SELECT id,email,full_name,disabled,is_service FROM identity.users WHERE id=%s", (actor,)).fetchone()
        if not session or not user or user["disabled"] or user["is_service"]:
            raise HTTPException(401, "Session no longer active")
        user["session_id"] = session_id
        return user


def actor_transaction(request: Request, user=Depends(current_user)):
    with transaction(user["id"]) as connection:
        connection.execute("SELECT set_config('app.request_id',%s,true)", (getattr(request.state, "request_id", str(user["session_id"])),))
        yield connection


def require_brand(connection, brand_id: UUID, scope: str) -> dict:
    row = connection.execute("SELECT *,mi.org_role(organization_id) AS actor_role FROM mi.brand_workspaces WHERE id=%s AND mi.has_brand_scope(id,%s)", (brand_id, scope)).fetchone()
    if not row:
        # The same response is used for absent and inaccessible brands.
        raise HTTPException(404, "Brand workspace not found")
    connection.execute("SELECT mi.log_access(%s,%s)", (brand_id, scope))
    return row


def require_admin(connection, organization_id: UUID):
    allowed = connection.execute("SELECT mi.can_org_admin(%s) AS allowed", (organization_id,)).fetchone()
    if not allowed["allowed"]:
        raise HTTPException(404, "Organization not found")


def throttle(connection, address: str, request: Request) -> list[str]:
    # Do not trust X-Forwarded-For from callers. Proxy trust is a deployment decision.
    ip = request.client.host if request.client else "unknown"
    keys = [subject_hash("account:" + address.casefold()), subject_hash("ip:" + ip)]
    for key in keys:
        allowed = connection.execute("SELECT identity.check_attempt(%s) AS allowed", (key,)).fetchone()["allowed"]
        if not allowed:
            return []
    return keys
