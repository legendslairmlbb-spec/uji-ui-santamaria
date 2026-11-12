# Uji UI/UX otomatis: LLM Perancang + Pelaksana + LLM Evaluator

Untuk aplikasi **Santa Maria**, **Yayasan Santa Maria**, dan **Wasiat** (login Google) di HP Android (USB debugging).

```
observasi awal (HP) ─► LLM Perancang ─► Pelaksana (Appium) ─► LLM Evaluator ─► yakin? ─ya─► laporan
                            ▲                                                   │
                            └──────────── permintaan uji ulang yang spesifik ◄──┘ belum (maks. N putaran)
```

- **LLM Perancang** melihat screenshot bernomor + daftar elemen layar, lalu menulis rencana uji (JSON).
- **Pelaksana** (`agen_uji.py`) menjalankan rencana di HP: mengukur waktu respons, startup, metrik
  aksesibilitas/kerumitan per layar, screenshot, struktur layar (XML), dan rekaman layar. Hanya komponen ini
  yang memegang akun; LLM cuma melihat nama peran.
- **LLM Evaluator** menilai semuanya (termasuk frame rekaman saat aplikasi memuat), menulis temuan berbasis
  bukti, dan memberi nilai keyakinan. Di bawah ambang → ia menulis permintaan uji ulang yang spesifik untuk
  Perancang. Di atas ambang → laporan dibuat.

## Struktur repo

| File | Fungsi |
|---|---|
| `orkestrator.py` | dua agen LLM, loop uji ulang, laporan |
| `agen_uji.py` | pelaksana Appium (bisa juga dipakai sendiri) |
| `akun.contoh.json` | templat akun uji per aplikasi; salin menjadi `akun.json` |
| `llm.contoh.json` | templat koneksi 9router/LLM; salin menjadi `llm.json` lalu isi `api_key` |
| `skenario.json` | daftar sesi uji (aplikasi, peran, tujuan), ambang keyakinan, maks. putaran |
| `prompt_perancang.md`, `prompt_evaluator.md` | system prompt kedua agen |
| `n8n_workflow_uji_ui.json` | workflow n8n (import ke n8n) |
| `contoh_rencana.json` | contoh format rencana uji |
| `tests/` | simulator HP + server LLM tiruan + uji ujung-ke-ujung |
| `tools/buat_workflow.py` | pembuat ulang `n8n_workflow_uji_ui.json` |

`akun.json` dan `llm.json` berisi kata sandi dan API key, jadi tidak ikut di-commit (lihat `.gitignore`).

## Persiapan (sekali saja)

```
pip install -U Appium-Python-Client pillow imageio-ffmpeg
```
```
copy akun.contoh.json akun.json
```
```
copy llm.contoh.json llm.json
```
Lalu isi email/HP/kata sandi akun uji di `akun.json` dan `api_key` 9router di `llm.json`.
Perangkat lunak lain: Appium 3 + driver UiAutomator2, Android platform-tools (adb), HP Android dengan USB
debugging, 9router (atau endpoint OpenAI-compatible lain yang bisa membaca gambar), dan n8n 2.x (opsional).

## Wasiat: login dengan Google

Wasiat memakai Google SSO, jadi tidak ada email/sandi di akun.json untuk aplikasi ini.

- Akun Google uji harus **sudah masuk di HP** (Setelan > Akun > Tambah akun > Google). Skrip tidak pernah
  mengetik kata sandi Google.
- Isi `google_akun` (bagian `wasiat` di akun.json) dengan email akun Google uji itu, terutama kalau HP punya
  beberapa akun Google. Kosong = akun pertama di daftar pemilih akun.
- Skrip menekan tombol yang labelnya memuat "Google", memilih akun, lalu menekan "Lanjutkan"/"Izinkan" kalau
  muncul layar persetujuan. Kalau label tombolnya lain, cek dengan `python agen_uji.py lihat --app wasiat --buka`
  lalu isi `label_tombol_sso`.
