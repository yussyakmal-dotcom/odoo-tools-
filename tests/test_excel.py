"""End-to-end Excel editing tests against a small SQLite snapshot; no Odoo network."""
from io import BytesIO
import json
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient

from odoo_tools.cli import bootstrap_admin
from odoo_tools.config import Settings
from odoo_tools.db import open_db, transaction
from odoo_tools.excel_local import COLUMNS, parse_workbook
from odoo_tools.main import create_app


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PASSWORD = "AdministratorPassword123!"


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "local-excel.sqlite3")
    bootstrap_admin("administrator", PASSWORD, path)
    with TestClient(create_app(Settings(db_path=path))) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "administrator", "password": PASSWORD,
        })
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        workspace = client.post("/api/v1/workspaces", json={
            "name": "Bakery", "slug": "bakery",
        }, headers={"X-CSRF-Token": csrf})
        wid = workspace.json()["id"]
        with transaction(path) as db:
            uid = db.execute("SELECT id FROM users WHERE is_admin=1").fetchone()["id"]
            run = db.execute(
                "INSERT INTO sync_runs "
                "(workspace_id,requested_by,connection_updated_at,state,created_at) "
                "VALUES(?,?,?,'completed','2026-11-09T00:00:00+00:00')",
                (wid, uid, "2026-11-09T00:00:00+00:00"),
            ).lastrowid
            snap = db.execute(
                "INSERT INTO sync_snapshots(workspace_id,run_id,state,created_at,activated_at) "
                "VALUES(?,?,'active','2026-11-09T00:00:00+00:00','2026-11-09T00:00:00+00:00')",
                (wid, run),
            ).lastrowid
            base = {
                "mrp.bom": [
                    {"id": 10, "display_name": "BOM Bread", "product_tmpl_id": [2, "Bread"]},
                ],
                "mrp.bom.line": [
                    {"id": 100, "bom_id": [10,"BOM Bread"], "product_id": [9,"Flour"],
                     "product_uom_id": [1,"kg"], "product_qty": 2.0},
                    {"id": 101, "bom_id": [10,"BOM Bread"], "product_id": [9,"Flour"],
                     "product_uom_id": [1,"kg"], "product_qty": 3.0},
                    {"id": 102, "bom_id": [10,"BOM Bread"], "product_id": [11,"Sugar"],
                     "product_uom_id": [1,"kg"], "product_qty": 4.0},
                ],
                "product.product": [
                    {"id": 9, "default_code": "FLOUR", "display_name": "Flour",
                     "active": True, "uom_id": [1,"kg"]},
                    {"id": 11, "default_code": "SUGAR", "display_name": "Sugar",
                     "active": True, "uom_id": [1,"kg"]},
                ],
                "product.template": [
                    {"id": 2, "name": "Bread"},
                ],
                "uom.uom": [
                    {"id": 1, "name": "kg", "category_id": [1,"Mass"]},
                ],
            }
            for model, records in base.items():
                for row in records:
                    many = lambda name: row[name][0] if isinstance(row.get(name),list) else None
                    db.execute(
                        "INSERT INTO sync_records "
                        "(snapshot_id,model,odoo_id,bom_id,product_id,product_tmpl_id,product_uom_id,raw_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (snap,model,row["id"],many("bom_id"),many("product_id"),
                         many("product_tmpl_id"),many("product_uom_id"),json.dumps(row)),
                    )
        yield client, csrf, wid, path, snap


def endpoint(wid):
    return f"/api/v1/workspaces/{wid}/excel"


def export(client,wid):
    response = client.get(endpoint(wid)+"/export")
    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK"
    with ZipFile(BytesIO(response.content)) as z:
        assert "xl/worksheets/sheet3.xml" in z.namelist()
    return response.content


