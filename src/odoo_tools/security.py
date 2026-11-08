"""Password policy, secure sessions, CSRF and coarse login throttling."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import sqlite3

from argon2 import PasswordHasher, exceptions
from fastapi import HTTPException

from .db import transaction

COOKIE_NAME = "bom_session"
PASSWORD_HASHER = PasswordHasher()
DUMMY_HASH = PASSWORD_HASHER.hash("dummy-unknown-username")


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 128 or not password.strip():
        raise HTTPException(422, "Password must be 12-128 characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str | None, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded or DUMMY_HASH, password)
    except (exceptions.VerifyMismatchError, exceptions.VerificationError, exceptions.InvalidHashError):
        return False


def issue_session(db: sqlite3.Connection, user_id: int, ttl_hours: int) -> tuple[str, str]:
    token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions(user_id,token_hash,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
        (user_id, digest(token), digest(csrf_token), timestamp(), timestamp(now() + timedelta(hours=ttl_hours))),
    )
    return token, csrf_token


def audit(db: sqlite3.Connection, actor_id: int | None, action: str, target: str = "", workspace_id: int | None = None) -> None:
    db.execute(
        "INSERT INTO audit_events(actor_id,action,target,workspace_id,created_at) VALUES(?,?,?,?,?)",
        (actor_id, action, target, workspace_id, timestamp()),
    )


def too_many_attempts(db: sqlite3.Connection, ip: str) -> bool:
    window = timestamp(now() - timedelta(minutes=15))
    db.execute("DELETE FROM login_failures WHERE created_at < ?", (window,))
    return db.execute(
        "SELECT COUNT(*) FROM login_failures WHERE ip_address=? AND created_at>=?", (ip, window)
    ).fetchone()[0] >= 8


def register_failure(db: sqlite3.Connection, ip: str) -> None:
    db.execute("INSERT INTO login_failures(ip_address, created_at) VALUES (?,?)", (ip, timestamp()))


def clear_failures(db: sqlite3.Connection, ip: str) -> None:
    db.execute("DELETE FROM login_failures WHERE ip_address=?", (ip,))
