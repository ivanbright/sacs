"""Password hashing, opaque session tokens, and session lifecycle.

Sessions are *server-validated*: the browser receives an opaque random token,
the database stores only its SHA-256 hash, and every protected request resolves
the token back to a live, unrevoked, unexpired session. No signed client-side
claims are trusted on their own.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .config import get_settings
from .models import Session as AuthSession
from .models import User, utcnow

_PBKDF2_ITERATIONS = 200_000
_TOKEN_BYTES = 32


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Tokens & sessions
# --------------------------------------------------------------------------- #

def generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: OrmSession,
    *,
    user: User,
    school_id: str | None = None,
    label: str | None = None,
    ttl_hours: int | None = None,
) -> tuple[AuthSession, str]:
    settings = get_settings()
    token = generate_token()
    expires_at = utcnow() + timedelta(hours=ttl_hours or settings.session_ttl_hours)
    session = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        school_id=school_id,
        expires_at=expires_at,
        label=label,
    )
    db.add(session)
    db.flush()
    return session, token


def resolve_session(db: OrmSession, token: str) -> AuthSession | None:
    if not token:
        return None
    session = db.execute(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if session is None or session.revoked:
        return None
    if session.expires_at < utcnow():
        return None
    return session


def revoke_session(db: OrmSession, session: AuthSession) -> None:
    session.revoked = True
