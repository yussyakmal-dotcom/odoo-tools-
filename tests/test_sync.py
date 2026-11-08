"""Mocked worker tests: never access a real Odoo and never write there."""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from odoo_tools.cli import bootstrap_admin
from odoo_tools.config import Settings
from odoo_tools.db import open_db
from odoo_tools.main import create_app
from odoo_tools.sync import claim_next_run, execute_run, recover_interrupted_runs, PAGE_SIZE


def sample_data(lines=1):
    datasets = {
        "mrp.bom": [{"id": 10, "product_tmpl_id": [2, "Bread"], "product_id": False,
                     "code": "BOM-10", "write_date": "2026-11-09 01:00:00"}],
        "mrp.bom.line": [
            {"id": 100 + i, "bom_id": [10, "BOM-10"], "product_id": [9, "Flour"],
             "product_qty": 1.0, "product_uom_id": [1, "kg"],
             "write_date": "2026-11-09 01:00:00"} for i in range(lines)
        ],
        "product.product": [{"id": 9, "product_tmpl_id": [3, "Flour"],
                             "default_code": "FLOUR", "write_date": "2026-11-09 01:00:00"}],
        "product.template": [{"id": 2, "name": "Bread", "write_date": "2026-11-09 01:00:00"},
                             {"id": 3, "name": "Flour", "write_date": "2026-11-09 01:00:00"}],
        "uom.uom": [{"id": 1, "name": "kg", "write_date": "2026-11-09 01:00:00"}],
        "res.company": [{"id": 1, "name": "Company"}],
        "mrp.bom.byproduct": [],
        "mrp.routing.workcenter": [],
    }
    return datasets


def mocked_odoo(datasets, *, fail_model=None, changed_count=None):
    calls = []

    def response(request):
        calls.append((request.url.path, json.loads(request.content)))
        assert request.method == "POST"
        assert request.headers.get("Authorization") == "bearer test-api-secret"
        pieces = request.url.path.split("/")
        model, method = pieces[-2], pieces[-1]
        if model == fail_model:
            return httpx.Response(503, json={"detail": "upstream unavailable"})
        rows = datasets[model]
        payload = json.loads(request.content)
        if method == "fields_get":
            fields = set().union(*(r.keys() for r in rows))
            fields |= {"id"}
            if model == "mrp.bom":
                fields |= {"product_tmpl_id"}
            if model == "mrp.bom.line":
                fields |= {"bom_id", "product_id"}
            return httpx.Response(200, json={key: {"type": "char"} for key in fields})
        if method == "search_count":
            count = len(rows)
            if changed_count and model in changed_count:
                count += changed_count[model]
            return httpx.Response(200, json=count)
        if method == "search_read":
            if payload.get("order", "").startswith("write_date"):
                values = sorted(rows, key=lambda r: (r.get("write_date", ""), r["id"]), reverse=True)[:1]
            else:
                greater_than = payload.get("domain", [["id", ">", 0]])[0][2]
                values = [r for r in rows if r["id"] > greater_than][:payload.get("limit", 250)]
            fields = payload["fields"]
            return httpx.Response(200, json=[{key: row.get(key, False) for key in fields} for row in values])
        raise AssertionError("Unexpected Odoo method: " + method)

    return httpx.MockTransport(response), calls


@pytest.fixture
def system(tmp_path, monkeypatch):
    path = str(tmp_path / "sync-test.sqlite3")
    monkeypatch.setenv("BOM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    bootstrap_admin("administrator", "InitialAdminPassword123!", path)
    with TestClient(create_app(Settings(db_path=path))) as c:
        csrf = c.post("/api/v1/auth/login", json={
            "username": "administrator", "password": "InitialAdminPassword123!"
        }).json()["csrf_token"]
        wids = []
        for slug in ("first", "second"):
            response = c.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf},
                              json={"name": slug, "slug": slug})
            assert response.status_code == 201
            wid = response.json()["id"]
            wids.append(wid)
            result = c.put(f"/api/v1/workspaces/{wid}/odoo/connection",
                           headers={"X-CSRF-Token": csrf},
                           json={"base_url": "https://fake.odoo.com",
                                 "api_key": "test-api-secret", "database": "fake"})
            assert result.status_code == 200, result.text
        yield c, csrf, wids, path


def enqueue(c, csrf, wid):
    return c.post(f"/api/v1/workspaces/{wid}/sync/runs", headers={"X-CSRF-Token": csrf})


def test_full_snapshot_download_and_isolation(system):
    c, csrf, (wid, second), path = system
    job = enqueue(c, csrf, wid)
    assert job.status_code == 202, job.text
    run_id = job.json()["run_id"]
    assert enqueue(c, csrf, wid).status_code == 409
    assert claim_next_run(path) == run_id
    transport, calls = mocked_odoo(sample_data())
    asyncio.run(execute_run(path, run_id, transport=transport))
    run = c.get(f"/api/v1/workspaces/{wid}/sync/runs/{run_id}").json()
    assert run["state"] == "completed", run
    assert run["downloaded"] == 7  # 1 BOM + 1 line + 1 product + 2 templates + 1 UOM + 1 company
    assert all("/write" not in path and "/create" not in path and "/unlink" not in path
               for path, _ in calls)
    current = c.get(f"/api/v1/workspaces/{wid}/sync/snapshots/current").json()
    assert current["available"] and current["total_records"] == 7
    assert c.get(f"/api/v1/workspaces/{second}/sync/snapshots/current").json()["available"] is False
    with open_db(path) as db:
        line = db.execute("SELECT bom_id,product_id FROM sync_records WHERE snapshot_id=? "
                          "AND model='mrp.bom.line'", (current["snapshot_id"],)).fetchone()
        assert (line["bom_id"], line["product_id"]) == (10, 9)


