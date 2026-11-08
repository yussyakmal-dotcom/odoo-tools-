# ARCHITECTURE — Odoo 19 BOM Tools

**Status:** Draft v0.3  
**Tanggal:** 2026-11-09  
**Repository:** [yussyakmal-dotcom/odoo-tools-](https://github.com/yussyakmal-dotcom/odoo-tools-)  
**Target:** Odoo 19 SaaS, paket Custom  
**Prioritas saat ini:** **Tahap 1 — menarik semua data BOM yang dapat diakses dari Odoo dan menyimpannya secara lokal.**

## 1. Tujuan

BOM Tools adalah **mesin kerja lokal atas keseluruhan struktur Bill of Materials (BOM)**, bukan sekadar antarmuka untuk mengedit satu BOM di Odoo.

Alur utama:

1. Ambil seluruh BOM dan seluruh baris komponen BOM dari Odoo ke database lokal.
2. Simpan relasi, ID asli Odoo, metadata, dan referensi yang diperlukan agar data dapat ditelusuri.
3. Lakukan pencarian, analisis, dan rancangan transformasi massal di lokal tanpa harus mengirim query berulang ke Odoo.
4. Pada tahap berikutnya, tampilkan *preview/diff* perubahan; baru setelah validasi dan persetujuan, kirim perubahan terpilih ke Odoo.
5. Verifikasi perubahan dengan membaca ulang data server.

**Contoh use case utama (tahap lanjutan):** cari semua baris komponen dengan `product_id = X`, tampilkan BOM terdampak, kemudian siapkan penggantian seluruh penggunaan komponen X menjadi Y. ID baris `mrp.bom.line` dan ID header `mrp.bom` tetap dipertahankan; yang diganti adalah referensi produk komponennya.

## 2. Keputusan arsitektur

| Topik | Keputusan |
|---|---|
| Pola integrasi | **Local-first, server-authoritative**: Odoo adalah sumber kebenaran; lokal adalah tempat analisis/perencanaan perubahan |
| Bahasa | Python 3.11+ |
| Backend API | **FastAPI** (REST API internal; dokumentasi OpenAPI) |
| Deployment | **Docker + Docker Compose**, terdiri atas service API dan worker sinkronisasi |
| Database lokal | SQLite dalam Docker persistent volume (versi pertama) |
| API Odoo | **External JSON-2 API** Odoo 19: `POST /json/2/<model>/<method>` |
| Autentikasi | API key pada backend/CLI, tidak pada browser atau repository |
| Antarmuka awal | Endpoint FastAPI untuk *preflight*, mulai sync, status, dan ringkasan snapshot; GUI bukan prasyarat |
| Scope tahap 1 | **Read-only**: tidak memanggil `create`, `write`, atau `unlink` |
| Pola sinkronisasi | FastAPI memasukkan sync job ke antrean lokal; worker terpisah membuat full snapshot *staging*, memvalidasi, lalu mempublikasikan snapshot aktif |
| Data historis | Snapshot sinkronisasi dan calon perubahan dipisah; tidak menimpa data asli |
| Update Odoo | **Belum diimplementasikan**; masuk tahap berikutnya dan dinonaktifkan secara default |

### Diagram alur

```text
+----------------------------+
| Browser / API Client       |
+-------------+--------------+
              |
              | HTTP (localhost default)
              v
+----------------------------+
| FastAPI service (Docker)   |
| enqueue job / status / API |
+-------------+--------------+
              |
              | SQLite job queue
              v
+----------------------------+
| Sync Worker (Docker)       |
| fetch / staging / validate |
+-------------+--------------+
              |
              | HTTPS JSON-2 API
              v
+----------------------------+
| Odoo 19 SaaS Custom        |
| BOM / BOM Lines / Products |
+-------------+--------------+
              |
              | HTTPS JSON-2 API (read-only, batch)
              v
+----------------------------+
| Odoo API Client            |
| auth, paging, retry, audit |
+-------------+--------------+
              |
              v
+----------------------------+
| Sync / Snapshot Builder    |
| staging, mapping, validate |
+-------------+--------------+
              |
              v
+----------------------------+
| SQLite Local Database      |
| snapshot BOM + referensi   |
+-------------+--------------+
              |
              v
+----------------------------+
| Local Analysis Engine      |
| search / impact / replace  |
| (tahap berikutnya)         |
+-------------+--------------+
              |
              v
+----------------------------+
| Preview + Change Set       |
| approval / conflict check  |
| (tahap berikutnya)         |
+-------------+--------------+
              |
              | explicit commit only
              v
+----------------------------+
| Odoo Write-back + Verify   |
| (tahap berikutnya)         |
+----------------------------+
```

## 3. Ruang lingkup

### 3.1 Tahap 1 — wajib ada

- Service FastAPI berjalan di Docker dan memiliki endpoint health check, preflight, start sync, serta status sync.
- Worker sinkronisasi berjalan sebagai service Docker terpisah dari FastAPI; tidak memproses full download di dalam HTTP request.
- Koneksi terautentikasi ke database Odoo 19 SaaS Custom.
- Deteksi kemampuan baca untuk model/field yang dibutuhkan.
- Ambil **semua header BOM dan semua BOM line yang terlihat oleh akun integrasi** melalui paging.
- Sertakan BOM arsip/nonaktif bila model dan hak akses memungkinkan (misalnya konteks `active_test: false`).
- Ambil referensi produk dan satuan yang diperlukan untuk memahami setiap komponen, produk hasil, dan relasinya.
- Simpan ID asli Odoo serta `write_date` / metadata perubahan bila tersedia.
- Validasi jumlah, duplikasi ID, integritas relasi, dan kelengkapan halaman.
- *Atomic publish*: snapshot lama tetap dapat digunakan jika proses gagal.
- Laporan sinkronisasi: waktu, status, jumlah record per model, cakupan company, warning/error, dan alasan jika hasil belum dapat dinyatakan lengkap.
- Tidak ada operasi perubahan terhadap Odoo.

### 3.2 Di luar tahap 1

- Penggantian X → Y, perubahan kuantitas, penghapusan/penambahan baris.
- Write-back, rollback, pemulihan perubahan di Odoo.
- UI web, login banyak pengguna, penjadwalan otomatis (FastAPI minimal tetap termasuk tahap 1).
- Costing, forecasting, MRP simulation, atau optimasi BOM.

Hal-hal di atas dipertimbangkan dalam desain tetapi **tidak boleh menghambat pengiriman tahap 1**.

## 4. Kontrak integrasi Odoo

Odoo 19 menyediakan **External JSON-2 API** bagi pelanggan paket Custom. Akses mengikuti ACL, record rules, field access, dan company yang diizinkan untuk user API.

- Basis URL: `https://<odoo-host>/json/2`
- Metode: HTTP `POST` untuk pemanggilan model/method.
- Header: `Authorization: bearer <API_KEY>`, `Content-Type: application/json`, `User-Agent`; `X-Odoo-Database` bila diperlukan.
- Model/method: `/json/2/mrp.bom/search_read`, `/json/2/mrp.bom.line/search_read`, dst., sesuai kemampuan database.
- Respons: JSON; tangani HTTP 4xx/5xx, error terstruktur Odoo, timeout, dan kegagalan jaringan.
- **Satu request JSON-2 adalah satu transaksi tersendiri**, bukan satu transaksi lintas seluruh proses sinkronisasi.

Dokumentasi resmi: https://www.odoo.com/documentation/19.0/developer/reference/external_api.html

**Catatan:** field yang tersedia dan behavior modul dapat berbeda menurut database Odoo. Jangan langsung mengunci seluruh field tanpa pemeriksaan. Gunakan metadata model seperti `fields_get` bila diizinkan dan/atau dokumentasi dinamis database (`/doc` bila tersedia), lalu uji query minimal.

### 4.1 Model data target

| Model Odoo | Tujuan | Contoh field penting (verifikasi lebih dulu) |
|---|---|---|
| `mrp.bom` | Header BOM | `id`, `code`, `product_tmpl_id`, `product_id`, `product_qty`, `product_uom_id`, `type`, `company_id`, `active`, `write_date` |
| `mrp.bom.line` | Komponen dalam BOM | `id`, `bom_id`, `product_id`, `product_qty`, `product_uom_id`, `sequence`, `operation_id`, `write_date` |
| `product.product` | Varian produk komponen/hasil | `id`, `product_tmpl_id`, `default_code`, `display_name`, `active`, `uom_id`, `write_date` |
| `product.template` | Template produk hasil/komponen | `id`, `default_code`, `name`, `active`, `uom_id`, `write_date` |
| `uom.uom` | Identitas dan kategori satuan | `id`, `name`, `category_id`, `factor`, `rounding` |
| `res.company` | Identitas company | `id`, `name` |

Field varian BOM line (contoh: `bom_product_template_attribute_value_ids`), operasi, by-product, dan metadata manufaktur lain **perlu diinventarisasi saat discovery** agar snapshot kelak dapat mendukung analisis struktur BOM yang akurat. Bila model tambahan relevan, masukkan sebagai sumber tambahan dengan aturan validasi tersendiri; jangan menganggap semua relasi tersedia sebelum diverifikasi.

**Aturan identitas:** semua ID `product_id` merujuk ke `product.product.id`, bukan SKU dan bukan `product.template.id`. SKU dapat berubah, tidak selalu unik, dan tidak boleh menjadi primary key operasi massal.

### 4.2 Batas kelengkapan

Kata **"semua" berarti semua record dalam scope dan hak akses akun integrasi**, bukan jaminan semua record fisik database Odoo.

Sebelum menyatakan snapshot lengkap, simpan:

- URL host/database Odoo (tanpa kredensial).
- User integrasi dan company scope yang diizinkan (tanpa rahasia).
- Filter domain, mode `active_test`, field list yang disetujui, serta warning ACL.
- Jumlah record server dan hasil lokal per model.
- Hasil validasi integritas; bila ada pengecualian, status *incomplete*.

## 4A. FastAPI, job sync, dan Docker Compose

**FastAPI adalah pintu masuk aplikasi; worker adalah pelaksana sinkronisasi jangka panjang.** HTTP request tidak menunggu sampai seluruh BOM selesai diunduh. Sistem menggunakan satu job aktif pada satu waktu untuk menghindari sync berbarengan ke SQLite/Odoo.

### 4A.1 Endpoint awal (read-only terhadap Odoo)

| Method | Endpoint | Hasil |
|---|---|---|
| `GET` | `/api/v1/health` | Status backend/container |
| `POST` | `/api/v1/connection/test` | Pemeriksaan koneksi, model dan hak baca Odoo (tanpa mengembalikan token) |
| `POST` | `/api/v1/sync/runs` | Buat sync job, balas `202 Accepted` dan `job_id`; tolak job aktif ganda |
| `GET` | `/api/v1/sync/runs/{job_id}` | Status `queued/running/completed/failed/incomplete`, progres dan pesan error aman |
| `GET` | `/api/v1/snapshots/current` | Statistik snapshot aktif lokal beserta waktu dan cakupan sync |

Endpoint di atas adalah **rancangan**, bukan API yang sudah tersedia. Kontrak request/response final akan distandarkan ketika implementasi. `POST` di sini hanya memulai pekerjaan **pembacaan** Odoo dan menulis database lokal; tidak pernah melakukan `write` ke Odoo dalam tahap 1.

### 4A.2 Pembagian service

- **`api`**: menjalankan Uvicorn/FastAPI, memvalidasi request, menyimpan status job, serta menyajikan data lokal. Tidak mengunduh seluruh BOM di thread request.
- **`worker`**: membaca antrean job SQLite, melakukan koneksi Odoo, mendownload per batch, membuat snapshot staging, validasi, dan publish. Untuk tahap awal gunakan tepat satu worker; jika terhenti, job `running` yang terlantar harus ditandai dan bisa diulang dengan aman.
- **`bom_data` volume**: volume persisten yang di-mount ke kedua service, misalnya `/app/data`. File SQLite tetap ada setelah container dibangun ulang/dijalankan ulang.
- **Satu image Python** dibangun dari `Dockerfile` dan dipakai kedua service dengan perintah startup berbeda. Gunakan `docker-compose.yml` dan `.dockerignore`.

SQLite perlu `PRAGMA foreign_keys=ON`, `busy_timeout`, dan konfigurasi WAL yang sesuai untuk dua proses pada **host dan filesystem lokal yang sama**. Jangan gunakan filesystem jaringan untuk volume SQLite. Hanya worker yang menulis data snapshot; FastAPI boleh menulis record job yang kecil. Untuk multi-server atau concurrency tinggi, evaluasi migrasi job queue dan storage ke PostgreSQL nanti.

### 4A.3 Network, secret, dan operasional

- **Port akses aplikasi ditetapkan 700 (bukan 8000)**. Docker Compose memetakan **host `127.0.0.1:700` → container `8080`** dengan `ports: ["127.0.0.1:700:8080"]`. Port 8080 hanya dipakai **di dalam container** untuk Uvicorn; URL akses dari host adalah `http://localhost:700`.
- **Jangan expose ke internet** tanpa autentikasi, TLS reverse proxy, dan kontrol akses. Port host 700 harus diperiksa agar tidak bentrok dengan layanan lain.
- `ODOO_API_KEY` melalui environment/secrets saat deployment, bukan di Dockerfile/image, repo, URL, atau request body.
- Contoh konfigurasi aman disediakan pada `.env.example`. File `.env` nyata ditambahkan ke `.gitignore`.
- Logging ke stdout/stderr Docker, tanpa token dan tanpa dump isi data sensitif.
- Healthcheck API dan prosedur start/stop serta backup volume didokumentasikan di README.
- Commands target setelah implementasi: `docker compose up -d --build`, `docker compose logs -f api worker`, `docker compose down`. **`down -v` tidak boleh dipakai sembarangan** karena menghapus volume data.

## 5. Strategi full synchronization

### 5.1 Prinsip

- Lakukan pengunduhan **berhalaman (batch)**, tidak satu response raksasa.
- Gunakan urutan stabil `id asc` dan cursor `id > last_id` bila didukung.
- Ukuran awal batch disarankan 200–500 record, dibuat dapat dikonfigurasi setelah uji.
- Gunakan `search_read` untuk pencarian dan pembacaan dalam satu request, bila tersedia.
- Simpan hasil per batch ke staging dalam transaksi SQLite.
- Idempotensi: re-run tidak menghasilkan baris ganda.
- Batasi concurrency dan lakukan retry dengan exponential backoff untuk masalah sementara; **jangan** terus mengulang 401/403 atau error field/schema.
- Jangan menganggap satu rangkaian request API sebagai snapshot atomik Odoo.

### 5.2 Algoritma full sync

1. **Preflight:** request FastAPI atau worker memvalidasi URL, API key, konektivitas, kemampuan model, company scope, dan field discovery. FastAPI mengantrekan job dan segera mengembalikan `job_id`; worker mengeksekusi langkah berikutnya.
2. Buat `sync_run` dengan status `running`; buat snapshot SQLite staging yang terisolasi dari snapshot aktif.
3. Tarik referensi yang diperlukan: company, UOM, produk dan template produk (lengkap dalam scope yang telah ditentukan).
4. Tarik `mrp.bom` dengan cursor/paging sampai habis. Gunakan filter yang eksplisit dan `active_test: false` bila didukung.
5. Tarik `mrp.bom.line` hingga habis dengan strategi yang sama. Untuk scope multi-company, terapkan aturan akses/relasi secara konsisten.
6. Normalisasi nilai relasi Many2one (misalnya `[id, display_name]` atau `false`) menjadi ID numerik/null; simpan nilai mentah tambahan bila diperlukan untuk audit.
7. Jalankan validasi (bagian 7): hitungan, ID unik, foreign key, dan kesesuaian cakupan.
8. Lakukan pemeriksaan ulang hitungan/perubahan untuk mendeteksi drift akibat transaksi server selama sync; jika data bergerak, jalankan pass rekonsiliasi atau tandai snapshot tidak stabil. Ini mengurangi risiko tetapi **bukan jaminan konsistensi atomik**.
9. Bila lulus, publikasikan staging sebagai **snapshot aktif** dalam transaksi lokal atomik; snapshot sebelumnya tetap tersedia untuk pemulihan.
10. Catat waktu selesai, statistik, dan laporan hasil yang bisa dibaca operator.

Jika gagal di salah satu langkah: snapshot aktif **tidak berubah** dan hasil run diberi status `failed` / `incomplete`. Resume merupakan optimasi; untuk implementasi pertama, *restart full sync* dengan staging baru lebih sederhana dan aman.

### 5.3 Perubahan di Odoo saat sinkronisasi

API JSON-2 tidak memberi transaksi tunggal lintas paging. Karena itu, sinkronisasi bukan *point-in-time database backup*. Sebagian data mungkin berubah selama pengunduhan.

Mitigasi:

- Simpan `write_date` per record jika tersedia.
- Bandingkan jumlah dan watermark perubahan sebelum/sesudah proses; deteksi drift, bukan sekadar menyamakan count.
- Jalankan reconciliation pass untuk record yang berubah/baru; periksa pula penghapusan dan arsip.
- Jika tidak bisa membuktikan cakupan stabil, laporkan `incomplete / unstable`. Jangan klaim *100% identical* secara berlebihan.
- Untuk operasi write-back kelak, **re-read record server tepat sebelum update** dan tolak jika tidak cocok dengan baseline yang disetujui.

## 6. Desain penyimpanan lokal

Gunakan SQLite dengan foreign key aktif dan indeks khusus untuk pencarian komponen.

Tabel inti yang diusulkan:

| Tabel | Isi |
|---|---|
| `sync_runs` | ID run, mulai/selesai, scope, jumlah, status, error |
| `snapshots` | ID snapshot, sync_run, status staging/active/archived, waktu publish |
| `bom_headers` | `snapshot_id` + `odoo_bom_id`, field BOM, payload tambahan |
| `bom_lines` | `snapshot_id` + `odoo_bom_line_id`, `odoo_bom_id`, `component_product_id`, qty/UOM, metadata |
| `products` | `snapshot_id` + `odoo_product_id`, SKU/nama/template, status |
| `product_templates` | `snapshot_id` + `odoo_template_id` |
| `uoms` | `snapshot_id` + `odoo_uom_id`, kategori dan konversi |
| `companies` | `snapshot_id` + `odoo_company_id` |
| `sync_issues` | Missing refs, permission, drift, mismatch, field unavailable |

Untuk tiap tabel data, simpan `raw_json` (payload API) **di samping kolom ter-normalisasi** agar informasi yang tidak dipetakan belum hilang. Record yang ada dalam snapshot berbeda harus bisa hidup berdampingan.

Indeks minimum:

- `bom_lines(snapshot_id, component_product_id)` — pencarian global komponen X.
- `bom_lines(snapshot_id, odoo_bom_id)` — rincian satu BOM.
- `bom_headers(snapshot_id, product_tmpl_id)` dan/atau `product_id` — pencarian produk hasil.
- `products(snapshot_id, default_code)` — pencarian berdasarkan SKU.
- Unique `(snapshot_id, odoo_bom_id)` dan `(snapshot_id, odoo_bom_line_id)` untuk mencegah duplikasi per snapshot.

**Catatan:** nama tabel/kolom di atas adalah desain internal, bukan nama field API Odoo. SQL DDL final dibuat setelah discovery field.

### 6.1 Contoh query lokal (target tahap 2)

Cari seluruh BOM yang memakai product variant ID X:

```sql
SELECT
    bl.odoo_bom_line_id,
    bl.odoo_bom_id,
    bl.component_product_id,
    bl.product_qty
FROM bom_lines bl
WHERE bl.snapshot_id = :active_snapshot_id
  AND bl.component_product_id = :product_x
ORDER BY bl.odoo_bom_id, bl.odoo_bom_line_id;
```

Operasi X → Y nantinya membuat *change set* terpisah, **bukan mengubah snapshot hasil download**. Setiap perubahan mengacu pada `odoo_bom_line_id` dan baseline dari snapshot aktif.

## 7. Validasi & acceptance criteria tahap 1

Sebuah full sync dinyatakan **berhasil** hanya jika:

1. Koneksi berjalan melalui JSON-2 dengan hak baca yang disetujui.
2. Semua halaman data dalam scope sudah terambil; tidak ada error atau halaman yang terlewati diam-diam.
3. Jumlah unik BOM header dan BOM line lokal cocok dengan hasil pembacaan scope server yang stabil; perbedaan harus didokumentasikan dan menyebabkan status incomplete.
4. Tidak ada primary key ganda pada snapshot yang sama.
5. Semua `bom_lines.odoo_bom_id` mengarah ke header pada snapshot, kecuali ada pengecualian yang jelas dilaporkan dan disetujui.
6. Semua referensi komponen dan UOM yang diperlukan tersedia, atau warning scope/hak akses dilaporkan.
7. BOM nonaktif/arsip diperlakukan sesuai scope yang dinyatakan, tidak terlewat tanpa pemberitahuan.
8. Jika full sync gagal, snapshot aktif lama tetap valid dan queryable.
9. Operator dapat melihat ringkasan hasil: total BOM, BOM line, produk, UOM, company, timestamp, warning, dan failed record count.
10. **Tidak ada write request ke Odoo** selama tahap 1.

Uji yang wajib mencakup: BOM tanpa line, penggunaan satu komponen dalam banyak BOM, produk tanpa SKU, BOM aktif/nonaktif, multi-company (bila digunakan), pagination >1 halaman, timeout di tengah sinkronisasi, retry, permission denied, dan perubahan data server saat sync berlangsung.

## 8. Keamanan & konfigurasi

- Kredensial API berada dalam environment variable atau secret store lokal, misalnya `ODOO_BASE_URL`, `ODOO_DATABASE` (opsional), `ODOO_API_KEY`; jangan commit `.env`.
- Gunakan akun bot integrasi dengan hak baca minimum pada tahap 1.
- Jangan menampilkan API key/token dalam log, error trace, screenshot, atau dokumen.
- Akses HTTPS wajib; verifikasi sertifikat TLS aktif.
- Database lokal dan snapshot dapat berisi data perusahaan sensitif: simpan di volume Docker persisten non-repo dengan permission terbatas dan backup terproteksi.
- Nama host dan konfigurasi publik boleh didokumentasikan, **token rahasia tidak boleh**.
- Untuk fase write-back: `WRITE_BACK_ENABLED=false` sebagai default, konfirmasi eksplisit, audit log, dan kemampuan dry-run.
- Repository tidak menyimpan salinan BOM produksi ataupun file SQLite nyata.

## 9. Struktur repository yang direncanakan

```text
odoo-tools-/
├── doc/
│   └── ARCHITECTURE.md          # dokumen ini
├── src/
│   └── odoo_tools/
│       ├── main.py             # FastAPI entrypoint
│       ├── api/
│       │   ├── health.py       # health
│       │   ├── connection.py   # Odoo preflight
│       │   ├── sync_runs.py    # antrean + status job
│       │   └── snapshots.py    # ringkasan data lokal
│       ├── config.py
│       ├── odoo_client.py       # JSON-2 API, auth, retry
│       ├── discovery.py         # field / model discovery
│       ├── worker.py            # proses sync job terpisah
│       ├── sync.py              # full snapshot builder
│       ├── db.py                # SQLite, schema, migrations
│       └── validation.py        # completeness checks
├── tests/
│   ├── test_api.py
│   ├── test_worker.py
│   ├── test_pagination.py
│   ├── test_mapping.py
│   ├── test_sync.py
│   └── test_validation.py
├── Dockerfile                    # image FastAPI dan worker
├── docker-compose.yml            # service api, worker, volume
├── .dockerignore
├── .env.example                 # nama variable saja, tanpa secret
├── .gitignore                   # .env, *.db, data/, logs/
├── pyproject.toml
└── README.md
```

Folder dan file di atas **rencana**, belum otomatis dibuat.

## 10. Roadmap dan urutan delivery

### Milestone M1 — FastAPI + Docker + koneksi/discovery

- Docker Compose menjalankan service `api` dan `worker` dengan volume SQLite persisten.
- FastAPI dapat menghubungi Odoo, membaca model/field yang tersedia, dan menguji hak akses.
- Endpoint `/api/v1/health` dan `/api/v1/connection/test` dapat diuji tanpa mengubah data Odoo.
- Hasil: laporan preflight; **tidak ada perubahan di Odoo**.

### Milestone M2 — Async sync job + full download + SQLite

- `POST /api/v1/sync/runs` mengantrekan pekerjaan, worker memprosesnya, dan endpoint status melaporkan progres.
- Download seluruh header BOM, BOM lines, produk/template, UOM, dan referensi yang diperlukan.
- Hasil: snapshot staging SQLite yang dapat dihitung dan di-query.

### Milestone M3 — Validation + snapshot publish

- Validasi integrity/completeness, publish snapshot aktif, dan laporan.
- Hasil: **Tahap 1 selesai**. Ringkasan snapshot dapat dibaca melalui FastAPI dan query lokal sudah dapat digunakan.

### Milestone M4 — Local analysis

- Pencarian ID X di semua BOM, jumlah dan daftar BOM terdampak.
- Semua query dijalankan lokal.

### Milestone M5 — Change sets / transform

- Rancang X → Y, preview dan validasi (UOM, company, varian, duplikasi line, produk tidak aktif).
- Snapshot asli tetap immutable.

### Milestone M6 — Safe write-back

- Re-read kondisi Odoo, conflict detection, approval, update selektif, audit dan verification.
- Karena setiap panggilan JSON-2 adalah transaksi terpisah, operasi lintas banyak record harus memiliki mekanisme partial-failure dan recovery.

## 11. Keputusan terbuka (diuji sebelum coding M1/M2)

1. URL instance Odoo dan nama database yang benar (jangan tulis API key di chat/repo).
2. Apakah BOM perlu diambil untuk satu company atau seluruh company yang diizinkan?
3. Hak akses bot integrasi dan visibilitas BOM arsip.
4. Field BOM/line tambahan apa yang tersedia di database aktual, termasuk varian, operasi, dan by-product?
5. Lokasi Docker host (laptop/server), backup Docker volume, dan kebutuhan pengamanan SQLite.
6. Ukuran data riil untuk menentukan batch size, timeout, serta performa SQLite.

## 12. Prinsip implementasi yang tidak boleh dilanggar

- **Download dulu, validasi dulu, baru analisis.**
- **Jangan edit snapshot mentah.** Transformasi disimpan terpisah.
- **ID Odoo harus dipertahankan** sebagai identitas relasi.
- **Read-only secara nyata** terhadap Odoo pada tahap pertama.
- **FastAPI bukan worker sinkronisasi**: unduhan penuh diproses service worker terpisah.
- **Volume data Docker harus persisten** dan aman dari penghapusan tidak sengaja.
- **Port publik lokal harus 700**, bukan port default FastAPI 8000.
- **Tidak ada klaim “semua data” tanpa cakupan dan laporan validasi.**
- **Satu kegagalan sinkronisasi tidak boleh merusak snapshot terakhir yang valid.**
- **Write-back hanya setelah preview, validasi, persetujuan, dan conflict check.**

---

### Referensi

- Odoo 19, *External JSON-2 API*: https://www.odoo.com/documentation/19.0/developer/reference/external_api.html

**Status pekerjaan:** Dokumen arsitektur dibuat sebagai draft awal untuk ditinjau. Implementasi tahap 1 dan pengujian koneksi Odoo belum dilakukan.
