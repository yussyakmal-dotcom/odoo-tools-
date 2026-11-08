"""Tests for Odoo connection settings and read-only JSON-2 probes.

All outbound HTTP calls are mocked. These tests never contact a live Odoo.
"""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from odoo_tools.cli import bootstrap_admin
from odoo_tools.config import Settings
from odoo_tools.db import open_db
from odoo_tools.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = str(tmp_path / "odoo-connection.sqlite3")
    monkeypatch.setenv("BOM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    bootstrap_admin("administrator", "InitialAdminPassword123!", path)
    with TestClient(create_app(Settings(db_path=path))) as instance:
        csrf = instance.post("/api/v1/auth/login", json={
            "username": "administrator", "password": "InitialAdminPassword123!",
        }).json()["csrf_token"]
        workspace = instance.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf},
                                  json={"name": "Production", "slug": "production"})
        assert workspace.status_code == 201, workspace.text
        yield instance, csrf, workspace.json()["id"], path


def conn_path(wid):
    return f"/api/v1/workspaces/{wid}/odoo/connection"


def put_connection(client, csrf, wid, **overrides):
    values = {"base_url": "https://demo.odoo.com", "database": "demo",
              "api_key": "s3cret-odoo-api-key"}
    values.update(overrides)
    return client.put(conn_path(wid), json=values, headers={"X-CSRF-Token": csrf})


def test_connection_is_encrypted_and_secret_never_readable(client):
    instance, csrf, wid, path = client
    response = put_connection(instance, csrf, wid)
    assert response.status_code == 200, response.text
    assert "s3cret-odoo-api-key" not in response.text
    public = instance.get(conn_path(wid))
    assert public.status_code == 200
    assert public.json()["api_key_configured"] is True
    assert "encrypted_api_key" not in public.text
    with open_db(path) as db:
        encrypted = db.execute(
            "SELECT encrypted_api_key FROM workspace_odoo_connections WHERE workspace_id=?",
            (wid,),
        ).fetchone()[0]
        assert b"s3cret-odoo-api-key" not in encrypted
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='odoo.connection.updated'"
        ).fetchone()[0] == 1

    # Blank/omitted key retains existing ciphertext; changing URL clears last test.
    update = instance.put(conn_path(wid), json={
        "base_url": "https://demo.odoo.com", "database": "renamed",
    }, headers={"X-CSRF-Token": csrf})
    assert update.status_code == 200
    assert update.json()["database"] == "renamed"


@pytest.mark.parametrize("bad_url", [
    "http://demo.odoo.com",
    "https://localhost",
    "https://127.0.0.1",
    "https://demo.odoo.com.evil.example",
    "https://demo.odoo.com/bom",
    "https://user:pass@demo.odoo.com",
    "https://demo.odoo.com:700",
])
def test_disallowed_odoo_urls_are_rejected(client, bad_url):
    instance, csrf, wid, _ = client
    assert put_connection(instance, csrf, wid, base_url=bad_url).status_code == 422


def test_read_only_probe_and_partial_permissions(client):
    instance, csrf, wid, path = client
    assert put_connection(instance, csrf, wid).status_code == 200
    calls = []

    def mock_handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.headers["Authorization"] == "bearer s3cret-odoo-api-key"
        assert request.headers["X-Odoo-Database"] == "demo"
        if request.url.path.endswith("/res.users/context_get"):
            return httpx.Response(200, json={"lang": "en_US"})
        if request.url.path.endswith("/mrp.bom.line/search_read"):
            return httpx.Response(403, json={"debug": "never-leak-this"})
        assert request.url.path.endswith("/search_read")
        assert request.content == b'{"domain":[],"fields":["id"],"limit":1}'
        return httpx.Response(200, json=[{"id": 1}])

    instance.app.state.odoo_transport = httpx.MockTransport(mock_handler)
    response = instance.post(conn_path(wid) + "/test", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "partial"
    assert result["connected"] is True
    assert result["models"][1]["model"] == "mrp.bom.line"
    assert result["models"][1]["reason"] == "permission"
    assert len(calls) == 6
    assert "never-leak-this" not in response.text
    with open_db(path) as db:
        assert db.execute(
            "SELECT last_test_status FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)
        ).fetchone()[0] == "partial"


def test_authentication_failure_is_redacted(client):
    instance, csrf, wid, _ = client
    assert put_connection(instance, csrf, wid).status_code == 200
    instance.app.state.odoo_transport = httpx.MockTransport(
        lambda _: httpx.Response(401, json={"debug": "secret-debug-info"})
    )
    response = instance.post(conn_path(wid) + "/test", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "authentication"
    assert "secret-debug-info" not in response.text


def test_encryption_required_and_csrf_enforced(client, monkeypatch):
    instance, csrf, wid, _ = client
    assert instance.put(conn_path(wid), json={
        "base_url": "https://demo.odoo.com", "api_key": "secret",
    }).status_code == 403
    monkeypatch.delenv("BOM_ENCRYPTION_KEY")
    assert put_connection(instance, csrf, wid).status_code == 503


def test_workspace_scope_restricted_for_nonmember(client):
    instance, csrf, wid, _ = client
    result = instance.post("/api/v1/admin/users",
                           headers={"X-CSRF-Token": csrf},
                           json={
                               "username": "viewer1", "display_name": "Viewer",
                               "password": "FirstLoginPassword123!", "is_admin": False,
                           })
    assert result.status_code == 201
    viewer = TestClient(instance.app)
    try:
        result = viewer.post("/api/v1/auth/login", json={
            "username": "viewer1", "password": "FirstLoginPassword123!",
        })
        assert result.status_code == 200
        # Must change temporary password before accessing the workspace.
        token = result.json()["csrf_token"]
        changed = viewer.post("/api/v1/auth/change-password",
                              headers={"X-CSRF-Token": token},
                              json={"current_password": "FirstLoginPassword123!",
                                    "new_password": "SecondLoginPassword123!"})
        assert changed.status_code == 200
        assert viewer.post("/api/v1/auth/login", json={
            "username": "viewer1", "password": "SecondLoginPassword123!",
        }).status_code == 200
        assert viewer.get(conn_path(wid)).status_code == 403
    finally:
        viewer.close()


def test_odoo_settings_survive_app_restart_and_key_is_not_returned(client):
    """Reinitializing FastAPI against the same SQLite volume must preserve settings."""
    instance, csrf, wid, path = client
    saved = put_connection(instance, csrf, wid)
    assert saved.status_code == 200
    with open_db(path) as db:
        secret_before = db.execute(
            "SELECT encrypted_api_key FROM workspace_odoo_connections WHERE workspace_id=?",
            (wid,),
        ).fetchone()[0]
    with TestClient(create_app(Settings(db_path=path))) as restarted:
        signed_in = restarted.post("/api/v1/auth/login", json={
            "username": "administrator", "password": "InitialAdminPassword123!",
        })
        assert signed_in.status_code == 200
        restored = restarted.get(conn_path(wid))
        assert restored.status_code == 200
        assert restored.json()["base_url"] == "https://demo.odoo.com"
        assert restored.json()["database"] == "demo"
        assert restored.json()["api_key_configured"] is True
        assert "s3cret-odoo-api-key" not in restored.text
    with open_db(path) as db:
        secret_after = db.execute(
            "SELECT encrypted_api_key FROM workspace_odoo_connections WHERE workspace_id=?",
            (wid,),
        ).fetchone()[0]
        assert secret_after == secret_before