- Layar pemilih akun Google tidak dipakai sebagai bukti dan tidak dikirim ke LLM.

## Izin aplikasi

Aplikasi yang mewajibkan semua izin sebelum masuk menu utama ditangani dua lapis:

1. **Sebelum uji** (dan setiap kali data aplikasi dihapus), semua izin yang diminta aplikasi diberikan lewat adb:
   izin runtime (notifikasi, kamera, lokasi termasuk "sepanjang waktu", berkas, dll.), izin khusus lewat app-ops
   (tampil di atas aplikasi lain, akses semua file, alarm tepat waktu, pasang aplikasi, akses penggunaan), dan
   pengecualian penghemat baterai. Daftarnya tercatat di `ringkasan.json` (`izin_otomatis`).
2. **Kalau aplikasi tetap membuka Pengaturan sistem**, pelaksana menyalakan sakelar/opsi izin yang sesuai, kembali
   ke aplikasi, dan menandai langkah itu (`keluar_ke_pengaturan`) supaya evaluator menilainya sebagai temuan UX.

HP **Xiaomi/Redmi/POCO**: aktifkan Opsi pengembang → **USB debugging (Security settings)**; tanpa ini adb tidak
boleh memberi izin. Untuk menilai alur permintaan izin aplikasi apa adanya, set `"beri_izin_otomatis": false` di
`skenario.json` (atau di skenario tertentu).

## Menjalankan dari terminal

Terminal 1 (biarkan terbuka; tutup sesi Appium Inspector kalau sedang terbuka):

```
appium
```

Terminal 2:

```
cd <folder repo ini>
```
```
python orkestrator.py cek-llm
```
Hasil harus `"status": "ok"` (`json_ok` dan `gambar_ok` = true). Lalu satu sesi uji:
```
python orkestrator.py siklus --skenario 0
```
atau semua skenario sekaligus (plus ringkasan gabungan):
```
python orkestrator.py semua
```
Sesi bebas tanpa skenario.json:
```
python orkestrator.py siklus --app wasiat --peran owner --tujuan "Evaluasi menu utama owner"
```

## Menjalankan lewat n8n

1. Jalankan n8n dengan node Execute Command diaktifkan (n8n 2.x mematikannya secara bawaan):
   ```
   $env:NODES_EXCLUDE='[]'; n8n start
   ```
2. Buka http://localhost:5678 → **Create workflow** → menu **...** → **Import from File** →
   `n8n_workflow_uji_ui.json`.
3. Cek node **Konfigurasi** (lokasi `python` dan `orkestrator.py`), lalu **Execute workflow**.
   Untuk menjalankan sebagian skenario saja, edit node **Pecah per skenario** (ada contohnya).

Alurnya terlihat di kanvas: Mulai sesi → LLM Perancang → Pelaksana → LLM Evaluator → *Perlu uji ulang?*
(ya: kembali ke LLM Perancang; tidak: Buat laporan) → skenario berikutnya → Ringkasan semua aplikasi.

## Hasil

`C:\Users\<nama>\.n8n-files\uji-ui\`

```
sesi_<waktu>_<aplikasi>_<peran>\
  laporan.html   ← buka ini (temuan + bukti screenshot, waktu, metrik per layar, cakupan)
  laporan.md / laporan.json
  observasi\     ← run awal (P0): screenshot, XML, ringkasan.json, rekaman.mp4
  putaran_1\     ← rencana.json, uji\ (hasil pelaksana), evaluasi.json, frame\, llm_*.json (isi percakapan LLM)
  putaran_2\ ...
