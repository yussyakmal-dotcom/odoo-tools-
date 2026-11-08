"""Read-only Odoo 19 BOM full snapshot sync; isolated per workspace.

One worker processes queued jobs. The previous active snapshot is never mutated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from .db import open_db, transaction
from .odoo_connection import OdooProbeError, decrypt_key, json2_call
from .security import timestamp

LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 250

# Explicitly whitelisted fields to avoid unexpectedly downloading sensitive product fields.
# fields_get limits this list to fields actually supported by the Odoo instance.
MODEL_FIELDS: dict[str, tuple[str, ...]] = {
    "mrp.bom": (
        "id", "code", "display_name", "product_tmpl_id", "product_id",
        "product_qty", "product_uom_id", "type", "company_id", "active", "write_date",
        "bom_line_ids", "operation_ids", "byproduct_ids", "consumption",
        "ready_to_produce", "picking_type_id", "sequence",
    ),
    "mrp.bom.line": (
        "id", "bom_id", "product_id", "product_qty", "product_uom_id",
        "sequence", "operation_id", "company_id", "write_date",
        "bom_product_template_attribute_value_ids", "manual_consumption",
    ),
    "product.product": (
        "id", "product_tmpl_id", "default_code", "display_name",
        "active", "uom_id", "company_id", "write_date",
    ),
    "product.template": (
        "id", "name", "default_code", "active", "uom_id",
        "company_id", "write_date",
    ),
    "uom.uom": ("id", "name", "category_id", "factor", "rounding", "active", "write_date"),
    "res.company": ("id", "name", "write_date"),
    "mrp.bom.byproduct": (
        "id", "bom_id", "product_id", "product_qty", "product_uom_id",
        "operation_id", "write_date",
    ),
    "mrp.routing.workcenter": (
        "id", "bom_id", "name", "workcenter_id", "time_cycle_manual",
        "sequence", "write_date",
    ),
}
CORE_MODELS = ("mrp.bom", "mrp.bom.line", "product.product", "product.template", "uom.uom")
OPTIONAL_MODELS = ("res.company", "mrp.bom.byproduct", "mrp.routing.workcenter")


def initialize_sync_schema(path: str) -> None:
    with open_db(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                requested_by INTEGER NOT NULL REFERENCES users(id),
                connection_updated_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('queued','running','completed','incomplete','failed')),
                current_model TEXT,
                downloaded INTEGER NOT NULL DEFAULT 0,
                estimated_total INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS sync_one_active_job
                ON sync_runs(workspace_id) WHERE state IN ('queued','running');
            CREATE INDEX IF NOT EXISTS idx_sync_runs_workspace
                ON sync_runs(workspace_id,id DESC);
            CREATE TABLE IF NOT EXISTS sync_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                run_id INTEGER NOT NULL UNIQUE REFERENCES sync_runs(id),
                state TEXT NOT NULL CHECK(state IN ('staging','active','retired','invalid')),
                created_at TEXT NOT NULL,
                activated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS sync_one_active_snapshot
                ON sync_snapshots(workspace_id) WHERE state='active';
            CREATE TABLE IF NOT EXISTS sync_model_progress (
                run_id INTEGER NOT NULL REFERENCES sync_runs(id),
                model TEXT NOT NULL,
                expected_count INTEGER,
                downloaded INTEGER NOT NULL DEFAULT 0,
                verified_count INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT,
                PRIMARY KEY(run_id,model)
            );
            CREATE TABLE IF NOT EXISTS sync_records (
                snapshot_id INTEGER NOT NULL REFERENCES sync_snapshots(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                odoo_id INTEGER NOT NULL,
                bom_id INTEGER,
                product_id INTEGER,
                product_tmpl_id INTEGER,
                product_uom_id INTEGER,
                default_code TEXT,
                write_date TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY(snapshot_id,model,odoo_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_component
                ON sync_records(snapshot_id,model,product_id);
            CREATE INDEX IF NOT EXISTS idx_sync_bom
                ON sync_records(snapshot_id,model,bom_id);
        """)


def _m2o(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (tuple, list)) and value and isinstance(value[0], int):
        return value[0]
    return None


