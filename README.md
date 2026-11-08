# Odoo 19 BOM Tools

Fondasi aplikasi BOM lokal berbasis **FastAPI + SQLite + Docker Compose**. Aplikasi menyediakan login Administrator, pengelolaan akun, pemilihan workspace, koneksi JSON-2 Odoo, serta **Download All BOM** ke SQLite lokal dengan proses worker dan monitoring. **Tidak ada fitur menulis/mengubah BOM di Odoo.** Desain lengkap: [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

## Jalankan dengan Docker

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Aplikasi: **http://localhost:700/**, Swagger: **http://localhost:700/docs**, health: **http://localhost:700/api/v1/health**.

**Port host 700** (`127.0.0.1:700`) dipetakan ke port internal container 8080; tidak menggunakan port 8000. Akses hanya dari mesin lokal secara default. Periksa konflik port 700 sebelum menjalankan.

### Membuat Administrator pertama

Satu kali sesudah container berjalan, jalankan (terminal interaktif):

```bash
docker compose exec api python -m odoo_tools.cli create-admin --username administrator
```

Password dimasukkan lewat prompt, minimal 12 karakter. **Tidak ada password bawaan.** Perintah bootstrap akan menolak jika Administrator sudah ada. Setelah itu buka `http://localhost:700/` dan login. Ubah password dari menu akun jika diperlukan.

### Upgrade server: aktifkan penyimpanan API Key Odoo

Setelah kode terbaru diambil dengan `git pull --ff-only origin main`, **jangan hapus Docker volume** karena di situ akun Administrator dan workspace tersimpan.

```bash
docker compose build api
docker compose run --rm --no-deps api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Salin hasil generator ke file `.env` **yang sudah ada** pada server, sebagai nilai `BOM_ENCRYPTION_KEY` (jangan kirim nilainya lewat chat, GitHub, atau screenshot). Contoh penulisan tanpa nilai nyata:

```dotenv
BOM_ENCRYPTION_KEY=PASTE_GENERATED_FERNET_KEY_HERE
BOM_ODOO_ALLOWED_HOSTS=
```

**Kunci ini harus dibuat SATU KALI dan disimpan/di-backup secara aman.** Jika diganti/hilang, API Key Odoo yang sudah disimpan tidak dapat dibuka lagi; masukkan API Key baru hanya setelah kunci benar diperbaiki. Jangan regenerasi kunci saat restart/upgrade.

Secara default URL `https://nama-instance.odoo.com` diizinkan. Bila menggunakan domain custom, isikan **nama host persis** (bukan URL), contoh `BOM_ODOO_ALLOWED_HOSTS=erp.perusahaan.id`. Semua request menggunakan HTTPS; URL localhost/IP/path atau protokol HTTP ditolak.

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps
```

Buka `http://localhost:700/`, login Administrator, pilih **Pengaturan koneksi Odoo**, pilih workspace, isi URL dan nama database jika dibutuhkan, lalu tempel API Key di formulir. Klik **Simpan konfigurasi** dan **Test Connection**.

- Pengujian memakai `res.users/context_get`, dilanjutkan `search_read` (`limit=1`, field `id`) untuk `mrp.bom`, `mrp.bom.line`, `product.product`, `product.template`, `uom.uom`.
- Hasil `connected` berarti semua model uji dapat dibaca, `partial` berarti autentikasi berhasil tetapi ada model yang gagal, `failed` berarti koneksi/autentikasi awal gagal. Ini **belum membuktikan kelengkapan seluruh BOM**.
- API Key disimpan terenkripsi dengan Fernet di SQLite; halaman baca konfigurasi **tidak pernah menampilkan kunci**. API Key baru hanya perlu diisi saat pertama kali atau saat rotasi.
- Hanya **Administrator** dan **Workspace Admin** yang boleh mengatur/menguji koneksi workspace. Pengguna lain ditolak oleh backend.
- Kode ini belum memanggil `write/create/unlink` ke Odoo dan belum menjalankan download massal.


### Jika konfigurasi Odoo tampak hilang setelah refresh atau upgrade

1. Pastikan **workspace yang dipilih benar**. Aplikasi sekarang mengingat workspace terakhir yang dipilih pada browser ini; API Key tidak pernah ditampilkan kembali demi keamanan.
2. Jika label menyatakan **API Key tersimpan di server** dan URL Odoo tampil, koneksi **masih tersimpan** meskipun input password terlihat kosong.
3. Jika URL dan label penyimpanan kosong pada workspace yang benar, cek database SQLite di volume **tanpa menampilkan API Key**:

```bash
cd ~/odoo-tools-
docker compose exec -T api python - <<'PY'
from odoo_tools.config import Settings
from odoo_tools.db import open_db

with open_db(Settings.from_env().db_path) as db:
    rows = db.execute("""
        SELECT w.id, w.name,
               CASE WHEN c.workspace_id IS NULL THEN 'TIDAK ADA' ELSE 'TERSIMPAN' END,
               COALESCE(c.last_test_status, 'belum diuji')
        FROM workspaces w
        LEFT JOIN workspace_odoo_connections c ON c.workspace_id = w.id
        ORDER BY w.id
    """).fetchall()
    for row in rows:
        print(f"Workspace {row[0]} ({row[1]}): koneksi {row[2]}, tes {row[3]}")
PY
```

Jika hasilnya `TERSIMPAN`, jangan membuat API Key baru: periksa pemilihan workspace dan refresh browser. Jika `TIDAK ADA`, cari tahu lebih dulu apakah Docker volume/proyek berubah; **jangan jalankan `docker compose down -v` atau regenerasi kunci enkripsi**. Perintah ini hanya membaca metadata, bukan nilai API Key.

## Download All BOM (worker read-only)

Sesudah Test Connection semua model utama berstatus `readable`, buka dashboard dan cari bagian **Download All BOM**.

1. Pilih workspace yang koneksi Odoo-nya sudah disimpan.
2. Klik **Download All BOM**, lalu konfirmasi. API segera mengembalikan `202 Accepted` beserta ID pekerjaan.
3. Docker service `worker` mengambil job dari SQLite. Antarmuka menampilkan status `queued/running/completed/incomplete/failed`, model yang diproses, record yang sudah diunduh, dan riwayat.
4. Tunggu status **Selesai — snapshot aktif**. Ringkasan snapshot lokal menampilkan jumlah record tiap model. **Tidak ada perubahan pada Odoo**.
5. Jika gagal, snapshot yang sebelumnya aktif **tidak diganti**. Periksa status dan `docker compose logs -f worker`, lalu ulangi download sesudah memperbaiki penyebabnya.

**Cakupan awal:** semua record yang terlihat oleh akun API dalam model inti `mrp.bom`, `mrp.bom.line`, `product.product`, `product.template`, dan `uom.uom`; model tambahan `res.company`, `mrp.bom.byproduct`, serta `mrp.routing.workcenter` diambil jika tersedia/diizinkan. `active_test=false` digunakan agar arsip yang boleh diakses tidak terlewat. Field yang diambil dipilih dari daftar aman menggunakan `fields_get`. ID asli Odoo dan payload JSON hasil unduhan dipertahankan dalam snapshot.

**Validasi:** dibandingkan count server sebelum/sesudah, jumlah baris yang diunduh, watermark `write_date` bila tersedia, keunikan ID, serta relasi BOM-line, komponen-produk, template produk, dan UOM. Jika data berubah saat download, proses ditandai `incomplete` dan dapat diulang. Pengecekan ini **bukan backup atomik Odoo** dan tidak menjamin tidak ada perubahan selama setiap halaman diambil.

Untuk jumlah data besar, pengunduhan dilakukan dengan cursor `id` dan batch 250 baris per request; koneksi HTTPS tidak melakukan redirect. Hanya satu job aktif per workspace; worker diproses serial. Snapshot staging tidak dapat diakses pengguna sampai lolos validasi. SQLite tetap berada dalam Docker volume `bom_data` dan tidak pernah dimasukkan ke repository.

### API monitoring

Semua endpoint membutuhkan login; tindakan memulai sync memerlukan Administrator atau Workspace Admin.

| Method | Path | Fungsi |
|---|---|---|
| `POST` | `/api/v1/workspaces/{wid}/sync/runs` | Antrekan full download, HTTP 202 |
| `GET` | `/api/v1/workspaces/{wid}/sync/runs` | Riwayat 20 job terakhir |
| `GET` | `/api/v1/workspaces/{wid}/sync/runs/{run_id}` | Progres per model dan error aman |
| `GET` | `/api/v1/workspaces/{wid}/sync/snapshots/current` | Statistik snapshot aktif |

### Upgrade dari versi sebelumnya

Di server Debian yang **sudah memiliki Administrator dan workspace**:

```bash
cd ~/odoo-tools-
git pull --ff-only origin main
docker compose up -d --build
docker compose ps
```

Tidak perlu membuat Administrator ulang, tidak perlu mengganti `BOM_ENCRYPTION_KEY`, dan **jangan menggunakan `docker compose down -v`**. Kunci enkripsi yang sudah tersimpan harus tetap sama agar API Key Odoo yang ada masih dapat didekripsi.

Jika job berjalan lama, lihat:

```bash
docker compose logs -f worker
```

Port browser tetap `http://localhost:700/` pada host; Docker memetakan localhost port `700` ke container `8080`.

## Edit BOM Lokal melalui Excel

**Sudah diimplementasikan.** Sesudah Download All BOM selesai, login dan buka panel **Excel Export–Import BOM Lokal** pada dashboard.

1. Pilih workspace dengan snapshot aktif.
2. Klik **Download Excel BOM**. Workbook berisi tiga sheet: `BOM Lines` (data dan kolom edit), `Instructions` (petunjuk), dan `Product Catalog` (Product ID/SKU/nama untuk memilih pengganti).
3. Di sheet `BOM Lines`, **hanya ubah kolom F** `new_component_product_id` dan/atau **kolom H** `new_qty`. Komponen dan qty boleh tetap sama atau berubah secara independen. **Jangan mengubah kolom ID, menambah atau menghapus baris.**
4. Klik **Upload & Preview Perubahan**, pilih file `.xlsx` hasil edit. Backend memeriksa seluruh baris terhadap working BOM terkini dan menampilkan ringkasan perubahan, maksimal 100 contoh dan peringatan. File yang tidak valid **ditolak seluruhnya**.
5. Setelah memeriksa perubahan, klik **Apply to Local**. Perubahan diterapkan **atomik pada versi kerja BOM di SQLite**, tanpa menyentuh snapshot asli maupun server Odoo.
6. Download Excel lagi jika ingin melihat nilai working terkini; kolom `original_...` mencerminkan versi lokal saat ekspor. Riwayat penerapan lokal dan jumlah baris dengan override terlihat pada panel.

**Contoh:** Product ID 123 qty 2 → Product ID 123 qty 5 (qty saja); Product ID 123 qty 2 → Product ID 456 qty 2 (komponen saja); Product ID 123 qty 2 → Product ID 456 qty 5 (keduanya).

### Keamanan dan pembatasan

- **Administrator**, **Workspace Admin** dan **Editor** dapat upload, preview, dan Apply to Local sesuai workspace; Viewer hanya dapat download Excel.
- Jika file telah usang karena snapshot baru atau perubahan working BOM sejak ekspor, preview/Apply ditolak; lakukan export ulang.
- Kolom identitas diverifikasi terhadap SQLite. Nilai komponen tujuan wajib ada di snapshot workspace yang sama. Qty harus lebih dari nol dan maksimal 12 angka desimal.
- Perbedaan company, produk arsip, atau default UOM akan ditampilkan sebagai peringatan. Perbedaan kategori UOM yang diketahui **ditolak**, tidak ada konversi qty otomatis.
- File Excel dibatasi 12 MB, maksimum 100.000 BOM Lines, formula pada sheet `BOM Lines` ditolak.
- Preview berlaku 60 menit dan hanya dapat diterapkan sekali oleh user yang meng-uploadnya.
- Edit lokal tersimpan pada `local_bom_working` dan audit batch `local_excel_apply_batches`/`local_excel_change_rows`. Snapshot `sync_records` tidak pernah diubah oleh modul ini.
- Proses Excel **tidak mengakses Odoo**. Mekanisme update ke Odoo **belum dibahas** dan tidak termasuk fitur ini.

### API Excel lokal

| Method | Endpoint | Keterangan |
|---|---|---|
| GET | `/api/v1/workspaces/{wid}/excel/export` | Unduh template BOM lokal `.xlsx` |
| POST | `/api/v1/workspaces/{wid}/excel/preview` | Upload multipart `file`, validasi + preview |
| POST | `/api/v1/workspaces/{wid}/excel/preview/{token}/apply` | Apply perubahan ke SQLite lokal |
| GET | `/api/v1/workspaces/{wid}/excel/working-summary` | Status overlay dan riwayat penerapan |

### Upgrade Debian (tanpa menghilangkan data)

```bash
cd ~/odoo-tools-
git pull --ff-only origin main
docker compose up -d --build
docker compose ps
```

**Jangan** menjalankan `docker compose down -v` dan **jangan** mengganti `BOM_ENCRYPTION_KEY`. Administrator, workspace, API Key, serta snapshot lokal yang sudah ada tetap dipertahankan dalam Docker volume.

### Modul yang sudah ada

- Login/logout dengan sesi HttpOnly cookie, CSRF token, Argon2id password hash, expiry, dan pembatasan percobaan login.
- Administrator adalah **superuser aplikasi** pada fase development: dapat mengelola user dan semua workspace tanpa harus memiliki membership.
- User biasa hanya dapat mengakses workspace yang ditetapkan; role: `workspace_admin`, `editor`, `viewer`.
- Administrator: membuat user, menampilkan user, mengganti nama/status dan status admin, reset password (user harus mengganti saat masuk kembali).
- Setiap user dapat mengganti password sendiri; sesi aktif dicabut saat perubahan password.
- Workspace dapat dibuat Administrator; membership dapat ditetapkan Administrator (implementasi awal backend API).
- SQLite berada di volume Docker `bom_data`, bukan di repository; log audit merekam tindakan penting.
- `/docs` menampilkan endpoint koneksi Odoo dan sync per workspace. Worker melakukan unduhan read-only dengan validasi snapshot.

### Pengujian lokal (tanpa Docker)

```bash
python -m pip install -e '.[dev]'
BOM_DB_PATH=/tmp/odoo-bom-dev.sqlite3 uvicorn odoo_tools.main:app --reload --port 700
pytest -q
```

### Perintah operasi

```bash
docker compose logs -f api worker
docker compose down        # stop, data tetap tersimpan
```

**Jangan gunakan `docker compose down -v`** kecuali sengaja ingin menghapus database SQLite dan semua akun.

### Batas keamanan

- Dev localhost memakai cookie `Secure=false` agar dapat diakses lewat HTTP. Untuk akses melalui HTTPS, atur `BOM_COOKIE_SECURE=true` dan reverse proxy dengan autentikasi/kontrol akses yang sesuai.
- Jangan expose port ke internet tanpa HTTPS, pengamanan jaringan dan review keamanan.
- Jangan commit `.env`, API key Odoo, file SQLite, atau backup data perusahaan.
- Hak Administrator hanya berlaku dalam aplikasi, **tidak melewati izin Odoo**. Modul write-back tidak tersedia pada tahap ini.

## Berikutnya

Gunakan modul Excel lokal untuk menguji editing komponen/qty pada satu atau beberapa BOM Lines. Setelah berhasil, kembangkan tampilan pencarian/Where Used terhadap **working BOM lokal**, tanpa melibatkan update Odoo.
