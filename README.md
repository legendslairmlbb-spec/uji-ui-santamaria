# Uji UI/UX otomatis: LLM Perancang + Pelaksana + LLM Evaluator

Untuk aplikasi **Santa Maria**, **Yayasan Santa Maria**, dan **Wasiat** (login Google) di HP Android (USB debugging).

```
observasi awal ─► penjelajah otomatis ─► LLM Evaluator ─► LLM Perancang ─► Pelaksana ─► LLM Evaluator ─► ...
                  (semua elemen, tanpa LLM)                (uji mendalam + CRUD)          │
                                                                                       semua tercakup? ─ya─► pemulihan data ─► laporan
```

- **LLM Perancang** melihat screenshot bernomor + daftar elemen layar, lalu menulis rencana uji (JSON).
- **Pelaksana** (`agen_uji.py`) menjalankan rencana di HP: mengukur waktu respons, startup, metrik
  aksesibilitas/kerumitan per layar, screenshot, struktur layar (XML), dan rekaman layar. Hanya komponen ini
  yang memegang akun; LLM cuma melihat nama peran.
- **LLM Evaluator** menilai semuanya (termasuk frame rekaman saat aplikasi memuat dan log error aplikasi), menulis
  temuan berbasis bukti beserta langkah reproduksi, dan meminta uji lanjutan yang spesifik kepada Perancang.
- **Penjelajah otomatis** (bagian dari pelaksana, tanpa LLM) mengetuk semua elemen yang bisa dijangkau di setiap
  layar. Ini yang membuat satu run mencakup semua fitur (lihat bagian Overtesting).

## Struktur repo

| File | Fungsi |
|---|---|
| `orkestrator.py` | dua agen LLM, loop uji ulang, laporan |
| `agen_uji.py` | pelaksana Appium (bisa juga dipakai sendiri) |
| `akun.contoh.json` | templat akun uji per aplikasi; salin menjadi `akun.json` |
| `llm.contoh.json` | templat koneksi 9router/LLM; salin menjadi `llm.json` lalu isi `api_key` |
| `skenario.json` | daftar sesi uji (aplikasi, peran, tujuan) dan pengaturan: mode overtesting, CRUD, batas putaran/waktu |
| `aplikasi.json` | perilaku per aplikasi: nama di HP (`label_hp`) dan langkah melewati halaman izin aplikasi |
| `prompt_perancang.md`, `prompt_evaluator.md` | system prompt kedua agen |
| `n8n_workflow_uji_ui.json` | workflow n8n (import ke n8n) |
| `contoh_rencana.json` | contoh format rencana uji |
| `tests/` | simulator HP (termasuk aplikasi demo), server LLM tiruan, uji ujung-ke-ujung |
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
boleh memberi izin. **Autostart** (izin khusus Xiaomi) tidak bisa lewat adb, jadi pelaksana memeriksanya lewat
tampilan: membuka daftar "Mulai otomatis", memakai fitur **Cari** dengan nama aplikasi (`label_hp` di
`aplikasi.json`), lalu menyalakan sakelarnya kalau masih mati (cadangan: halaman Info aplikasi). Kalau sudah
dinyalakan manual, hasilnya "sudah" dan tidak disentuh. Status (termasuk gagal) dicatat di `autostart.json`
sehingga tidak dicoba ulang setiap uji; ulangi pemeriksaan dengan `python agen_uji.py siapkan --paksa`.

Halaman izin **di dalam aplikasi** (Santa Maria meminta konfirmasi "Saya sudah mengaktifkan autostart" lalu
"Sudah aktif" dan "Periksa ulang") dilewati dengan langkah `gerbang_izin` di `aplikasi.json`, setelah buka aplikasi
atau login. Konfirmasi ini hilang setiap kali data aplikasi dihapus (ganti akun), jadi langkahnya diulang otomatis.

