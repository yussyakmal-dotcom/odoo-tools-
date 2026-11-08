"""Excel (.xlsx) export and import using the Python standard-library OOXML reader.

Only local SQLite working overlays are updated. No Odoo client is imported here.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import re
import secrets
import sqlite3
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from fastapi import HTTPException

from .db import open_db, transaction
from .security import now, timestamp, audit

MAX_XLSX_BYTES = 12 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 120 * 1024 * 1024
MAX_LINES = 100_000
PREVIEW_TTL_MINUTES = 60
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
TAG = lambda tag: "{" + NS + "}" + tag

COLUMNS = (
    "workspace_id", "snapshot_id", "bom_id", "bom_line_id",
    "original_component_product_id", "new_component_product_id",
    "original_qty", "new_qty", "uom_id", "bom_name",
    "component_name", "sku",
)
EDITABLE = {"new_component_product_id", "new_qty"}


def initialize_excel_schema(path: str) -> None:
    with open_db(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS local_bom_working (
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                snapshot_id INTEGER NOT NULL REFERENCES sync_snapshots(id),
                bom_line_id INTEGER NOT NULL,
                component_product_id INTEGER NOT NULL,
                qty TEXT NOT NULL,
                updated_by INTEGER NOT NULL REFERENCES users(id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, snapshot_id, bom_line_id)
            );
            CREATE TABLE IF NOT EXISTS local_excel_previews (
                token_hash TEXT PRIMARY KEY,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                snapshot_id INTEGER NOT NULL REFERENCES sync_snapshots(id),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','applied')),
                changes_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                changed_count INTEGER NOT NULL,
                applied_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_excel_previews_ws
                ON local_excel_previews(workspace_id,created_at);
            CREATE TABLE IF NOT EXISTS local_excel_apply_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                snapshot_id INTEGER NOT NULL REFERENCES sync_snapshots(id),
                applied_by INTEGER NOT NULL REFERENCES users(id),
                applied_at TEXT NOT NULL,
                changed_count INTEGER NOT NULL,
                preview_hash TEXT NOT NULL UNIQUE REFERENCES local_excel_previews(token_hash)
            );
            CREATE TABLE IF NOT EXISTS local_excel_change_rows (
                batch_id INTEGER NOT NULL REFERENCES local_excel_apply_batches(id),
                bom_line_id INTEGER NOT NULL,
                old_product_id INTEGER NOT NULL,
                new_product_id INTEGER NOT NULL,
                old_qty TEXT NOT NULL,
                new_qty TEXT NOT NULL,
                PRIMARY KEY(batch_id,bom_line_id)
            );
        """)


def active_snapshot(db: sqlite3.Connection, workspace_id: int) -> int:
    row = db.execute(
        "SELECT id FROM sync_snapshots WHERE workspace_id=? AND state='active'",
        (workspace_id,),
    ).fetchone()
    if not row:
        raise HTTPException(409, "No active BOM snapshot for this workspace")
    return row["id"]


def _decimal(value: object, where: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{where}: qty is missing or invalid")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{where}: qty must be numeric") from None
    if not result.is_finite() or result <= 0 or result > Decimal("1000000000000"):
        raise ValueError(f"{where}: qty must be positive and finite")
    # A precision guard avoids silently inserting values with huge scales.
    if result.as_tuple().exponent < -12:
        raise ValueError(f"{where}: qty supports at most 12 decimal places")
    return result


def qty_text(number: object) -> str:
    value = _decimal(number, "value")
    return format(value.normalize(), "f")


def _positive_id(value: object, label: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label}: ID is missing")
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite() or n != n.to_integral_value() or not (0 < n <= 2**53):
            raise ValueError()
        return int(n)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}: must be a positive numeric ID") from None


def _m2o(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, list) and value and isinstance(value[0], int):
        return value[0]
    return None