def _store_page(db: sqlite3.Connection, snapshot_id: int, model: str, rows: list[dict]) -> None:
    values = []
    for record in rows:
        record_id = record.get("id")
        if not isinstance(record_id, int) or isinstance(record_id, bool):
            raise SyncFailure("invalid_response")
        values.append((
            snapshot_id, model, record_id, _m2o(record.get("bom_id")),
            _m2o(record.get("product_id")), _m2o(record.get("product_tmpl_id")),
            _m2o(record.get("product_uom_id")),
            record.get("default_code") if isinstance(record.get("default_code"), str) else None,
            record.get("write_date") if isinstance(record.get("write_date"), str) else None,
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        ))
    db.executemany(
        "INSERT INTO sync_records(snapshot_id,model,odoo_id,bom_id,product_id,"
        "product_tmpl_id,product_uom_id,default_code,write_date,raw_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        values,
    )


@dataclass
class SyncFailure(Exception):
    code: str
    detail: str = ""

    def __str__(self) -> str:
        return self.code


def claim_next_run(path: str) -> int | None:
    """Atomically claim the oldest queued run for this single worker."""
    with transaction(path) as db:
        row = db.execute(
            "SELECT id FROM sync_runs WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cursor = db.execute(
            "UPDATE sync_runs SET state='running',started_at=?,message=? "
            "WHERE id=? AND state='queued'",
            (timestamp(), "Preparing snapshot", row["id"]),
        )
        return row["id"] if cursor.rowcount else None


def recover_interrupted_runs(path: str) -> None:
    """On worker restart, fail interrupted runs instead of silently hanging."""
    with transaction(path) as db:
        jobs = db.execute("SELECT id FROM sync_runs WHERE state='running'").fetchall()
        for job in jobs:
            db.execute(
                "UPDATE sync_runs SET state='failed',finished_at=?,error_code='worker_interrupted',"
                "message='Worker restarted before sync finished' WHERE id=?",
                (timestamp(), job["id"]),
            )
            db.execute("UPDATE sync_snapshots SET state='invalid' WHERE run_id=? AND state='staging'", (job["id"],))


def _update_progress(path: str, run_id: int, model: str, count: int, note: str = "") -> None:
    with transaction(path) as db:
        db.execute(
            "UPDATE sync_model_progress SET downloaded=?,status='running',note=? "
            "WHERE run_id=? AND model=?", (count, note or None, run_id, model)
        )
        db.execute(
            "UPDATE sync_runs SET current_model=?,downloaded=("
            "SELECT COALESCE(SUM(downloaded),0) FROM sync_model_progress WHERE run_id=?),"
            "message=? WHERE id=?",
            (model, run_id, f"Downloading {model}: {count} records", run_id),
        )


async def _read(client: httpx.AsyncClient, url: str, dbname: str | None, token: str,
                model: str, method: str, params: dict) -> Any:
    try:
        return await json2_call(client, url, dbname, token, model, method, params)
    except OdooProbeError as exc:
        raise SyncFailure(exc.category, f"{model}/{method}") from exc


async def _count(client: httpx.AsyncClient, url: str, dbname: str | None, token: str,
                 model: str) -> int:
    total = await _read(client, url, dbname, token, model, "search_count",
                        {"domain": [], "context": {"active_test": False}})
    if not isinstance(total, int) or total < 0:
        raise SyncFailure("invalid_count", model)
    return total


async def _fields(client: httpx.AsyncClient, url: str, dbname: str | None, token: str,
                  model: str) -> list[str]:
    metadata = await _read(client, url, dbname, token, model, "fields_get",
                           {"attributes": ["type", "relation"]})
    if not isinstance(metadata, dict):
        raise SyncFailure("invalid_fields", model)
    fields = [field for field in MODEL_FIELDS[model] if field in metadata]
    required = ("id", "bom_id", "product_id") if model == "mrp.bom.line" else (
        ("id", "product_tmpl_id") if model == "mrp.bom" else ("id",)
    )
    if any(field not in fields for field in required):
        raise SyncFailure("missing_required_fields", model)
    return fields


async def _watermark(client: httpx.AsyncClient, url: str, dbname: str | None,
                     token: str, model: str, fields: list[str]) -> tuple:
    if "write_date" not in fields:
        return ()
    rows = await _read(
        client, url, dbname, token, model, "search_read",
        {"domain": [], "fields": ["id", "write_date"], "limit": 1,
         "order": "write_date desc,id desc", "context": {"active_test": False}},
    )
    if not isinstance(rows, list):
        raise SyncFailure("invalid_response", model)
    return tuple((rows[0].get("id"), rows[0].get("write_date"))) if rows else ()