ringkasan_<waktu>.html   ← tabel semua aplikasi (perintah semua / n8n)
```

Ref di laporan: `P1.4` = putaran 1 langkah 4; `L2` = layar unik ke-2; `L2-A1` = masalah A1 di layar L2.
Kode masalah otomatis: **A** tanpa label, **T** area sentuh < 48 dp, **H** label hanya dari hint,
**B** tombol tidak bisa diaktifkan TalkBack, **K** perkiraan kontras teks < 4,5:1.

## Langkah terpisah (yang dipanggil n8n)

```
python orkestrator.py mulai --skenario 0
python orkestrator.py rancang --sesi <folder sesi>
python orkestrator.py jalankan --sesi <folder sesi>
python orkestrator.py evaluasi --sesi <folder sesi>
python orkestrator.py laporan --sesi <folder sesi>
```

Status sesi tersimpan di `sesi.json`, jadi kalau proses berhenti di tengah (LLM kena limit, HP terlepas),
lanjutkan dari langkah yang gagal dengan perintah di atas.

## Mengatur perilaku

- `skenario.json` → `ambang_yakin` (bawaan 0.75), `maks_putaran` (3), `rekam` (true), `beri_izin_otomatis` (true),
  `boleh_ubah_data` (false: tombol hapus/simpan/kirim/bayar/keluar/WhatsApp/telepon diblokir pelaksana).
- `llm.json` → model per agen, `model_cadangan` (dipakai otomatis kalau model utama gagal/kena limit),
  `reasoning_effort_*`, jumlah dan lebar gambar.
- Tes manual satu rencana tanpa LLM: `python agen_uji.py jalankan contoh_rencana.json --rekam`.

## Masalah umum

| Gejala | Penyebab / solusi |
|---|---|
| `tidak bisa menyambung ke http://localhost:20128/v1` | 9router belum jalan |
| `gagal menyambung ke Appium` | `appium` belum jalan, atau sesi Inspector masih terbuka |
| `HP tidak terdeteksi oleh adb` | cek `adb devices`, izinkan USB debugging di HP |
| `aplikasi ... tidak terpasang di HP` | pasang aplikasinya atau hapus skenarionya |
| `observasi gagal di langkah 'login_...'` | lihat screenshot yang disebut; cek akun/sandi di akun.json |
| `file akun tidak ditemukan` | salin `akun.contoh.json` menjadi `akun.json` |
| `Google meminta kata sandi` (Wasiat) | masukkan dulu akun Google uji di HP lewat Setelan > Akun |
| `tombol masuk dengan Google tidak ditemukan` | isi `label_tombol_sso` di akun.json sesuai label tombolnya |
| `izin yang gagal diberikan` di log | Xiaomi/Redmi/POCO: aktifkan USB debugging (Security settings) |
| `gambar_ok: false` di cek-llm | model tidak membaca gambar; ganti model di llm.json |
| Login dilewati padahal akun berbeda | hapus `.n8n-files\uji-ui\status_login.json` (dibuat ulang otomatis) |

Catatan metode: waktu respons diukur lewat Appium (termasuk overhead ±0,1–0,5 detik), jadi bandingkan antar-run
di HP yang sama. Metrik aksesibilitas dihitung dari struktur elemen, bukan pengujian TalkBack; temuan LLM
sebaiknya diverifikasi manual sebelum dijadikan keputusan.

## Pengujian kode (tanpa HP dan tanpa LLM)

`tests/` berisi simulator HP (memakai struktur layar login Santa Maria asli dan aplikasi Wasiat tiruan dengan
pemilih akun Google) serta server OpenAI-compatible tiruan pengganti 9router.

```
python tests/test_agen_uji.py
```
```
python tests/test_orkestrator.py
```

Yang diuji antara lain: validasi rencana dan pemblokiran tombol berisiko, login form dan Google SSO, pemakaian
ulang sesi login, pemberian izin lewat adb dan penanganan halaman Pengaturan sistem, perbaikan rencana yang ditolak validator, coba ulang saat HTTP 429, loop evaluator -> uji
ulang -> laporan, sensor data akun di teks dan gambar, frame rekaman untuk langkah lambat, serta laporan HTML/MD.
Workflow n8n juga sudah dijalankan penuh di n8n 2.47.3 memakai `tests/orkestrator_sim.py`.
