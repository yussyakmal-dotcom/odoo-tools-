from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odoo_tools.cli import bootstrap_admin
from odoo_tools.config import Settings
from odoo_tools.main import create_app
from odoo_tools.db import open_db

ADMIN_PASSWORD = "InitialAdminPassphrase123!"


@pytest.fixture
def client(tmp_path: Path):
    db_path = str(tmp_path / "test.sqlite3")
    bootstrap_admin("administrator", ADMIN_PASSWORD, db_path)
    with TestClient(create_app(Settings(db_path=db_path))) as client:
        yield client


def login(client, username="administrator", password=ADMIN_PASSWORD):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def send(client, method, path, csrf, data=None):
    return client.request(method, path, headers={"X-CSRF-Token": csrf}, json=data)


def test_health_and_login(client):
    assert client.get("/api/v1/health").json()["odoo_sync_enabled"] is False
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "administrator", "password": "wrong"}).status_code == 401
    csrf = login(client)
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["is_admin"] is True
    assert "password_hash" not in me.text
    assert send(client, "POST", "/api/v1/auth/logout", csrf).status_code == 403  # /me rotated CSRF
    csrf = me.json()["csrf_token"]
    assert send(client, "POST", "/api/v1/auth/logout", csrf).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_csrf_user_management_and_workspace_isolation(client):
    csrf = login(client)
    assert client.post("/api/v1/workspaces", json={"name": "Production", "slug": "production"}).status_code == 403
    ws = send(client, "POST", "/api/v1/workspaces", csrf, {"name":"Production", "slug":"production"})
    assert ws.status_code == 201, ws.text
    wid = ws.json()["id"]
    new_user = send(client, "POST", "/api/v1/admin/users", csrf, {
        "username":"editor1", "display_name":"Editor One", "password":"TemporaryPassword123!", "is_admin": False
    })
    assert new_user.status_code == 201, new_user.text
    uid = new_user.json()["id"]
    assert send(client, "PUT", f"/api/v1/workspaces/{wid}/members/{uid}", csrf, {"role":"viewer"}).status_code == 200
    csrf = login(client, "editor1", "TemporaryPassword123!")
    assert client.get("/api/v1/workspaces").status_code == 403  # must change temporary password
    changed = send(client, "POST", "/api/v1/auth/change-password", csrf, {
        "current_password":"TemporaryPassword123!", "new_password":"LongTermPassword123!"
    })
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    csrf = login(client, "editor1", "LongTermPassword123!")
    assert len(client.get("/api/v1/workspaces").json()) == 1
    assert client.get(f"/api/v1/workspaces/{wid}/members").status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403
    assert send(client, "POST", "/api/v1/workspaces", csrf, {"name":"Other", "slug":"other"}).status_code == 403


def test_admin_global_workspace_and_last_admin_protection(client):
    csrf=login(client)
    a=send(client, "POST", "/api/v1/workspaces", csrf, {"name":"A", "slug":"aaa"})
    b=send(client, "POST", "/api/v1/workspaces", csrf, {"name":"B", "slug":"bbb"})
    assert a.status_code==201 and b.status_code==201
    assert len(client.get("/api/v1/workspaces").json()) == 2  # no memberships assigned
    own = client.get("/api/v1/admin/users").json()[0]["id"]
    result=send(client, "PATCH", f"/api/v1/admin/users/{own}", csrf, {"is_admin":False})
    assert result.status_code == 409
    assert send(client, "POST", "/api/v1/admin/users/999/reset-password", csrf, {"new_password":"SomeLongPassword123!"}).status_code==404


def test_password_change_revokes_all_sessions(client):
    csrf = login(client)
    result=send(client, "POST", "/api/v1/auth/change-password", csrf, {
        "current_password": ADMIN_PASSWORD,"new_password":"UpdatedAdminPassword123!"
    })
    assert result.status_code==200
    assert client.get("/api/v1/auth/me").status_code==401
    assert client.post("/api/v1/auth/login", json={"username":"administrator", "password":ADMIN_PASSWORD}).status_code==401
    assert login(client,password="UpdatedAdminPassword123!")


def test_bootstrap_only_once_and_persistence(tmp_path):
    path = str(tmp_path / "persist.sqlite3")
    bootstrap_admin("administrator", ADMIN_PASSWORD, path)
    with pytest.raises(ValueError, match="already exists"):
        bootstrap_admin("other", "AnotherAdminPassword123!", path)
    with TestClient(create_app(Settings(db_path=path))) as first:
        csrf=login(first)
        assert send(first,"POST","/api/v1/workspaces",csrf,{"name":"Persistent","slug":"persistent"}).status_code==201
    with TestClient(create_app(Settings(db_path=path))) as second:
        csrf=login(second)
        assert len(second.get("/api/v1/workspaces").json())==1
    with open_db(path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 3