async def _download_model(path: str, run_id: int, snapshot_id: int, client: httpx.AsyncClient,
                          url: str, dbname: str | None, token: str, model: str) -> None:
    fields = await _fields(client, url, dbname, token, model)
    before = await _count(client, url, dbname, token, model)
    watermark_before = await _watermark(client, url, dbname, token, model, fields)
    with transaction(path) as db:
        db.execute(
            "INSERT INTO sync_model_progress(run_id,model,expected_count,status) "
            "VALUES(?,?,?,'running')", (run_id, model, before)
        )
        db.execute(
            "UPDATE sync_runs SET estimated_total=estimated_total+? WHERE id=?", (before, run_id)
        )

    downloaded, cursor = 0, 0
    while True:
        rows = await _read(
            client, url, dbname, token, model, "search_read",
            {"domain": [["id", ">", cursor]], "fields": fields,
             "limit": PAGE_SIZE, "order": "id asc", "context": {"active_test": False}},
        )
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            raise SyncFailure("invalid_response", model)
        if not rows:
            break
        ids = [row.get("id") for row in rows]
        if any(not isinstance(i, int) or isinstance(i, bool) for i in ids) or ids != sorted(ids) or len(set(ids)) != len(ids) or ids[0] <= cursor:
            raise SyncFailure("invalid_page_order", model)
        with transaction(path) as db:
            _store_page(db, snapshot_id, model, rows)
        downloaded += len(rows)
        cursor = ids[-1]
        _update_progress(path, run_id, model, downloaded)
        if len(rows) < PAGE_SIZE:
            break

    after = await _count(client, url, dbname, token, model)
    watermark_after = await _watermark(client, url, dbname, token, model, fields)
    with transaction(path) as db:
        db.execute(
            "UPDATE sync_model_progress SET verified_count=?,downloaded=?,status=?,note=? "
            "WHERE run_id=? AND model=?",
            (after, downloaded, "completed" if (before == downloaded == after and watermark_before == watermark_after)
             else "incomplete",
             None if (before == downloaded == after and watermark_before == watermark_after)
             else "Count or update watermark changed during sync", run_id, model),
        )
    if before != downloaded or after != downloaded or watermark_before != watermark_after:
        raise SyncFailure("data_changed_during_sync", model)


def _verify_relations(db: sqlite3.Connection, snapshot_id: int) -> None:
    # Every BOM line must refer to a BOM downloaded in the same snapshot.
    missing_boms = db.execute(
        "SELECT COUNT(*) FROM sync_records line WHERE line.snapshot_id=? "
        "AND line.model='mrp.bom.line' AND NOT EXISTS ("
        "SELECT 1 FROM sync_records bom WHERE bom.snapshot_id=line.snapshot_id "
        "AND bom.model='mrp.bom' AND bom.odoo_id=line.bom_id)",
        (snapshot_id,),
    ).fetchone()[0]
    missing_components = db.execute(
        "SELECT COUNT(*) FROM sync_records line WHERE line.snapshot_id=? "
        "AND line.model='mrp.bom.line' AND NOT EXISTS ("
        "SELECT 1 FROM sync_records product WHERE product.snapshot_id=line.snapshot_id "
        "AND product.model='product.product' AND product.odoo_id=line.product_id)",
        (snapshot_id,),
    ).fetchone()[0]
    missing_templates = db.execute(
        "SELECT COUNT(*) FROM sync_records bom WHERE bom.snapshot_id=? AND bom.model='mrp.bom' "
        "AND bom.product_tmpl_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM sync_records template WHERE template.snapshot_id=bom.snapshot_id "
        "AND template.model='product.template' AND template.odoo_id=bom.product_tmpl_id)",
        (snapshot_id,),
    ).fetchone()[0]
    missing_uoms = db.execute(
        "SELECT COUNT(*) FROM sync_records line WHERE line.snapshot_id=? "
        "AND line.model='mrp.bom.line' AND line.product_uom_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM sync_records uom WHERE uom.snapshot_id=line.snapshot_id "
        "AND uom.model='uom.uom' AND uom.odoo_id=line.product_uom_id)",
        (snapshot_id,),
    ).fetchone()[0]
    if missing_boms or missing_components or missing_templates or missing_uoms:
        raise SyncFailure(
            "broken_relations",
            f"missing_boms={missing_boms}, missing_products={missing_components}, "
            f"missing_templates={missing_templates}, missing_uoms={missing_uoms}",
        )


