# Odoo 19 BOM Tools

Fondasi aplikasi BOM lokal berbasis **FastAPI + SQLite + Docker Compose**. Tahap ini menyediakan login Administrator, pengelolaan akun, pemilihan workspace, dan pembatasan akses awal. **Belum terhubung ke Odoo dan belum mengunduh atau mengubah BOM.** Desain lengkap: [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

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
docker compose exec -it api python -m odoo_tools.cli create-admin --username administrator
```

Password dimasukkan lewat prompt, minimal 12 karakter. **Tidak ada password bawaan.** Perintah bootstrap akan menolak jika Administrator sudah ada. Setelah itu buka `http://localhost:700/` dan login. Ubah password dari menu akun jika diperlukan.

### Modul yang sudah ada

- Login/logout dengan sesi HttpOnly cookie, CSRF token, Argon2id password hash, expiry, dan pembatasan percobaan login.
- Administrator adalah **superuser aplikasi** pada fase development: dapat mengelola user dan semua workspace tanpa harus memiliki membership.
- User biasa hanya dapat mengakses workspace yang ditetapkan; role: `workspace_admin`, `editor`, `viewer`.
- Administrator: membuat user, menampilkan user, mengganti nama/status dan status admin, reset password (user harus mengganti saat masuk kembali).
- Setiap user dapat mengganti password sendiri; sesi aktif dicabut saat perubahan password.
- Workspace dapat dibuat Administrator; membership dapat ditetapkan Administrator (implementasi awal backend API).
- SQLite berada di volume Docker `bom_data`, bukan di repository; log audit merekam tindakan penting.
- `/docs` menampilkan spesifikasi endpoint. Layanan worker terpisah sudah tersedia sebagai kerangka; **sync Odoo belum diimplementasikan**.

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

Hubungkan API Odoo 19 JSON-2 menggunakan kredensial yang aman per workspace, kemudian buat worker sync read-only dengan staging, validasi dan aktivasi snapshot lokal.