def load_lines(db: sqlite3.Connection, workspace_id: int, snapshot_id: int) -> list[dict]:
    query = """
        SELECT l.odoo_id AS bom_line_id,l.bom_id,l.product_id AS base_product_id,
               l.product_uom_id AS uom_id,l.raw_json AS line_json,
               w.component_product_id AS working_product_id,w.qty AS working_qty,
               b.raw_json AS bom_json
        FROM sync_records l
        LEFT JOIN local_bom_working w
          ON w.workspace_id=? AND w.snapshot_id=l.snapshot_id AND w.bom_line_id=l.odoo_id
        LEFT JOIN sync_records b
          ON b.snapshot_id=l.snapshot_id AND b.model='mrp.bom' AND b.odoo_id=l.bom_id
        WHERE l.snapshot_id=? AND l.model='mrp.bom.line'
        ORDER BY l.bom_id,l.odoo_id
    """
    rows = db.execute(query, (workspace_id, snapshot_id)).fetchall()
    if len(rows) > MAX_LINES:
        raise HTTPException(409, f"Snapshot exceeds Excel limit of {MAX_LINES} BOM lines")
    products = {
        row["odoo_id"]: json.loads(row["raw_json"])
        for row in db.execute(
            "SELECT odoo_id,raw_json FROM sync_records "
            "WHERE snapshot_id=? AND model='product.product'", (snapshot_id,),
        )
    }
    result = []
    for row in rows:
        raw = json.loads(row["line_json"])
        bom = json.loads(row["bom_json"]) if row["bom_json"] else {}
        product_id = row["working_product_id"] if row["working_product_id"] is not None else row["base_product_id"]
        if product_id is None or row["bom_id"] is None or product_id not in products:
            raise HTTPException(409, "Snapshot contains BOM lines with missing component or BOM references")
        try:
            quantity = qty_text(row["working_qty"] if row["working_qty"] is not None else raw.get("product_qty"))
        except ValueError as exc:
            raise HTTPException(409, f"Invalid local quantity in BOM line {row['bom_line_id']}") from exc
        product = products[product_id]
        result.append({
            "workspace_id": workspace_id, "snapshot_id": snapshot_id,
            "bom_id": row["bom_id"], "bom_line_id": row["bom_line_id"],
            "original_component_product_id": product_id,
            "new_component_product_id": product_id,
            "original_qty": quantity, "new_qty": quantity,
            "uom_id": row["uom_id"] or _m2o(raw.get("product_uom_id")) or 0,
            "bom_name": str(bom.get("display_name") or bom.get("code") or row["bom_id"]),
            "component_name": str(product.get("display_name") or product.get("name") or product_id),
            "sku": str(product.get("default_code") or ""),
        })
    return result


def _col(n: int) -> str:
    chars = ""
    while n:
        n, rem = divmod(n - 1, 26)
        chars = chr(65 + rem) + chars
    return chars


def _xml_safe(value: object) -> str:
    text = str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return escape(text, {'"': "&quot;"})


def _cell(col: int, row: int, value: object, style: int = 0) -> str:
    ref = f"{_col(col)}{row}"
    attr = f' r="{ref}" s="{style}"'
    if value is None:
        return f"<c{attr}/>"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return f"<c{attr}><v>{value}</v></c>"
    # Explicit inlineStr prevents Excel from interpreting leading = + - @ as formulas.
    return f'<c{attr} t="inlineStr"><is><t xml:space="preserve">{_xml_safe(value)}</t></is></c>'


def _sheet_xml(headers: tuple[str, ...], rows: list[list[object]]) -> bytes:
    last_col = _col(len(headers))
    width = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(
            (15, 15, 13, 15, 23, 23, 18, 18, 12, 38, 38, 22)[:len(headers)], start=1
        )
    )
    pieces = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<worksheet xmlns="{NS}"><dimension ref="A1:{last_col}{len(rows)+1}"/>',
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
        f'<cols>{width}</cols><sheetData>',
        '<row r="1">' + "".join(_cell(i, 1, h, 1) for i, h in enumerate(headers, 1)) + '</row>',
    ]
    for ridx, values in enumerate(rows, 2):
        pieces.append(
            f'<row r="{ridx}">' +
            "".join(_cell(j, ridx, v, 2 if headers[j-1] in EDITABLE else 0)
                    for j, v in enumerate(values, 1)) + '</row>'
        )
    pieces.extend([
        '</sheetData>',
        f'<autoFilter ref="A1:{last_col}{len(rows)+1}"/>',
        '</worksheet>',
    ])
    return "".join(pieces).encode("utf-8")