async def execute_run(path: str, run_id: int, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Execute an already-claimed run. Never requests Odoo write methods."""
    snapshot_id = None
    try:
        with transaction(path) as db:
            job = db.execute("SELECT * FROM sync_runs WHERE id=? AND state='running'", (run_id,)).fetchone()
            if job is None:
                raise SyncFailure("run_not_claimed")
            conn = db.execute(
                "SELECT * FROM workspace_odoo_connections WHERE workspace_id=?",
                (job["workspace_id"],),
            ).fetchone()
            if conn is None or conn["updated_at"] != job["connection_updated_at"]:
                raise SyncFailure("connection_changed")
            token = decrypt_key(conn["encrypted_api_key"])
            snapshot_id = db.execute(
                "INSERT INTO sync_snapshots(workspace_id,run_id,state,created_at) VALUES(?,?,'staging',?)",
                (job["workspace_id"], run_id, timestamp()),
            ).lastrowid

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0), follow_redirects=False,
            trust_env=False, transport=transport,
        ) as client:
            for model in CORE_MODELS:
                await _download_model(path, run_id, snapshot_id, client,
                                      conn["base_url"], conn["database_name"], token, model)
            for model in OPTIONAL_MODELS:
                try:
                    await _download_model(path, run_id, snapshot_id, client,
                                          conn["base_url"], conn["database_name"], token, model)
                except SyncFailure as exc:
                    # Optional models may not exist or may not be accessible in Odoo Online.
                    with open_db(path) as db:
                        progress = db.execute(
                            "SELECT downloaded FROM sync_model_progress WHERE run_id=? AND model=?",
                            (run_id, model),
                        ).fetchone()
                    # Only skip when the optional model cannot be accessed at discovery.
                    # Failing after partial download is a real sync failure, never a skip.
                    if exc.code in ("permission", "not_found", "missing_required_fields") and progress is None:
                        with transaction(path) as db:
                            db.execute(
                                "INSERT INTO sync_model_progress(run_id,model,status,note) "
                                "VALUES(?,?,'skipped',?)",
                                (run_id, model, exc.code),
                            )
                    else:
                        raise
        with transaction(path) as db:
            _verify_relations(db, snapshot_id)
            # Connection changes invalidate the snapshot before publication.
            current = db.execute(
                "SELECT updated_at FROM workspace_odoo_connections WHERE workspace_id=?",
                (job["workspace_id"],),
            ).fetchone()
            if not current or current["updated_at"] != job["connection_updated_at"]:
                raise SyncFailure("connection_changed")
            db.execute(
                "UPDATE sync_snapshots SET state='retired' WHERE workspace_id=? AND state='active'",
                (job["workspace_id"],),
            )
            db.execute(
                "UPDATE sync_snapshots SET state='active',activated_at=? WHERE id=?",
                (timestamp(), snapshot_id),
            )
            db.execute(
                "UPDATE sync_runs SET state='completed',current_model=NULL,finished_at=?,"
                "message='Snapshot validated and activated' WHERE id=?",
                (timestamp(), run_id),
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, SyncFailure) else "unexpected_error"
        # Exceptions might contain secrets from third parties. Do not log or expose them.
        LOGGER.warning("Sync run %s failed: %s", run_id, code)
        with transaction(path) as db:
            db.execute(
                "UPDATE sync_runs SET state=?,error_code=?,message=?,finished_at=? WHERE id=?",
                ("incomplete" if code in ("data_changed_during_sync", "broken_relations") else "failed",
                 code, "Download not published; previous snapshot preserved", timestamp(), run_id),
            )
            if snapshot_id is not None:
                db.execute(
                    "UPDATE sync_snapshots SET state='invalid' WHERE id=? AND state='staging'",
                    (snapshot_id,),
                )
    finally:
        # Avoid accumulating potentially large invalid snapshot data.
        if snapshot_id is not None:
            with transaction(path) as db:
                state = db.execute("SELECT state FROM sync_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if state is not None and state["state"] == "invalid":
                    db.execute("DELETE FROM sync_records WHERE snapshot_id=?", (snapshot_id,))


def run_summary(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    models = [
        dict(model=r["model"], status=r["status"], expected_count=r["expected_count"],
             downloaded=r["downloaded"], verified_count=r["verified_count"], note=r["note"])
        for r in db.execute(
            "SELECT * FROM sync_model_progress WHERE run_id=? ORDER BY model", (row["id"],)
        )
    ]
    return {
        "id": row["id"], "workspace_id": row["workspace_id"], "state": row["state"],
        "current_model": row["current_model"], "downloaded": row["downloaded"],
        "estimated_total": row["estimated_total"], "error_code": row["error_code"],
        "message": row["message"], "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "models": models,
    }