def test_pagination_more_than_one_batch(system):
    c, csrf, (wid, _), path = system
    run_id = enqueue(c, csrf, wid).json()["run_id"]
    assert claim_next_run(path) == run_id
    data = sample_data(lines=PAGE_SIZE + 5)
    transport, calls = mocked_odoo(data)
    asyncio.run(execute_run(path, run_id, transport=transport))
    summary = c.get(f"/api/v1/workspaces/{wid}/sync/runs/{run_id}").json()
    assert summary["state"] == "completed", summary
    line_progress = next(item for item in summary["models"] if item["model"] == "mrp.bom.line")
    assert line_progress["downloaded"] == PAGE_SIZE + 5
    assert len([method for path, method in calls if path.endswith("/mrp.bom.line/search_read")
                and method.get("order") == "id asc"]) == 2


def test_failed_download_preserves_previous_snapshot(system):
    c, csrf, (wid, _), path = system
    run_id = enqueue(c, csrf, wid).json()["run_id"]
    claim_next_run(path)
    transport, _ = mocked_odoo(sample_data())
    asyncio.run(execute_run(path, run_id, transport=transport))
    old = c.get(f"/api/v1/workspaces/{wid}/sync/snapshots/current").json()
    bad_id = enqueue(c, csrf, wid).json()["run_id"]
    claim_next_run(path)
    bad_transport, _ = mocked_odoo(sample_data(), fail_model="mrp.bom.line")
    asyncio.run(execute_run(path, bad_id, transport=bad_transport))
    bad_run = c.get(f"/api/v1/workspaces/{wid}/sync/runs/{bad_id}").json()
    assert bad_run["state"] == "failed"
    current = c.get(f"/api/v1/workspaces/{wid}/sync/snapshots/current").json()
    assert current["snapshot_id"] == old["snapshot_id"]
    assert current["total_records"] == old["total_records"]
    with open_db(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sync_records WHERE snapshot_id NOT IN "
                          "(SELECT id FROM sync_snapshots WHERE state IN ('active','retired'))").fetchone()[0] == 0


def test_server_count_drift_marks_incomplete(system):
    c, csrf, (wid, _), path = system
    run_id = enqueue(c, csrf, wid).json()["run_id"]
    claim_next_run(path)
    transport, _ = mocked_odoo(sample_data(), changed_count={"mrp.bom": 1})
    asyncio.run(execute_run(path, run_id, transport=transport))
    run = c.get(f"/api/v1/workspaces/{wid}/sync/runs/{run_id}").json()
    assert run["state"] == "incomplete"
    assert c.get(f"/api/v1/workspaces/{wid}/sync/snapshots/current").json()["available"] is False


def test_worker_restart_marks_interrupted_job(system):
    c, csrf, (wid, _), path = system
    run_id = enqueue(c, csrf, wid).json()["run_id"]
    assert claim_next_run(path) == run_id
    recover_interrupted_runs(path)
    assert c.get(f"/api/v1/workspaces/{wid}/sync/runs/{run_id}").json()["error_code"] == "worker_interrupted"
    assert enqueue(c, csrf, wid).status_code == 202


def test_viewer_cannot_trigger_sync(system):
    c, csrf, (wid, _), path = system
    new = c.post("/api/v1/admin/users", headers={"X-CSRF-Token": csrf},
                 json={"username": "viewer1", "display_name": "Viewer",
                       "password": "NewTempPassword123!", "is_admin": False})
    assert new.status_code == 201
    uid = new.json()["id"]
    assert c.put(f"/api/v1/workspaces/{wid}/members/{uid}",
                 headers={"X-CSRF-Token": csrf}, json={"role": "viewer"}).status_code == 200

    with TestClient(c.app) as viewer:
        token = viewer.post("/api/v1/auth/login", json={
            "username": "viewer1", "password": "NewTempPassword123!"
        }).json()["csrf_token"]
        assert viewer.post("/api/v1/auth/change-password", headers={"X-CSRF-Token": token},
                           json={"current_password": "NewTempPassword123!",
                                 "new_password": "NewLongPassword123!"}).status_code == 200
        token = viewer.post("/api/v1/auth/login", json={
            "username": "viewer1", "password": "NewLongPassword123!"
        }).json()["csrf_token"]
        assert viewer.post(f"/api/v1/workspaces/{wid}/sync/runs",
                           headers={"X-CSRF-Token": token}).status_code == 403
        assert viewer.get(f"/api/v1/workspaces/{wid}/sync/runs").status_code == 200
        assert viewer.get(f"/api/v1/workspaces/{wid}/sync/snapshots/current").status_code == 200