Persiapan HP sekali jalan untuk ketiga aplikasi (izin + Autostart):
```
python agen_uji.py siapkan
``` Untuk menilai alur permintaan izin aplikasi apa adanya, set `"beri_izin_otomatis": false` di
`skenario.json` (atau di skenario tertentu).

## Overtesting: semua fitur dalam satu run

`skenario.json` bawaan memakai `"mode": "overtesting"` dan `"boleh_ubah_data": true`. Satu sesi berjalan begini:

1. **Observasi awal** (buka aplikasi, login, layar pertama).
2. **Penjelajah otomatis** (putaran 1, tanpa LLM) mengetuk setiap elemen yang bisa dijangkau di setiap layar:
   menggulir untuk menemukan elemen di bawah, membuka lembar bawah/dialog, berpindah tab, lalu kembali lewat Back atau
   tab semula. Kalau tersesat, aplikasi dibuka ulang dan jalur ketukan diputar ulang. Item daftar sejenis (nama,
   pesanan, anomali) cukup diwakili dua contoh **kalau** keduanya menuju layar yang sama; menu yang menuju layar
   berbeda tetap diketuk semua; kisi seragam seperti tanggal di kalender juga cukup dua contoh. Satu layar dibatasi
   sekitar 1/8 anggaran ketukan, dan kalau 6 elemen berturut-turut tidak bisa diketuk, sisanya diserahkan ke LLM
   supaya penjelajah tidak macet di satu layar. Setiap 25 ketukan log menampilkan kemajuan. Crash, error di log,
   tombol yang tidak bereaksi, dan elemen yang tak terjangkau dicatat. Tombol pengubah data tidak diketuk di tahap
   ini.
3. **Uji mendalam oleh LLM** (putaran berikutnya), mengikuti daftar uji yang dibuat otomatis dari layar yang
   ditemukan:
   - validasi form: kirim kosong, format salah, teks panjang, emoji;
   - CRUD dengan pemulihan;
   - pencarian dan filter;
   - teks besar 1,3 dan 2,0;
   - tema gelap;
   - tanpa internet (mode pesawat);
   - ketuk cepat berulang;
   - konsistensi tombol Back.
4. **Evaluator** mencari bug, crash, data janggal, cacat visual, dan tampilan yang kurang enak dilihat. Layar yang
   belum pernah ia lihat didahulukan, supaya semua layar dinilai visual.
5. Sesi berhenti kalau semua elemen, tugas uji, dan layar sudah tercakup dan semua data uji sudah dipulihkan, atau
   kalau `maks_putaran` (14) / `batas_menit_sesi` (180) tercapai, atau 3 putaran tanpa kemajuan. Satu sesi bisa
   makan waktu 1-3 jam, tergantung jumlah menu.

**CRUD dan pemulihan data**
- Data yang dibuat uji memuat penanda unik (mis. `UJI-1112-153012`) di kolom teksnya. Di akhir rencana, langkah
  "pemulihan" menghapusnya lagi lalu memeriksa bahwa datanya sudah hilang.
- Hapus dan ubah status (setujui, tolak, akui, selesaikan, abaikan, batalkan) **hanya boleh pada data berpenanda**.
  Pelaksana memblokir tombol itu kalau baris atau halamannya tidak memuat penanda, jadi data lama tidak tersentuh.
- Isian lama yang diubah dikembalikan. Sakelar pengaturan (mis. Fingerprint) dikembalikan otomatis. Skala font,
  tema, dan jaringan HP juga dikembalikan di akhir setiap run.
- Kalau masih ada data uji yang belum terhapus, orkestrator menjalankan **putaran pemulihan** sebelum laporan.
  Laporan berisi tabel "Perubahan data dan pemulihan".
- **Selalu diblokir**, juga dalam mode CRUD:
  - pembayaran;
  - WhatsApp, telepon, SMS, dan email;
  - hapus atau nonaktifkan akun;
  - ganti kata sandi dan mengaktifkan verifikasi 2 langkah akun uji, karena keduanya bisa mengunci akun uji.
