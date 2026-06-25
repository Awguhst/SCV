"""Authentication for the Single View of Wealth (SVW) platform.

A minimal JWT-based login layered on top of the existing DuckDB store, with
two demo roles:

* **admin**   - everything an analyst can do, plus the destructive/expensive
  pipeline operations (`POST /generate-data`, `POST /run-linkage`).
* **analyst** - read/search/export the resolved wealth profiles.

This is POC-scoped on purpose: two seeded demo accounts, no signup, no
password reset, no refresh tokens - matching the rest of this project's
"no manual setup required" philosophy. `SVW_JWT_SECRET` should be overridden
with a real secret outside of local/demo use; a dev-only fallback is used
otherwise so the app still runs out of the box.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.data_generator import get_connection

logger = logging.getLogger("svw")

# POC-only demo credentials - clearly not for production use.
DEMO_USERS = [
    ("admin", "admin123", "admin"),
    ("analyst", "analyst123", "analyst"),
]

_DEV_JWT_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
JWT_SECRET = os.environ.get("SVW_JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = _DEV_JWT_SECRET
    logger.warning(
        "SVW_JWT_SECRET is not set - using an insecure development default. "
        "Set SVW_JWT_SECRET before exposing this app beyond local/demo use."
    )

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def seed_demo_users() -> None:
    """Create the `users` table (if missing) and seed the demo accounts.

    Idempotent: safe to call on every startup. Untouched by
    `data_generator.generate_all()`, which only rebuilds the synthetic
    payroll/cluster tables - who can log in has nothing to do with which
    synthetic dataset is currently loaded.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR PRIMARY KEY,
                password_hash VARCHAR,
                role VARCHAR,
                created_at TIMESTAMP DEFAULT now()
            )
            """
        )
        for username, password, role in DEMO_USERS:
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", [username]).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    [username, hash_password(password), role],
                )
    finally:
        conn.close()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _get_user(username: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?", [username]
        ).fetchone()
        if row is None:
            return None
        return {"username": row[0], "password_hash": row[1], "role": row[2]}
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> dict | None:
    """Return the user row if `username`/`password` are valid, else None."""
    user = _get_user(username)
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    return user


def create_access_token(username: str, role: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": username, "role": role, "exp": expires_at}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises `jwt.PyJWTError` on failure."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """FastAPI dependency: resolve the bearer token to a live user row.

    Re-fetches from `users` (rather than trusting the token's claims alone)
    so a role change takes effect immediately rather than waiting out the
    token's expiry - cheap given every other lookup in this app already
    opens a short-lived DuckDB connection per call.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        username = payload.get("sub")
        if username is None:
            raise credentials_error
    except jwt.PyJWTError:
        raise credentials_error

    user = _get_user(username)
    if user is None:
        raise credentials_error
    return user


def require_role(*roles: str):
    """FastAPI dependency factory: 403s unless the current user's role is
    one of `roles`. Usage: `Depends(require_role("admin"))`."""

    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {' or '.join(roles)}",
            )
        return user

    return _check
