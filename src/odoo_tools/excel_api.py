"""FastAPI endpoints for Excel editing of local BOM working data only."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from .db import open_db
from .excel_local import (
    MAX_XLSX_BYTES, active_snapshot, apply_preview, export_workbook,
    load_lines, parse_workbook, save_preview, validate_import,
)


def router_for(workspace_access, require_ready, csrf_guard, db_path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces/{wid}/excel", tags=["Local Excel BOM editing"])

    def require_local_editor(wid: int, request: Request, user: dict) -> None:
        workspace_access(request, wid, user)
        if user["is_admin"]:
            return
        with open_db(db_path(request)) as db:
            row = db.execute(
                "SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
                (wid, user["id"]),
            ).fetchone()
            if not row or row["role"] not in ("editor", "workspace_admin"):
                raise HTTPException(403, "Local BOM edit permission required")

    @router.get("/export")
    def export_excel(wid: int, request: Request, user: dict = Depends(require_ready)) -> Response:
        workspace_access(request, wid, user)
        with open_db(db_path(request)) as db:
            snapshot_id = active_snapshot(db, wid)
            lines = load_lines(db, wid, snapshot_id)
            catalog = []
            for row in db.execute(
                "SELECT odoo_id,raw_json FROM sync_records WHERE snapshot_id=? "
                "AND model='product.product' ORDER BY odoo_id", (snapshot_id,),
            ):
                product = json.loads(row["raw_json"])
                default_uom = product.get("uom_id")
                if isinstance(default_uom, list):
                    default_uom = default_uom[0] if default_uom else None
                catalog.append([
                    row["odoo_id"], str(product.get("default_code") or ""),
                    str(product.get("display_name") or row["odoo_id"]),
                    default_uom or "", "Yes" if product.get("active", True) else "No",
                ])
            uom_catalog = []
            for row in db.execute(
                "SELECT odoo_id,raw_json FROM sync_records WHERE snapshot_id=? "
                "AND model='uom.uom' ORDER BY odoo_id", (snapshot_id,),
            ):
                uom = json.loads(row["raw_json"])
                category = uom.get("category_id")
                category_label = (category[1] if isinstance(category, list) and
                                  len(category) > 1 else str(category or ""))
                uom_catalog.append([
                    row["odoo_id"], str(uom.get("name") or ""), category_label,
                    uom.get("rounding") if uom.get("rounding") is not None else "",
                ])
        data = export_workbook(wid, snapshot_id, lines, catalog, uom_catalog)
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="BOM_Local_Workspace_{wid}_Snapshot_{snapshot_id}.xlsx"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/preview", dependencies=[Depends(csrf_guard)])
    async def preview_excel(wid: int, request: Request,
                            file: UploadFile = File(...),
                            user: dict = Depends(require_ready)) -> dict:
        require_local_editor(wid, request, user)
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            raise HTTPException(422, "Upload a .xlsx file exported from BOM Tools")
        content = await file.read(MAX_XLSX_BYTES + 1)
        await file.close()
        try:
            parsed = parse_workbook(content)
        except ValueError as exc:
            raise HTTPException(422, {"message": str(exc)}) from exc
        # Compare against current local working values. No Odoo requests are used.
        with open_db(db_path(request)) as db:
            result = validate_import(db, wid, parsed)
        preview_token = save_preview(db_path(request), wid, user["id"], result)
        return {
            "workspace_id": wid, "snapshot_id": result["snapshot_id"],
            "total_lines": result["total_lines"], "changed_count": result["changed_count"],
            "unchanged_count": result["unchanged_count"],
            "warning_count": len(result["warnings"]),
            "warnings": result["warnings"][:50],
            "changes_sample": result["changes"][:100],
            "preview_token": preview_token,
            "state": "ready" if preview_token else "no_changes",
            "note": "No Odoo data has been changed. Apply to Local is a separate action.",
        }

    @router.post("/preview/{preview_token}/apply", dependencies=[Depends(csrf_guard)])
    def apply_excel(wid: int, preview_token: str, request: Request,
                    user: dict = Depends(require_ready)) -> dict:
        require_local_editor(wid, request, user)
        if len(preview_token) > 128:
            raise HTTPException(404, "Preview not found")
        result = apply_preview(db_path(request), wid, user["id"], preview_token)
        result["note"] = "Applied only to SQLite local working BOM; Odoo and original snapshot unchanged"
        return result

    @router.get("/working-summary")
    def working_summary(wid: int, request: Request, user: dict = Depends(require_ready)) -> dict:
        workspace_access(request, wid, user)
        with open_db(db_path(request)) as db:
            snapshot_id = active_snapshot(db, wid)
            overrides = db.execute(
                "SELECT COUNT(*) FROM local_bom_working WHERE workspace_id=? AND snapshot_id=?",
                (wid, snapshot_id),
            ).fetchone()[0]
            recent = db.execute(
                "SELECT id, applied_at, changed_count FROM local_excel_apply_batches "
                "WHERE workspace_id=? AND snapshot_id=? ORDER BY id DESC LIMIT 20",
                (wid, snapshot_id),
            ).fetchall()
        return {"workspace_id": wid, "snapshot_id": snapshot_id,
                "working_overrides": overrides, "recent_applies": [dict(r) for r in recent],
                "original_snapshot_unchanged": True,
                "odoo_write_enabled": False}

    return router