- Kalau punya akses server uji, isi `perintah_cadangan` dan `perintah_pulihkan` di `skenario.json` (perintah shell
  di laptop ini, mis. skrip dump/restore database). Keduanya dijalankan sebelum sesi dan sebelum laporan sebagai
  jaminan tambahan.

Hanya penjelajah otomatis, tanpa LLM dan tanpa mengubah data:
```
python agen_uji.py jelajahi --app santamaria --peran owner
```
Mode lama (berhenti begitu evaluator yakin, tanpa CRUD): ubah `mode` menjadi `"evaluasi"` dan `boleh_ubah_data`
menjadi `false`.

## Menjalankan dari terminal

Terminal 1 (biarkan terbuka; tutup sesi Appium Inspector kalau sedang terbuka):

```
appium
```

Terminal 2:

```
cd D:\uji-ui-santamaria
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
  putaran_1\     ← penjelajah otomatis: uji\jelajah.json (layar, status tiap elemen), ringkasan.json, screenshot layar baru
  putaran_2\ ... ← rencana.json, uji\ (hasil pelaksana), evaluasi.json, frame\, llm_*.json (isi percakapan LLM)
ringkasan_<waktu>.html   ← tabel semua aplikasi (perintah semua / n8n)
```

Ref di laporan: `P1.4` = putaran 1 langkah 4; `L2` = layar unik ke-2; `L2-A1` = masalah A1 di layar L2.
Temuan dikelompokkan: bug/crash/data janggal, cacat visual & estetika, aksesibilitas, performa, navigasi. Setiap
temuan punya langkah reproduksi. Kode masalah otomatis:
- **A** tanpa label;
- **T** area sentuh < 48 dp (elemen yang terpotong tepi layar tidak dinilai);
- **H** label hanya dari hint;
- **B** tombol tidak bisa diaktifkan TalkBack;
- **K** perkiraan kontras teks < 4,3:1 (yang tertutup bilah sistem tidak diukur);
- **D** pembungkus kembar tanpa label;
- **E** teks janggal (null, NaN, durasi atau tanggal mustahil);
- **O** elemen bertumpuk;
- **U** elemen tertutup dan tidak bisa digulir ke area yang terlihat.

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

- `skenario.json`:
  - `mode`: `overtesting` atau `evaluasi`;
  - `maks_putaran` (14) dan `batas_menit_sesi` (180; 0 = tanpa batas);
  - `jelajah_otomatis` (true) dengan `batas_ketukan_jelajah` (400) dan `batas_menit_jelajah` (45);
  - `boleh_ubah_data` (true = CRUD dengan pemulihan);
  - `perintah_cadangan` / `perintah_pulihkan`;
  - `ambang_yakin`, `rekam`, `beri_izin_otomatis`.
- `llm.json` → model per agen, `model_cadangan` (dipakai otomatis kalau model utama gagal/kena limit),
  `reasoning_effort_*`, jumlah dan lebar gambar.
- Tes manual satu rencana tanpa LLM: `python agen_uji.py jalankan contoh_rencana.json --rekam`.

## Masalah umum

