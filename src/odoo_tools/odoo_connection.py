"""Workspace-scoped Odoo 19 JSON-2 connection configuration and read-only probes."""
from __future__ import annotations

import os
import re
import sqlite3
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from .db import open_db, transaction
from .security import audit, timestamp


MODEL_PROBES = ("mrp.bom", "mrp.bom.line", "product.product", "product.template", "uom.uom")


class ConnectionUpdate(BaseModel):
    base_url: str = Field(min_length=12, max_length=255)
    database: str | None = Field(default=None, max_length=128)
    api_key: SecretStr | None = None


class OdooProbeError(Exception):
    def __init__(self, category: str, status: int | None = None):
        self.category = category
        self.status = status
        super().__init__(category)


def initialize_schema(db_path: str) -> None:
    with open_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_odoo_connections (
                workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                base_url TEXT NOT NULL,
                database_name TEXT,
                encrypted_api_key BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                last_tested_at TEXT,
                last_test_status TEXT
            )
        """)


def _fernet() -> Fernet:
    raw = os.getenv("BOM_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise HTTPException(503, "Server encryption key is not configured")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(503, "Server encryption key is invalid") from exc


def _allowed_host(host: str) -> bool:
    # SaaS hosts work by default. Custom Odoo domains must be explicitly listed
    # in BOM_ODOO_ALLOWED_HOSTS on the server, never supplied by the browser.
    configured = os.getenv("BOM_ODOO_ALLOWED_HOSTS", "")
    allowed = [".odoo.com"] + [x.strip().lower() for x in configured.split(",") if x.strip()]
    return any((host.endswith(x) and host != x[1:]) if x.startswith(".") else host == x for x in allowed)


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        port = parsed.port  # may raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "Invalid Odoo URL") from exc
    hostname = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9.-]+", hostname)
            or hostname.startswith(".") or hostname.endswith(".") or ".." in hostname
            or not _allowed_host(hostname)):
        raise HTTPException(422, "Use an allowed HTTPS Odoo hostname without path or port")
    return "https://" + hostname


def sanitize_database(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise HTTPException(422, "Invalid Odoo database name")
    return value or None


def public_connection(row: sqlite3.Row | None) -> dict:
    if not row:
        return {"configured": False, "base_url": None, "database": None,
                "api_key_configured": False, "updated_at": None,
                "last_tested_at": None, "last_test_status": None}
    return {"configured": True, "base_url": row["base_url"],
            "database": row["database_name"], "api_key_configured": True,
            "updated_at": row["updated_at"], "last_tested_at": row["last_tested_at"],
            "last_test_status": row["last_test_status"]}


def decrypt_key(encrypted: bytes) -> str:
    try:
        return _fernet().decrypt(encrypted).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise HTTPException(503, "Stored Odoo credential cannot be decrypted; check server encryption key") from exc


async def json2_call(client: httpx.AsyncClient, base_url: str, database: str | None,
                     api_key: str, model: str, method: str, payload: dict) -> object:
    headers = {"Authorization": "bearer " + api_key,
               "Content-Type": "application/json; charset=utf-8",
               "User-Agent": "odoo-bom-tools/0.2"}
    if database:
        headers["X-Odoo-Database"] = database
    try:
        response = await client.post(f"{base_url}/json/2/{model}/{method}", headers=headers, json=payload)
    except httpx.TransportError as exc:
        raise OdooProbeError("network") from exc
    if response.status_code in (401, 403):
        raise OdooProbeError("authentication" if response.status_code == 401 else "permission", response.status_code)
    if response.status_code == 404:
        raise OdooProbeError("not_found", 404)
    if response.status_code >= 400:
        raise OdooProbeError("odoo_error", response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise OdooProbeError("invalid_response") from exc


async def probe_connection(request: Request, base_url: str, database: str | None, api_key: str) -> dict:
    # For tests, MockTransport may be supplied by app.state, never by user input.
    transport = getattr(request.app.state, "odoo_transport", None)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False,
                                 trust_env=False, transport=transport) as client:
        try:
            await json2_call(client, base_url, database, api_key, "res.users", "context_get", {})
        except OdooProbeError as exc:
            return {"connected": False, "error": exc.category, "models": []}
        results = []
        for model in MODEL_PROBES:
            try:
                records = await json2_call(client, base_url, database, api_key, model, "search_read",
                                           {"domain": [], "fields": ["id"], "limit": 1})
                if not isinstance(records, list):
                    raise OdooProbeError("invalid_response")
                results.append({"model": model, "status": "readable", "sample_found": bool(records)})
            except OdooProbeError as exc:
                results.append({"model": model, "status": "unavailable", "reason": exc.category})
        return {"connected": True, "models": results,
                "all_models_readable": all(x["status"] == "readable" for x in results)}


def router_for(workspace_access, require_ready, csrf_guard, db_path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces/{wid}/odoo", tags=["Odoo connection"])

    @router.get("/connection")
    def get_connection(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user, manage=True)
        with open_db(db_path(request)) as db:
            row = db.execute("SELECT * FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)).fetchone()
            return public_connection(row)

    @router.put("/connection", dependencies=[Depends(csrf_guard)])
    def save_connection(wid: int, body: ConnectionUpdate, request: Request,
                        user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user, manage=True)
        url = normalize_url(body.base_url)
        database = sanitize_database(body.database)
        key = body.api_key.get_secret_value().strip() if body.api_key is not None else ""
        if key and (len(key) > 1024 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key)):
            raise HTTPException(422, "API key format is invalid")
        # Fail closed if encryption secret was not configured, even when only URL changes.
        crypto = _fernet()
        with transaction(db_path(request)) as db:
            existing = db.execute("SELECT encrypted_api_key FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)).fetchone()
            if not key and not existing:
                raise HTTPException(422, "API key is required for a new connection")
            if key:
                encrypted = crypto.encrypt(key.encode("utf-8"))
            else:
                encrypted = existing["encrypted_api_key"]
                # Prevent saving changes with an unrecoverable old encryption key.
                decrypt_key(encrypted)
            db.execute("""
                INSERT INTO workspace_odoo_connections
                    (workspace_id,base_url,database_name,encrypted_api_key,updated_at,last_tested_at,last_test_status)
                VALUES(?,?,?,?,?,NULL,NULL)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    base_url=excluded.base_url, database_name=excluded.database_name,
                    encrypted_api_key=excluded.encrypted_api_key, updated_at=excluded.updated_at,
                    last_tested_at=NULL, last_test_status=NULL
            """, (wid, url, database, encrypted, timestamp()))
            audit(db, user["id"], "odoo.connection.updated", str(wid), wid)
            row = db.execute("SELECT * FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)).fetchone()
            return public_connection(row)

    @router.post("/connection/test", dependencies=[Depends(csrf_guard)])
    async def test_connection(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user, manage=True)
        with open_db(db_path(request)) as db:
            row = db.execute("SELECT * FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)).fetchone()
        if not row:
            raise HTTPException(409, "Odoo connection is not configured")
        api_key = decrypt_key(row["encrypted_api_key"])
        result = await probe_connection(request, row["base_url"], row["database_name"], api_key)
        status = "connected" if result["connected"] and result.get("all_models_readable") else "partial" if result["connected"] else "failed"
        with transaction(db_path(request)) as db:
            # If config changed during network call, do not attach stale probe results.
            cursor = db.execute(
                "UPDATE workspace_odoo_connections SET last_tested_at=?,last_test_status=? "
                "WHERE workspace_id=? AND updated_at=?",
                (timestamp(), status, wid, row["updated_at"]),
            )
            if cursor.rowcount:
                audit(db, user["id"], "odoo.connection.tested", status, wid)
        return {"status": status, **result}

    return router
