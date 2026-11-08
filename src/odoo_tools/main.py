"""FastAPI application: local authentication and workspace foundation.

No Odoo API write/sync endpoints exist in this milestone.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import re
import secrets
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from .config import Settings
from .db import initialize, open_db, transaction
from .security import (
    COOKIE_NAME, audit, clear_failures, digest, hash_password, issue_session,
    now, register_failure, timestamp, too_many_attempts, verify_password,
)

STATIC_DIR = Path(__file__).parent / "static"
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
ROLE_NAMES = {"workspace_admin", "editor", "viewer"}


class LoginInput(BaseModel):
    username: str
    password: SecretStr


class ChangePasswordInput(BaseModel):
    current_password: SecretStr
    new_password: SecretStr


class UserCreate(BaseModel):
    username: str
    display_name: str = Field(min_length=1, max_length=100)
    password: SecretStr
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    is_admin: bool | None = None


class PasswordReset(BaseModel):
    new_password: SecretStr


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=3, max_length=64)


class MembershipChange(BaseModel):
    role: str


def public_user(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"], "username": row["username"],
        "display_name": row["display_name"], "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        "must_change_password": bool(row["must_change_password"]),
    }


def db_path(request: Request) -> str:
    return request.app.state.settings.db_path


def principal(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Authentication required")
    with open_db(db_path(request)) as db:
        result = db.execute(
            "SELECT users.*, sessions.id AS session_id, sessions.csrf_hash "
            "FROM sessions JOIN users ON users.id=sessions.user_id "
            "WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL "
            "AND sessions.expires_at>? AND users.is_active=1",
            (digest(token), timestamp()),
        ).fetchone()
        if result is None:
            raise HTTPException(401, "Session expired or invalid")
        return dict(result)


def require_ready(user: dict = Depends(principal)) -> dict:
    if user["must_change_password"]:
        raise HTTPException(403, "Change your password before using the application")
    return user


def require_admin(user: dict = Depends(require_ready)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator permission required")
    return user


def csrf_guard(request: Request, user: dict = Depends(principal), x_csrf_token: str | None = Header(default=None)) -> None:
    if not x_csrf_token or not secrets.compare_digest(digest(x_csrf_token), user["csrf_hash"]):
        raise HTTPException(403, "Invalid CSRF token")


def workspace_access(request: Request, wid: int, user: dict, *, manage: bool = False) -> dict:
    with open_db(db_path(request)) as db:
        workspace = db.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        if workspace is None:
            raise HTTPException(404, "Workspace not found")
        if user["is_admin"]:
            return dict(workspace)
        membership = db.execute(
            "SELECT role FROM workspace_memberships WHERE user_id=? AND workspace_id=?",
            (user["id"], wid),
        ).fetchone()
        if not membership or (manage and membership["role"] != "workspace_admin"):
            raise HTTPException(403, "Workspace access denied")
        return dict(workspace)


def username_or_422(raw: str) -> str:
    value = raw.strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise HTTPException(422, "Username must be 3-64 letters, digits, dots, dashes or underscores")
    return value


def add_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok", "service": "odoo-bom-tools", "odoo_sync_enabled": False}

    @app.post("/api/v1/auth/login")
    def login(body: LoginInput, request: Request, response: Response) -> dict:
        ip = request.client.host if request.client else "unknown"
        with transaction(db_path(request)) as db:
            if too_many_attempts(db, ip):
                raise HTTPException(429, "Too many attempts. Try again later")
            user = db.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
            valid = verify_password(user["password_hash"] if user else None, body.password.get_secret_value())
            if not user or not user["is_active"] or not valid:
                register_failure(db, ip)
                raise_invalid = True
            else:
                clear_failures(db, ip)
                token, csrf = issue_session(db, user["id"], request.app.state.settings.session_ttl_hours)
                audit(db, user["id"], "auth.login")
                raise_invalid = False
        if raise_invalid:
            raise HTTPException(401, "Invalid username or password")
        settings: Settings = request.app.state.settings
        response.set_cookie(
            COOKIE_NAME, token, httponly=True, secure=settings.cookie_secure,
            samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/",
        )
        return {"user": public_user(user), "csrf_token": csrf}

    @app.get("/api/v1/auth/me")
    def me(request: Request, user: dict = Depends(principal)) -> dict:
        # Rotate the CSRF token on each new page/app session; token never enters a cookie.
        csrf = secrets.token_urlsafe(32)
        with transaction(db_path(request)) as db:
            db.execute("UPDATE sessions SET csrf_hash=? WHERE id=?", (digest(csrf), user["session_id"]))
            if user["is_admin"]:
                workspaces = db.execute("SELECT id,name,slug FROM workspaces ORDER BY name").fetchall()
            else:
                workspaces = db.execute(
                    "SELECT w.id,w.name,w.slug,m.role FROM workspaces w "
                    "JOIN workspace_memberships m ON m.workspace_id=w.id "
                    "WHERE m.user_id=? ORDER BY w.name", (user["id"],),
                ).fetchall()
        return {"user": public_user(user), "csrf_token": csrf, "workspaces": [dict(w) for w in workspaces]}

    @app.post("/api/v1/auth/logout", dependencies=[Depends(csrf_guard)])
    def logout(request: Request, response: Response, user: dict = Depends(principal)) -> dict:
        with transaction(db_path(request)) as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (timestamp(), user["session_id"]))
            audit(db, user["id"], "auth.logout")
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"message": "Signed out"}

    @app.post("/api/v1/auth/change-password", dependencies=[Depends(csrf_guard)])
    def change_password(body: ChangePasswordInput, request: Request, response: Response, user: dict = Depends(principal)) -> dict:
        if not verify_password(user["password_hash"], body.current_password.get_secret_value()):
            raise HTTPException(400, "Current password is incorrect")
        new_hash = hash_password(body.new_password.get_secret_value())
        with transaction(db_path(request)) as db:
            db.execute("UPDATE users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
                       (new_hash, timestamp(), user["id"]))
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (timestamp(), user["id"]))
            audit(db, user["id"], "auth.password.changed")
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"message": "Password changed. Please sign in again."}

    @app.get("/api/v1/admin/users")
    def list_users(request: Request, _: dict = Depends(require_admin)) -> list[dict]:
        with open_db(db_path(request)) as db:
            return [public_user(row) for row in db.execute("SELECT * FROM users ORDER BY id")]

    @app.post("/api/v1/admin/users", status_code=201, dependencies=[Depends(csrf_guard)])
    def create_user(body: UserCreate, request: Request, admin: dict = Depends(require_admin)) -> dict:
        username = username_or_422(body.username)
        password_hash = hash_password(body.password.get_secret_value())
        with transaction(db_path(request)) as db:
            try:
                cursor = db.execute(
                    "INSERT INTO users(username,display_name,password_hash,is_admin,must_change_password,created_at,updated_at) "
                    "VALUES(?,?,?,?,1,?,?)",
                    (username, body.display_name.strip(), password_hash, int(body.is_admin), timestamp(), timestamp()),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Username already exists") from exc
            audit(db, admin["id"], "user.created", str(cursor.lastrowid))
            row = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
            return public_user(row)

    @app.patch("/api/v1/admin/users/{uid}", dependencies=[Depends(csrf_guard)])
    def update_user(uid: int, body: UserUpdate, request: Request, admin: dict = Depends(require_admin)) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "No changes provided")
        with transaction(db_path(request)) as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if user is None:
                raise HTTPException(404, "User not found")
            if changes.get("is_active") is False or changes.get("is_admin") is False:
                if user["is_admin"] and db.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1").fetchone()[0] <= 1:
                    raise HTTPException(409, "Cannot disable or demote the last active Administrator")
            cols, values = [], []
            for field in ("display_name", "is_active", "is_admin"):
                if field in changes:
                    value = changes[field]
                    if field == "display_name" and not value.strip():
                        raise HTTPException(422, "Display name cannot be blank")
                    cols.append(field + "=?")
                    values.append(int(value) if isinstance(value, bool) else value.strip())
            db.execute(f"UPDATE users SET {','.join(cols)},updated_at=? WHERE id=?", (*values, timestamp(), uid))
            if changes.get("is_active") is False or changes.get("is_admin") is False:
                db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (timestamp(), uid))
            audit(db, admin["id"], "user.updated", str(uid))
            return public_user(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    @app.post("/api/v1/admin/users/{uid}/reset-password", dependencies=[Depends(csrf_guard)])
    def reset_password(uid: int, body: PasswordReset, request: Request, admin: dict = Depends(require_admin)) -> dict:
        password_hash = hash_password(body.new_password.get_secret_value())
        with transaction(db_path(request)) as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                raise HTTPException(404, "User not found")
            db.execute("UPDATE users SET password_hash=?,must_change_password=1,updated_at=? WHERE id=?",
                       (password_hash, timestamp(), uid))
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (timestamp(), uid))
            audit(db, admin["id"], "user.password.reset", str(uid))
        return {"message": "Password reset; user must change it on next login"}

    @app.get("/api/v1/workspaces")
    def list_workspaces(request: Request, user: dict = Depends(require_ready)) -> list[dict]:
        with open_db(db_path(request)) as db:
            if user["is_admin"]:
                rows = db.execute("SELECT id,slug,name FROM workspaces ORDER BY name").fetchall()
            else:
                rows = db.execute(
                    "SELECT w.id,w.slug,w.name,m.role FROM workspaces w "
                    "JOIN workspace_memberships m ON m.workspace_id=w.id "
                    "WHERE m.user_id=? ORDER BY w.name", (user["id"],),
                ).fetchall()
            return [dict(row) for row in rows]

    @app.post("/api/v1/workspaces", status_code=201, dependencies=[Depends(csrf_guard)])
    def create_workspace(body: WorkspaceCreate, request: Request, admin: dict = Depends(require_admin)) -> dict:
        slug = body.slug.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", slug):
            raise HTTPException(422, "Slug must use lowercase letters, digits, dashes or underscores")
        if not body.name.strip():
            raise HTTPException(422, "Workspace name cannot be blank")
        with transaction(db_path(request)) as db:
            try:
                cur = db.execute("INSERT INTO workspaces(name,slug,created_at) VALUES(?,?,?)",
                                 (body.name.strip(), slug, timestamp()))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Workspace slug already exists") from exc
            audit(db, admin["id"], "workspace.created", str(cur.lastrowid), cur.lastrowid)
            return dict(db.execute("SELECT id,name,slug FROM workspaces WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.get("/api/v1/workspaces/{wid}/members")
    def list_members(wid: int, request: Request, user: dict = Depends(require_ready)) -> list[dict]:
        workspace_access(request, wid, user, manage=True)
        with open_db(db_path(request)) as db:
            rows = db.execute(
                "SELECT u.id,u.username,u.display_name,m.role FROM workspace_memberships m "
                "JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.username", (wid,)
            ).fetchall()
            return [dict(row) for row in rows]

    @app.put("/api/v1/workspaces/{wid}/members/{uid}", dependencies=[Depends(csrf_guard)])
    def put_member(wid: int, uid: int, body: MembershipChange, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user, manage=True)
        if body.role not in ROLE_NAMES:
            raise HTTPException(422, "Invalid workspace role")
        with transaction(db_path(request)) as db:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND is_active=1", (uid,)).fetchone():
                raise HTTPException(404, "Active user not found")
            db.execute(
                "INSERT INTO workspace_memberships(user_id,workspace_id,role) VALUES(?,?,?) "
                "ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=excluded.role",
                (uid, wid, body.role),
            )
            audit(db, user["id"], "workspace.member.updated", str(uid), wid)
        return {"workspace_id": wid, "user_id": uid, "role": body.role}

    @app.delete("/api/v1/workspaces/{wid}/members/{uid}", dependencies=[Depends(csrf_guard)])
    def delete_member(wid: int, uid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user, manage=True)
        with transaction(db_path(request)) as db:
            count = db.execute("DELETE FROM workspace_memberships WHERE workspace_id=? AND user_id=?", (wid, uid)).rowcount
            if not count:
                raise HTTPException(404, "Workspace membership not found")
            audit(db, user["id"], "workspace.member.removed", str(uid), wid)
        return {"message": "Membership removed"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(settings.db_path)
        from .odoo_connection import initialize_schema
        initialize_schema(settings.db_path)
        from .sync import initialize_sync_schema
        initialize_sync_schema(settings.db_path)
        from .excel_local import initialize_excel_schema
        initialize_excel_schema(settings.db_path)
        yield

    application = FastAPI(title="Odoo 19 BOM Tools", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    add_routes(application)
    from .odoo_connection import router_for
    application.include_router(router_for(workspace_access, require_ready, csrf_guard, db_path))
    from .sync_api import router_for as sync_router_for
    application.include_router(sync_router_for(workspace_access, require_ready, csrf_guard, db_path))
    from .excel_api import router_for as excel_router_for
    application.include_router(excel_router_for(workspace_access, require_ready, csrf_guard, db_path))
    return application


app = create_app()