def export_workbook(workspace_id: int, snapshot_id: int, lines: list[dict], catalog: list[list[object]]) -> bytes:
    rows: list[list[object]] = []
    for line in lines:
        values: list[object] = []
        for field in COLUMNS:
            value = line[field]
            if field in ("original_qty", "new_qty"):
                value = Decimal(value)
            values.append(value)
        rows.append(values)
    notes = [
        ["BOM TOOLS - EDIT LOKAL", "File ini hanya untuk mengubah SQLite lokal. Tidak menulis ke Odoo."],
        ["Workspace ID", workspace_id],
        ["Snapshot ID", snapshot_id],
        ["Baris BOM Line", len(lines)],
        ["LANGKAH 1", "Edit hanya kolom F (new_component_product_id) dan H (new_qty) pada sheet BOM Lines."],
        ["LANGKAH 2", "Jangan mengubah/hapus/tambah baris atau ID. Urutan baris boleh diubah."],
        ["LANGKAH 3", "Upload Excel ke aplikasi, periksa preview, lalu tekan Apply to Local."],
        ["CATATAN", "Qty > 0; Product ID harus tersedia pada snapshot yang sama."],
        ["KATALOG PRODUK", "Gunakan sheet Product Catalog untuk memilih Product ID pengganti."],
        ["KEAMANAN", "Snapshot Odoo asli tetap utuh; perubahan hanya di working BOM lokal."],
    ]
    ct = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet3.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<workbook xmlns="{NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="BOM Lines" sheetId="1" r:id="rId1"/>'
        '<sheet name="Instructions" sheetId="2" r:id="rId2"/>'
        '<sheet name="Product Catalog" sheetId="3" r:id="rId4"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId4" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet3.xml"/></Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<styleSheet xmlns="{NS}">'
        '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font></fonts>'
        '<fills count="4"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF173954"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF0BA"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3"><xf xfId="0" fontId="0" fillId="0" borderId="0"/>'
        '<xf xfId="0" fontId="1" fillId="2" borderId="0" applyFill="1" applyFont="1"/>'
        '<xf xfId="0" fontId="0" fillId="3" borderId="0" applyFill="1"/>'
        '</cellXfs></styleSheet>'
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as z:
        for name, value in (
            ("[Content_Types].xml", ct), ("_rels/.rels", root_rels),
            ("xl/workbook.xml", workbook), ("xl/_rels/workbook.xml.rels", workbook_rels),
            ("xl/styles.xml", styles),
            ("xl/worksheets/sheet1.xml", _sheet_xml(COLUMNS, rows)),
            ("xl/worksheets/sheet2.xml", _sheet_xml(("Petunjuk", "Keterangan"), notes)),
            ("xl/worksheets/sheet3.xml", _sheet_xml(
                ("Product ID", "SKU", "Product Name", "Default UOM ID", "Active"), catalog)),
        ):
            z.writestr(name, value)
    return buffer.getvalue()


def _decode_shared_strings(data: bytes) -> list[str]:
    root = ET.fromstring(data)
    return [
        "".join(part.text or "" for part in si.iter(TAG("t")))
        for si in root.findall(TAG("si"))
    ]