| Gejala | Penyebab / solusi |
|---|---|
| `tidak bisa menyambung ke http://localhost:20128/v1` | 9router belum jalan |
| `gagal menyambung ke Appium` | `appium` belum jalan, atau sesi Inspector masih terbuka |
| `HP tidak terdeteksi oleh adb` | kabel USB (kabel data), buka kunci layar, USB debugging masih aktif; cek `adb devices` |
| `status 'unauthorized'` | buka kunci HP, ketuk Izinkan di dialog "Izinkan USB debugging?" |
| `HP terdeteksi tapi 'offline'` | cabut-pasang kabel; kalau masih, `adb kill-server` |
| `ada 2 perangkat terhubung ke adb` | cabut yang lain, atau isi `serial` di bagian `hp` pada aplikasi.json |
| `aplikasi ... tidak terpasang di HP` | pasang aplikasinya atau hapus skenarionya (muncul hanya kalau HP terhubung) |
| `observasi gagal di langkah 'login_...'` | lihat screenshot yang disebut; cek akun/sandi di akun.json |
| `file akun tidak ditemukan` | salin `akun.contoh.json` menjadi `akun.json` |
| `Google meminta kata sandi` (Wasiat) | masukkan dulu akun Google uji di HP lewat Setelan > Akun |
| `tombol masuk dengan Google tidak ditemukan` | isi `label_tombol_sso` di akun.json sesuai label tombolnya |
| `izin yang gagal diberikan` di log | Xiaomi/Redmi/POCO: aktifkan USB debugging (Security settings) |
| `gambar_ok: false` di cek-llm | model tidak membaca gambar; ganti model di llm.json |
| Login dilewati padahal akun berbeda | hapus `.n8n-files\uji-ui\status_login.json` (dibuat ulang otomatis) |
| `perubahan data GAGAL dipulihkan` | lihat tabel "Perubahan data dan pemulihan" di laporan; hapus manual data berpenanda UJI-... |
| Log berulang `jelajah_menuju_...` / `GAGAL ... tertutup` di satu dialog | versi lama menganggap lapisan gelap di belakang dialog/lembar bawah (label "Tutup") menutupi isinya; sudah diperbaiki, perbarui file |
| Penjelajah berhenti sebelum semua menu (`habis_anggaran`) | naikkan `batas_ketukan_jelajah` / `batas_menit_jelajah`; sisanya dikerjakan LLM |
| `tap diblokir: ... data buatan uji` | wajar: hapus/ubah status hanya untuk data berpenanda; Perancang akan membuat data uji dulu |

Catatan metode: waktu respons diukur lewat Appium (termasuk overhead ±0,1–0,5 detik), jadi bandingkan antar-run
di HP yang sama. Metrik aksesibilitas dihitung dari struktur elemen, bukan pengujian TalkBack; temuan LLM
sebaiknya diverifikasi manual sebelum dijadikan keputusan.

## Pengujian kode (tanpa HP dan tanpa LLM)

`tests/` berisi simulator HP (struktur layar login Santa Maria asli, aplikasi Wasiat tiruan dengan pemilih akun
Google, dan aplikasi demo dengan tab, lembar bawah, dialog kalender layar penuh beserta lapisan penutupnya, menu di bawah
bilah navigasi, crash, tombol mati, teks janggal, sakelar, dan CRUD) serta server OpenAI-compatible tiruan pengganti 9router.

```
python tests/test_agen_uji.py
```
```
python tests/test_orkestrator.py
```

Yang diuji antara lain:
- validasi rencana dan pemblokiran tombol berisiko;
- ketukan aman (gulir dulu, tidak pernah mengetuk bilah navigasi HP);
- pemulihan langkah gagal;
- penjelajah otomatis: semua layar demo, wakil daftar sejenis, crash dan log error, sakelar dikembalikan, tanpa
  mengubah data;
- CRUD berpenanda dengan pemulihan, pemblokiran hapus data lama, ganti kata sandi, dan WhatsApp;
- tema gelap dan jaringan dikembalikan di akhir uji;
- login form dan Google SSO, izin lewat adb, halaman Pengaturan sistem, Autostart Xiaomi, sambungan adb;
- sesi overtesting penuh: jelajah -> CRUD -> putaran pemulihan -> laporan;
- coba ulang saat HTTP 429, sensor data akun di teks dan gambar, frame rekaman, serta laporan HTML/MD.

Workflow n8n juga sudah dijalankan penuh di n8n 2.47.3 memakai `tests/orkestrator_sim.py`.
