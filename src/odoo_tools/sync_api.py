"""Authenticated, workspace-scoped sync queue and monitoring endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .db import open_db, transaction
from .security import audit, timestamp
from .sync import run_summary


def router_for(workspace_access, require_ready, csrf_guard, db_path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces/{wid}/sync", tags=["BOM synchronization"])

    @router.post("/runs", status_code=202, dependencies=[Depends(csrf_guard)])
    def enqueue_sync(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        # The Administrator or Workspace Admin may start a full download.
        workspace_access(request, wid, user, manage=True)
        with transaction(db_path(request)) as db:
            connection = db.execute(
                "SELECT updated_at FROM workspace_odoo_connections WHERE workspace_id=?", (wid,)
            ).fetchone()
            if not connection:
                raise HTTPException(409, "Configure the Odoo connection before starting sync")
            running = db.execute(
                "SELECT id FROM sync_runs WHERE workspace_id=? AND state IN ('queued','running')",
                (wid,),
            ).fetchone()
            if running:
                raise HTTPException(409, "A synchronization is already queued or running for this workspace")
            try:
                cur = db.execute(
                    "INSERT INTO sync_runs(workspace_id,requested_by,connection_updated_at,state,created_at,message) "
                    "VALUES(?,?,?,'queued',?,?)",
                    (wid, user["id"], connection["updated_at"], timestamp(), "Waiting for worker"),
                )
            except Exception as exc:
                # Unique active sync constraint ensures no two jobs overlap.
                if "UNIQUE constraint failed" in str(exc):
                    raise HTTPException(409, "A sync is already pending") from None
                raise
            audit(db, user["id"], "bom.sync.queued", str(cur.lastrowid), wid)
            return {"run_id": cur.lastrowid, "state": "queued"}

    @router.get("/runs")
    def list_runs(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user)
        with open_db(db_path(request)) as db:
            rows = db.execute(
                "SELECT * FROM sync_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 20",
                (wid,),
            ).fetchall()
            return {"runs": [run_summary(db, r) for r in rows]}

    @router.get("/runs/{run_id}")
    def run_status(wid: int, run_id: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user)
        with open_db(db_path(request)) as db:
            row = db.execute(
                "SELECT * FROM sync_runs WHERE id=? AND workspace_id=?", (run_id, wid)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Sync run not found")
            return run_summary(db, row)

    @router.get("/snapshots/current")
    def active_snapshot(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user)
        with open_db(db_path(request)) as db:
            row = db.execute(
                "SELECT s.id,s.activated_at,s.run_id FROM sync_snapshots s "
                "WHERE s.workspace_id=? AND s.state='active'", (wid,),
            ).fetchone()
            if row is None:
                return {"available": False, "snapshot_id": None, "models": []}
            models = db.execute(
                "SELECT model, COUNT(*) AS count FROM sync_records WHERE snapshot_id=? "
                "GROUP BY model ORDER BY model", (row["id"],),
            ).fetchall()
            return {"available": True, "snapshot_id": row["id"],
                    "run_id": row["run_id"], "activated_at": row["activated_at"],
                    "models": [dict(m) for m in models],
                    "total_records": sum(m["count"] for m in models)}

    return router