def edit_excel(original, changes=None, drop_rows=None, formula=None):
    changes = changes or {}
    drop_rows = drop_rows or []
    source = ZipFile(BytesIO(original))
    buffer = BytesIO()
    with source as z, ZipFile(buffer,"w",ZIP_DEFLATED) as destination:
        for name in z.namelist():
            data = z.read(name)
            if name == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                sheet_data = root.find(NS+"sheetData")
                for row in list(sheet_data):
                    if int(row.get("r")) in drop_rows:
                        sheet_data.remove(row)
                        continue
                    for cell in row.findall(NS+"c"):
                        ref = cell.get("r")
                        if ref in changes:
                            value = cell.find(NS+"v")
                            if value is None:
                                value = ET.SubElement(cell,NS+"v")
                            value.text = str(changes[ref])
                            cell.attrib.pop("t",None)
                            inline = cell.find(NS+"is")
                            if inline is not None:
                                cell.remove(inline)
                        if ref == formula:
                            ET.SubElement(cell,NS+"f").text = "1+1"
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            destination.writestr(name,data)
    return buffer.getvalue()


def upload(client,csrf,wid,data):
    return client.post(endpoint(wid)+"/preview",
                       headers={"X-CSRF-Token": csrf},
                       files={"file": ("BOM_Local.xlsx",data,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def apply(client,csrf,wid,token):
    return client.post(endpoint(wid)+f"/preview/{token}/apply",
                       headers={"X-CSRF-Token":csrf})


def test_round_trip_component_only_qty_only_both_and_original_immutable(setup):
    client,csrf,wid,path,snap=setup
    xlsx=export(client,wid)
    parsed=parse_workbook(xlsx)
    assert len(parsed)==3
    assert tuple(parsed[0][1]) == COLUMNS
    # 100: product only; 101: qty only; 102: both.
    edited=edit_excel(xlsx,{"F2":11,"H3":5,"F4":9,"H4":7})
    response=upload(client,csrf,wid,edited)
    assert response.status_code==200, response.text
    preview=response.json()
    assert preview["changed_count"]==3
    assert preview["unchanged_count"]==0
    assert preview["state"]=="ready"
    assert preview["warning_count"]==0
    result=apply(client,csrf,wid,preview["preview_token"])
    assert result.status_code==200, result.text
    assert result.json()["changed_count"]==3
    assert apply(client,csrf,wid,preview["preview_token"]).status_code==409
    with open_db(path) as db:
        source=db.execute(
            "SELECT product_id,raw_json FROM sync_records "
            "WHERE snapshot_id=? AND model='mrp.bom.line' AND odoo_id=100", (snap,)
        ).fetchone()
        assert source["product_id"]==9
        assert json.loads(source["raw_json"])["product_qty"]==2
        working={r["bom_line_id"]:(r["component_product_id"],r["qty"])
                 for r in db.execute("SELECT * FROM local_bom_working WHERE snapshot_id=?",(snap,))}
        assert working=={100:(11,"2"),101:(9,"5"),102:(9,"7")}
        assert db.execute("SELECT COUNT(*) FROM local_excel_change_rows").fetchone()[0]==3
    refreshed=parse_workbook(export(client,wid))
    assert refreshed[0][1]["original_component_product_id"]=="11"
    assert refreshed[1][1]["original_qty"]=="5"
    assert client.get(endpoint(wid)+"/working-summary").json()["working_overrides"]==3


def test_no_changes_and_validation_rejects_modified_identity_missing_rows(setup):
    client,csrf,wid,path,snap=setup
    xlsx=export(client,wid)
    normal=upload(client,csrf,wid,xlsx)
    assert normal.status_code==200
    assert normal.json()["state"]=="no_changes"
    assert normal.json()["preview_token"] is None
    assert upload(client,csrf,wid,edit_excel(xlsx,{"D2":999})).status_code==422
    assert upload(client,csrf,wid,edit_excel(xlsx,drop_rows=[4])).status_code==422
    assert upload(client,csrf,wid,edit_excel(xlsx,{"F2":9999})).status_code==422
    assert upload(client,csrf,wid,edit_excel(xlsx,{"H2":-1})).status_code==422
    assert upload(client,csrf,wid,edit_excel(xlsx,formula="H2")).status_code==422
    assert client.get(endpoint(wid)+"/working-summary").json()["working_overrides"]==0


def test_two_previews_cannot_overwrite_stale_local_edits(setup):
    client,csrf,wid,path,snap=setup
    xlsx=export(client,wid)
    edited=edit_excel(xlsx,{"H2":8})
    first=upload(client,csrf,wid,edited).json()["preview_token"]
    second=upload(client,csrf,wid,edited).json()["preview_token"]
    assert apply(client,csrf,wid,first).status_code==200
    assert apply(client,csrf,wid,second).status_code==409
    with open_db(path) as db:
        assert db.execute("SELECT COUNT(*) FROM local_excel_apply_batches").fetchone()[0]==1


def test_csrf_and_viewer_restriction(setup):
    client,csrf,wid,path,snap=setup
    xlsx=export(client,wid)
    missing_csrf=client.post(endpoint(wid)+"/preview", files={"file":("file.xlsx",xlsx)})
    assert missing_csrf.status_code==403
    user=client.post("/api/v1/admin/users",headers={"X-CSRF-Token":csrf},json={
        "username":"viewer1","display_name":"Viewer","password":"InitialViewerPassword123!",
        "is_admin":False,
    })
    assert user.status_code==201
    uid=user.json()["id"]
    assert client.put(f"/api/v1/workspaces/{wid}/members/{uid}",
                      headers={"X-CSRF-Token":csrf},json={"role":"viewer"}).status_code==200
    with TestClient(client.app) as viewer:
        token=viewer.post("/api/v1/auth/login",json={
            "username":"viewer1","password":"InitialViewerPassword123!",
        }).json()["csrf_token"]
        changed=viewer.post("/api/v1/auth/change-password",
                            headers={"X-CSRF-Token":token},json={
                                "current_password":"InitialViewerPassword123!",
                                "new_password":"ActualViewerPassword123!",
                            })
        assert changed.status_code==200
        token=viewer.post("/api/v1/auth/login",json={
            "username":"viewer1","password":"ActualViewerPassword123!",
        }).json()["csrf_token"]
        assert viewer.get(endpoint(wid)+"/export").status_code==200
        assert upload(viewer,token,wid,xlsx).status_code==403


def test_excel_export_name_with_formula_prefix_remains_text(setup):
    client,csrf,wid,path,snap=setup
    with transaction(path) as db:
        raw=db.execute(
            "SELECT raw_json FROM sync_records WHERE snapshot_id=? AND model='product.product' AND odoo_id=9",
            (snap,),
        ).fetchone()[0]
        product=json.loads(raw)
        product["display_name"]="=HYPERLINK(\"https://evil.example\",\"click\")"
        db.execute("UPDATE sync_records SET raw_json=? "
                   "WHERE snapshot_id=? AND model='product.product' AND odoo_id=9",
                   (json.dumps(product),snap))
    xlsx=export(client,wid)
    rows=parse_workbook(xlsx)
    assert rows[0][1]["component_name"].startswith("=HYPERLINK")
    with ZipFile(BytesIO(xlsx)) as z:
        sheet=z.read("xl/worksheets/sheet1.xml")
        assert b"<f>" not in sheet


def test_zero_quantity_source_exports_and_is_editable_locally(setup):
    """An Odoo BOM line with qty=0 must never block a whole workbook export."""
    client, csrf, wid, path, snap = setup
    with transaction(path) as db:
        raw = db.execute(
            "SELECT raw_json FROM sync_records WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100", (snap,),
        ).fetchone()[0]
        line = json.loads(raw)
        line["product_qty"] = 0.0
        db.execute(
            "UPDATE sync_records SET raw_json=? WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100",
            (json.dumps(line), snap),
        )
    original_excel = export(client, wid)
    rows = parse_workbook(original_excel)
    assert rows[0][1]["original_qty"] == "0"
    assert rows[0][1]["new_qty"] == "0"
    unchanged = upload(client, csrf, wid, original_excel)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["state"] == "no_changes"
    # Component-only changes may retain source qty=0.
    edited = edit_excel(original_excel, {"F2": 11})
    preview = upload(client, csrf, wid, edited)
    assert preview.status_code == 200, preview.text
    assert preview.json()["changed_count"] == 1
    applied = apply(client, csrf, wid, preview.json()["preview_token"])
    assert applied.status_code == 200, applied.text
    with open_db(path) as db:
        working = db.execute(
            "SELECT component_product_id,qty FROM local_bom_working "
            "WHERE snapshot_id=? AND bom_line_id=100", (snap,),
        ).fetchone()
        assert (working["component_product_id"], working["qty"]) == (11, "0")
        source = json.loads(db.execute(
            "SELECT raw_json FROM sync_records WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100", (snap,),
        ).fetchone()[0])
        assert source["product_qty"] == 0
    # Re-export from local working version, changing zero to a positive qty.
    revised = export(client, wid)
    changed_qty = upload(client, csrf, wid, edit_excel(revised, {"H2": 2.5}))
    assert changed_qty.status_code == 200, changed_qty.text
    assert apply(client, csrf, wid, changed_qty.json()["preview_token"]).status_code == 200


def test_high_precision_odoo_quantity_remains_exact_through_excel_round_trip(setup):
    """Original values with >12 fractional digits must not be rounded on export."""
    client, csrf, wid, path, snap = setup
    quantity = 0.12345678901234566
    with transaction(path) as db:
        raw = db.execute(
            "SELECT raw_json FROM sync_records WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100", (snap,),
        ).fetchone()[0]
        line = json.loads(raw)
        line["product_qty"] = quantity
        db.execute(
            "UPDATE sync_records SET raw_json=? WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100",
            (json.dumps(line), snap),
        )
    raw_excel = export(client, wid)
    parsed = parse_workbook(raw_excel)
    assert parsed[0][1]["original_qty"] == str(quantity)
    assert parsed[0][1]["new_qty"] == str(quantity)
    assert upload(client, csrf, wid, raw_excel).json()["state"] == "no_changes"
    # Component change alone is valid; source high-precision qty remains unchanged.
    modified = edit_excel(raw_excel, {"F2": 11})
    preview = upload(client, csrf, wid, modified)
    assert preview.status_code == 200, preview.text
    assert apply(client, csrf, wid, preview.json()["preview_token"]).status_code == 200
    with open_db(path) as db:
        qty = db.execute(
            "SELECT qty FROM local_bom_working WHERE snapshot_id=? AND bom_line_id=100",
            (snap,),
        ).fetchone()[0]
        assert qty == str(quantity)


def test_negative_new_qty_is_rejected_but_zero_new_qty_is_allowed_with_warning(setup):
    client, csrf, wid, path, snap = setup
    xlsx = export(client, wid)
    invalid = upload(client, csrf, wid, edit_excel(xlsx, {"H2": -5}))
    assert invalid.status_code == 422
    zero = upload(client, csrf, wid, edit_excel(xlsx, {"H2": 0}))
    assert zero.status_code == 200, zero.text
    assert zero.json()["changed_count"] == 1
    assert zero.json()["warning_count"] == 1
    assert apply(client, csrf, wid, zero.json()["preview_token"]).status_code == 200


def test_missing_source_qty_reports_specific_reason_without_mutating_snapshot(setup):
    client, csrf, wid, path, snap = setup
    with transaction(path) as db:
        line = json.loads(db.execute(
            "SELECT raw_json FROM sync_records WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100", (snap,),
        ).fetchone()[0])
        line["product_qty"] = None
        db.execute(
            "UPDATE sync_records SET raw_json=? WHERE snapshot_id=? "
            "AND model='mrp.bom.line' AND odoo_id=100",
            (json.dumps(line), snap),
        )
    response = client.get(endpoint(wid)+"/export")
    assert response.status_code == 409
    assert "BOM line 100" in response.text
    assert "missing" in response.text