def parse_workbook(data: bytes) -> list[tuple[int, dict]]:
    """Return (Excel row number, values), rejecting formulas, macros, and zip bombs."""
    if not data or len(data) > MAX_XLSX_BYTES:
        raise ValueError("Excel file is empty or exceeds 12 MB")
    try:
        z = ZipFile(BytesIO(data))
        with z:
            infos = z.infolist()
            names = {info.filename for info in infos}
            if (len(infos) > 1000 or len(names) != len(infos) or
                sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES or
                any(info.flag_bits & 1 or info.filename.startswith("/")
                    or ".." in info.filename.split("/")
                    or "externalLinks" in info.filename or "vbaProject" in info.filename
                    for info in infos)):
                raise ValueError("Unsafe or oversized Excel archive")
            if "xl/worksheets/sheet1.xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("Invalid Excel workbook")
            root = ET.fromstring(z.read("xl/workbook.xml"))
            sheets = root.find(TAG("sheets"))
            if sheets is None or len(sheets) < 1 or sheets[0].get("name") != "BOM Lines":
                raise ValueError("First worksheet must be named BOM Lines")
            strings = _decode_shared_strings(z.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else []
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            result: list[tuple[int, dict]] = []
            for row in sheet.findall(".//" + TAG("sheetData") + "/" + TAG("row")):
                row_no = int(row.get("r", "0"))
                if row_no > MAX_LINES + 2:
                    raise ValueError("Excel exceeds maximum row limit")
                values: dict[int, object] = {}
                for cell in row.findall(TAG("c")):
                    if cell.find(TAG("f")) is not None:
                        raise ValueError(f"Excel formula found in row {row_no}; use values only")
                    ref = cell.get("r", "")
                    letters = re.match(r"([A-Z]+)[0-9]+$", ref)
                    if not letters:
                        raise ValueError(f"Invalid cell address in row {row_no}")
                    col = 0
                    for char in letters.group(1):
                        col = col * 26 + ord(char) - 64
                    v = cell.find(TAG("v"))
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        inline = cell.find(TAG("is"))
                        value = "".join(part.text or "" for part in inline.iter(TAG("t"))) if inline is not None else ""
                    elif kind == "s":
                        index = int(v.text) if v is not None and v.text is not None else -1
                        if not 0 <= index < len(strings):
                            raise ValueError(f"Invalid shared string in row {row_no}")
                        value = strings[index]
                    elif kind in ("str", "e"):
                        raise ValueError(f"Unexpected cell type in row {row_no}")
                    elif v is None or v.text is None:
                        value = None
                    else:
                        value = v.text
                    if value is not None and str(value).strip() != "":
                        values[col] = value
                if row_no == 1:
                    headers = tuple(str(values.get(i, "")) for i in range(1, len(COLUMNS) + 1))
                    if headers != COLUMNS or any(c > len(COLUMNS) for c in values):
                        raise ValueError("Excel headers do not match the BOM Tools template")
                elif values:
                    if any(c > len(COLUMNS) for c in values):
                        raise ValueError(f"Unexpected column in Excel row {row_no}")
                    result.append((row_no, {name: values.get(i) for i, name in enumerate(COLUMNS, 1)}))
                    if len(result) > MAX_LINES:
                        raise ValueError("Too many BOM lines in uploaded Excel")
            if "headers" not in locals():
                raise ValueError("Missing Excel header row")
            return result
    except (BadZipFile, ET.ParseError, KeyError, OverflowError) as exc:
        raise ValueError("Invalid or corrupt .xlsx file") from exc


def validate_import(db: sqlite3.Connection, workspace_id: int, parsed: list[tuple[int, dict]]) -> dict:
    snapshot_id = active_snapshot(db, workspace_id)
    current = {item["bom_line_id"]: item for item in load_lines(db, workspace_id, snapshot_id)}
    products = {
        row["odoo_id"]: json.loads(row["raw_json"])
        for row in db.execute(
            "SELECT odoo_id,raw_json FROM sync_records "
            "WHERE snapshot_id=? AND model='product.product'", (snapshot_id,)
        )
    }
    uoms = {
        row["odoo_id"]: json.loads(row["raw_json"])
        for row in db.execute(
            "SELECT odoo_id,raw_json FROM sync_records "
            "WHERE snapshot_id=? AND model='uom.uom'", (snapshot_id,)
        )
    }
    boms = {
        row["odoo_id"]: json.loads(row["raw_json"])
        for row in db.execute(
            "SELECT odoo_id,raw_json FROM sync_records "
            "WHERE snapshot_id=? AND model='mrp.bom'", (snapshot_id,)
        )
    }
    seen, changes, warnings, errors = set(), [], [], []
    for excel_row, values in parsed:
        label = f"Excel row {excel_row}"
        try:
            ws = _positive_id(values["workspace_id"], label + " workspace_id")
            snap = _positive_id(values["snapshot_id"], label + " snapshot_id")
            bom_id = _positive_id(values["bom_id"], label + " bom_id")
            line_id = _positive_id(values["bom_line_id"], label + " bom_line_id")
            old_product = _positive_id(values["original_component_product_id"], label + " old component")
            new_product = _positive_id(values["new_component_product_id"], label + " new component")
            original_qty = _decimal(values["original_qty"], label + " original_qty")
            new_qty = _decimal(values["new_qty"], label + " new_qty")
            uom_id = _positive_id(values["uom_id"], label + " uom_id")
            if (ws, snap) != (workspace_id, snapshot_id):
                raise ValueError(f"{label}: workspace or snapshot differs from current")
            if line_id in seen:
                raise ValueError(f"{label}: duplicate BOM Line ID {line_id}")
            seen.add(line_id)
            baseline = current.get(line_id)
            if baseline is None:
                raise ValueError(f"{label}: unknown BOM Line ID {line_id}")
            if (bom_id != baseline["bom_id"] or
                uom_id != baseline["uom_id"] or
                old_product != baseline["original_component_product_id"] or
                original_qty != Decimal(baseline["original_qty"])):
                raise ValueError(f"{label}: source values/IDs changed; export a fresh Excel file")
            for field in ("bom_name", "component_name", "sku"):
                if str(values.get(field) or "") != str(baseline.get(field) or ""):
                    raise ValueError(f"{label}: column {field} is read-only; edit only F and H")
            if new_product not in products:
                raise ValueError(f"{label}: component Product ID {new_product} not in local snapshot")
            if old_product == new_product and original_qty == new_qty:
                continue
            product = products[new_product]
            bom = boms.get(bom_id, {})
            product_company = _m2o(product.get("company_id"))
            bom_company = _m2o(bom.get("company_id"))
            if product_company and bom_company and product_company != bom_company:
                warnings.append(f"{label}: component and BOM company differ")
            if product.get("active") is False:
                warnings.append(f"{label}: destination product is archived/inactive")
            product_uom = _m2o(product.get("uom_id"))
            if product_uom and product_uom != uom_id:
                source_category = _m2o(uoms.get(uom_id, {}).get("category_id"))
                target_category = _m2o(uoms.get(product_uom, {}).get("category_id"))
                if source_category and target_category and source_category != target_category:
                    raise ValueError(f"{label}: component UOM category is incompatible with BOM line UOM")
                warnings.append(f"{label}: destination product default UOM differs; BOM Line UOM stays unchanged")
            changes.append({
                "bom_line_id": line_id, "bom_id": bom_id, "old_product_id": old_product,
                "new_product_id": new_product, "old_qty": qty_text(original_qty),
                "new_qty": qty_text(new_qty),
            })
        except ValueError as exc:
            errors.append(str(exc))
    if len(seen) != len(current):
        missing = len(current) - len(seen)
        errors.append(f"Excel must contain every BOM Line exactly once; {missing} missing")
    return {
        "snapshot_id": snapshot_id,
        "total_lines": len(parsed),
        "changed_count": len(changes),
        "unchanged_count": len(parsed) - len(changes) - len(errors),
        "changes": changes,
        "warnings": warnings,
        "errors": errors,
    }


def save_preview(path: str, workspace_id: int, user_id: int, validated: dict) -> str | None:
    if validated["errors"]:
        raise HTTPException(
            422, {"message": "Excel validation failed", "error_count": len(validated["errors"]),
                  "errors": validated["errors"][:50]},
        )
    if not validated["changes"]:
        return None
    token = secrets.token_urlsafe(32)
    with transaction(path) as db:
        if active_snapshot(db, workspace_id) != validated["snapshot_id"]:
            raise HTTPException(409, "Snapshot changed while validating Excel")
        db.execute(
            "INSERT INTO local_excel_previews "
            "(token_hash,workspace_id,snapshot_id,created_by,created_at,expires_at,state,"
            "changes_json,warnings_json,changed_count) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            (sha256(token.encode()).hexdigest(), workspace_id, validated["snapshot_id"],
             user_id, timestamp(), timestamp(now() + timedelta(minutes=PREVIEW_TTL_MINUTES)),
             json.dumps(validated["changes"], separators=(",", ":")),
             json.dumps(validated["warnings"], separators=(",", ":")),
             validated["changed_count"]),
        )
        audit(db, user_id, "excel.local.preview", str(validated["changed_count"]), workspace_id)
    return token


def apply_preview(path: str, workspace_id: int, user_id: int, token: str) -> dict:
    token_hash = sha256(token.encode()).hexdigest()
    with transaction(path) as db:
        preview = db.execute(
            "SELECT * FROM local_excel_previews WHERE token_hash=? AND workspace_id=?",
            (token_hash, workspace_id),
        ).fetchone()
        if preview is None or preview["created_by"] != user_id:
            raise HTTPException(404, "Excel preview not found")
        if preview["state"] != "pending":
            raise HTTPException(409, "Excel preview has already been applied")
        if preview["expires_at"] < timestamp():
            raise HTTPException(409, "Excel preview expired; upload the Excel file again")
        if active_snapshot(db, workspace_id) != preview["snapshot_id"]:
            raise HTTPException(409, "Snapshot changed since preview; download fresh Excel")
        changes = json.loads(preview["changes_json"])
        expected = {row["bom_line_id"]: row for row in load_lines(db, workspace_id, preview["snapshot_id"])}
        for item in changes:
            current = expected.get(item["bom_line_id"])
            if (not current or
                current["original_component_product_id"] != item["old_product_id"] or
                Decimal(current["original_qty"]) != Decimal(item["old_qty"])):
                raise HTTPException(409, "Working BOM changed since preview; upload a fresh Excel")
        batch_id = db.execute(
            "INSERT INTO local_excel_apply_batches "
            "(workspace_id,snapshot_id,applied_by,applied_at,changed_count,preview_hash) "
            "VALUES(?,?,?,?,?,?)",
            (workspace_id, preview["snapshot_id"], user_id, timestamp(), len(changes), token_hash),
        ).lastrowid
        for item in changes:
            original = db.execute(
                "SELECT product_id,raw_json FROM sync_records "
                "WHERE snapshot_id=? AND model='mrp.bom.line' AND odoo_id=?",
                (preview["snapshot_id"], item["bom_line_id"]),
            ).fetchone()
            if original is None:
                raise HTTPException(409, "Source BOM Line no longer exists")
            base_qty = qty_text(json.loads(original["raw_json"]).get("product_qty"))
            if original["product_id"] == item["new_product_id"] and Decimal(base_qty) == Decimal(item["new_qty"]):
                db.execute(
                    "DELETE FROM local_bom_working WHERE workspace_id=? AND snapshot_id=? AND bom_line_id=?",
                    (workspace_id, preview["snapshot_id"], item["bom_line_id"]),
                )
            else:
                db.execute(
                    "INSERT INTO local_bom_working "
                    "(workspace_id,snapshot_id,bom_line_id,component_product_id,qty,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(workspace_id,snapshot_id,bom_line_id) DO UPDATE SET "
                    "component_product_id=excluded.component_product_id,qty=excluded.qty,"
                    "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                    (workspace_id, preview["snapshot_id"], item["bom_line_id"],
                     item["new_product_id"], item["new_qty"], user_id, timestamp()),
                )
            db.execute(
                "INSERT INTO local_excel_change_rows "
                "(batch_id,bom_line_id,old_product_id,new_product_id,old_qty,new_qty) "
                "VALUES(?,?,?,?,?,?)",
                (batch_id, item["bom_line_id"], item["old_product_id"],
                 item["new_product_id"], item["old_qty"], item["new_qty"]),
            )
        db.execute(
            "UPDATE local_excel_previews SET state='applied',applied_at=? WHERE token_hash=?",
            (timestamp(), token_hash),
        )
        audit(db, user_id, "excel.local.applied", str(batch_id), workspace_id)
        return {"state": "applied", "batch_id": batch_id, "changed_count": len(changes),
                "workspace_id": workspace_id, "snapshot_id": preview["snapshot_id"]}
