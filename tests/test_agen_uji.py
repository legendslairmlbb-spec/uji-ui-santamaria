"""Uji agen_uji.py dengan simulator HP (tanpa Appium/HP sungguhan).

Simulator mengevaluasi selector UiAutomator dan XPath yang dipakai skrip terhadap XML layar:
layar login asli Santa Maria (dari Inspector), plus beranda, laporan, dialog izin, dan launcher tiruan.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="ujiui_")
os.environ["UJI_UI_FOLDER"] = TMP
AKAR = Path(__file__).resolve().parent.parent
os.environ["UJI_UI_AKUN"] = str(AKAR / "tests" / "fixtures" / "akun_uji.json")
sys.path.insert(0, str(AKAR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agen_uji as ag  # noqa: E402
import simulator  # noqa: E402

SIM = simulator.pasang(ag)
PASSWORD = simulator.PASSWORD


def jalankan_cli(*argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    lama_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            kode = ag.main(list(argv))
    finally:
        sys.stdin = lama_stdin
    baris = [b for b in out.getvalue().splitlines() if b.strip()]
    return kode, baris, err.getvalue()


def json_terakhir(baris):
    return json.loads(baris[-1])


lulus = 0


def cek(kondisi, pesan):
    global lulus
    assert kondisi, pesan
    lulus += 1


# ---------------------------------------------------------------- 1. skema
kode, baris, _ = jalankan_cli("skema", "--app", "wasiat")
teks = "\n".join(baris)
cek(kode == 0 and '"wasiat" (Wasiat): pengguna_google' in teks, "skema harus mencantumkan peran wasiat")
cek(simulator.PASSWORD not in teks and "@contoh.test" not in teks, "skema tidak boleh membocorkan akun")
print("1. skema: OK")

# ---------------------------------------------------------------- 2. validasi
rencana_salah = {"aplikasi": "santamaria", "langkah": [
    {"aksi": "terbang"}, {"aksi": "login", "peran": "presiden"}, {"aksi": "tap"},
    {"aksi": "ketik", "target": {"kelas": "EditText"}, "teks": PASSWORD}, {"aksi": "gulir", "arah": "serong"}]}
kode, baris, _ = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana_salah))
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "rencana_tidak_valid" and len(h["kesalahan"]) == 5, f"validasi: {h}")
cek(any("kata sandi" in k for k in h["kesalahan"]), "harus menolak kata sandi di rencana")
kode, baris, _ = jalankan_cli("jalankan", "-", stdin="{bukan json")
cek(json_terakhir(baris)["status"] == "rencana_tidak_valid", "JSON rusak harus ditolak dengan rapi")
print("2. validasi rencana: OK ->", h["kesalahan"][:2], "...")

# ---------------------------------------------------------------- 3. contoh rencana (owner, HP masih login akun lain)
kode, baris, err = jalankan_cli("jalankan", str(AKAR / "contoh_rencana.json"), "--rekam")
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "selesai" and h["berhasil"], f"contoh rencana harus berhasil: {h}\n{err}")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
nama_langkah = [l["langkah"] for l in r["langkah"]]
cek(nama_langkah == ["buka_aplikasi", "login_owner_isi_identitas", "login_owner_isi_sandi",
                     "login_owner_tekan_masuk", "gulir_beranda", "beranda_bawah"], f"urutan langkah: {nama_langkah}")
cek(("adb", "shell pm clear id.co.santamaria.app") in SIM.panggilan, "harus pm clear karena masih login akun lain")
cek(("klik", "Izinkan") in SIM.panggilan, "dialog izin notifikasi harus ditekan Izinkan")
cek(SIM.login_sebagai == "dummy.owner@contoh.test", "harus login sebagai owner")
cek(r["langkah"][0]["startup"] == {"total_time_ms": 812, "launch_state": "COLD"}, "startup harus terbaca")
masuk = r["langkah"][3]
cek(450 <= masuk["respon_ms"] <= 2500, f"respon login harus sekitar 0,5 detik: {masuk['respon_ms']}")
cek(r["rekaman"] == "rekaman.mp4" and (Path(h["folder"]) / "rekaman.mp4").exists(), "rekaman harus tersimpan")
cek(any(e["label"] == "Item 6" for e in r["layar_akhir"]), "layar akhir harus versi beranda yang sudah digulir")
akhir = r["langkah"][-1]["metrik"]["masalah"]
cek(len(akhir["target_sentuh_di_bawah_48dp"]) == 1 and len(akhir["interaktif_tanpa_label"]) == 1,
    "ikon kecil tanpa label di beranda harus terdeteksi")
semua_file = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in Path(h["folder"]).glob("*.json"))
cek(PASSWORD not in semua_file, "kata sandi tidak boleh tersimpan di file hasil")
print("3. contoh rencana owner: OK -> respon login", masuk["respon_ms"], "ms;", len(r["langkah"]), "langkah")

# ---------------------------------------------------------------- 4. lihat sebagai consumer (tab Konsumen)
kode, baris, err = jalankan_cli("lihat", "--app", "santamaria", "--peran", "consumer")
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "selesai", f"lihat consumer: {h}\n{err}")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
cek(any(l["langkah"] == "login_pilih_tab_konsumen" and l["berhasil"] for l in r["langkah"]), "tab Konsumen harus dipilih")
cek(SIM.login_sebagai == "dummy.consumer@contoh.test", "harus login sebagai consumer")
cek(h["layar"]["elemen"] and h["layar"]["screenshot"].endswith(".png"), "lihat harus mengembalikan daftar elemen")
print("4. lihat consumer: OK ->", [e["label"] for e in h["layar"]["elemen"][:4]])

# ---------------------------------------------------------------- 5. tap + tunggu_muncul, kembali, koordinat
rencana = {"aplikasi": "santamaria", "judul": "buka laporan", "peran": "consumer", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka", "cold_start": False},
    {"aksi": "tap", "nama": "buka_laporan", "target": {"kelas": "Button", "label": "Laporan"},
     "tunggu_muncul": {"label": "Laporan Keuangan"}},
    {"aksi": "kembali", "nama": "kembali_ke_beranda", "tunggu_muncul": {"label": "Beranda"}},
    {"aksi": "tap", "nama": "pesanan", "target": {"label_mengandung": "Pesanan"}},
    {"aksi": "tap", "nama": "ikon_pojok", "target": {"koordinat": [1140, 180]}},
    {"aksi": "tunggu", "nama": "jeda", "detik": 0.2},
]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
cek(h["status"] == "selesai", f"rencana laporan: {h}\n{err}")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
cek(r["langkah"][1]["respon_ms"] is not None and r["langkah"][2]["berhasil"], "tap & kembali harus terukur")
cek(("tap", [(1140, 180)]) in SIM.panggilan, "tap koordinat harus dikirim")
print("5. tap/kembali/koordinat: OK -> respon buka_laporan", r["langkah"][1]["respon_ms"], "ms")

# ---------------------------------------------------------------- 6. langkah gagal
rencana = {"aplikasi": "santamaria", "judul": "gagal", "langkah": [
    {"aksi": "tap", "nama": "menu_tidak_ada", "target": {"label": "Menu Rahasia"}, "batas_cari_detik": 1},
    {"aksi": "tangkap", "nama": "tidak_boleh_jalan"}]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "gagal_langkah" and h["langkah_gagal"]["langkah"] == "menu_tidak_ada",
    f"langkah gagal harus dilaporkan: {h}")
cek(h["jumlah_langkah"] == 1 and h["langkah_gagal"]["screenshot"], "rencana berhenti dan bukti tersimpan")
print("6. langkah gagal: OK ->", h["langkah_gagal"]["galat"])

# ---------------------------------------------------------------- 7. tap berisiko ditolak saat validasi
rencana = {"aplikasi": "santamaria", "langkah": [{"aksi": "tap", "target": {"label": "Keluar"}}]}
kode, baris, _ = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
cek(h["status"] == "rencana_tidak_valid" and "diblokir" in h["kesalahan"][0], f"tap Keluar harus diblokir: {h}")
print("7. blokir statis: OK ->", h["kesalahan"][0][:70])

# ---------------------------------------------------------------- 8. tap koordinat ke tombol berisiko diblokir saat jalan
rencana = {"aplikasi": "santamaria", "peran": "consumer", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka", "cold_start": False},
    {"aksi": "tap", "nama": "pojok_bawah", "target": {"koordinat": [600, 2490]}}]}
kode, baris, _ = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
cek(h["status"] == "gagal_langkah" and "diblokir" in h["langkah_gagal"]["galat"], f"tap koordinat Keluar: {h}")
cek(SIM.login_sebagai == "dummy.consumer@contoh.test", "tidak boleh benar-benar logout")
print("8. blokir saat jalan: OK ->", h["langkah_gagal"]["galat"][:70])

# ---------------------------------------------------------------- 9. login peran yang sama dipakai ulang
n_awal = len(SIM.panggilan)
rencana = {"aplikasi": "santamaria", "judul": "ulang", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"}, {"aksi": "login", "nama": "login", "peran": "consumer"},
    {"aksi": "tap", "nama": "buka_laporan", "target": {"label": "Laporan"}, "tunggu_muncul": {"label": "Laporan Keuangan"}}]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana), )
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
nama_langkah = [l["langkah"] for l in r["langkah"]]
cek(h["status"] == "selesai" and nama_langkah == ["buka", "login_sudah_login", "buka_laporan"], f"login ulang: {nama_langkah}\n{err}")
cek(not any(p[0] == "adb" and "pm clear" in p[1] for p in SIM.panggilan[n_awal:]), "tidak boleh pm clear")
cek(all("t_mulai_ms" in l and l["aksi"] for l in r["langkah"]), "langkah harus punya t_mulai_ms & aksi")
cek(r["langkah"][2]["respon_ms"] >= 1100, f"jeda laporan 1,2 s harus terukur: {r['langkah'][2]['respon_ms']}")
print("9. login dipakai ulang: OK ->", nama_langkah, "respon laporan", r["langkah"][2]["respon_ms"], "ms")

# ---------------------------------------------------------------- 10. Wasiat: login Google SSO
rencana = {"aplikasi": "wasiat", "judul": "sso", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"}, {"aksi": "login", "nama": "login", "peran": "pengguna_google"},
    {"aksi": "tangkap", "nama": "beranda"}]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
nama_langkah = [l["langkah"] for l in r["langkah"]]
cek(h["status"] == "selesai" and nama_langkah == ["buka", "login_google", "beranda"], f"SSO: {nama_langkah} {h}\n{err[-1500:]}")
sso = r["langkah"][1]
cek(sso.get("privasi") is True and sso["respon_ms"] >= 1000, f"langkah SSO: {sso}")
cek(SIM.wasiat_login == "uji.wasiat@gmail.com", "tanpa google_akun -> akun pertama")
isi_folder = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in Path(h["folder"]).glob("*.*") if f.suffix in (".json", ".xml"))
cek("@gmail.com" not in isi_folder, "layar pemilih akun Google tidak boleh tersimpan saat login berhasil")
print("10. Wasiat Google SSO: OK -> respon", sso["respon_ms"], "ms")

# ---------------------------------------------------------------- 11. sesi Google dipakai ulang
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
r = json.loads(Path(json_terakhir(baris)["ringkasan"]).read_text(encoding="utf-8"))
cek([l["langkah"] for l in r["langkah"]] == ["buka", "login_sudah_login", "beranda"], f"pakai ulang SSO: {r['langkah']}")
print("11. sesi Google dipakai ulang: OK")

# ---------------------------------------------------------------- 12. google_akun memilih akun tertentu / salah akun
cfg = json.loads(Path(os.environ["UJI_UI_AKUN"]).read_text(encoding="utf-8"))
ulang = {"aplikasi": "wasiat", "judul": "sso_akun", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"}, {"aksi": "reset_aplikasi", "nama": "reset"},
    {"aksi": "buka_aplikasi", "nama": "buka_lagi"}, {"aksi": "login", "nama": "login", "peran": "pengguna_google"}]}
for email, harap in (("lain@gmail.com", "selesai"), ("tidakada@gmail.com", "gagal_langkah")):
    cfg["aplikasi"]["wasiat"]["akun"]["pengguna_google"]["google_akun"] = email
    f_akun = Path(TMP) / "akun_sso.json"
    f_akun.write_text(json.dumps(cfg), encoding="utf-8")
    kode, baris, err = jalankan_cli("--akun", str(f_akun), "jalankan", "-", stdin=json.dumps(ulang))
    h = json_terakhir(baris)
    cek(h["status"] == harap, f"google_akun={email}: {h}\n{err[-1200:]}")
    if harap == "selesai":
        cek(SIM.wasiat_login == "lain@gmail.com", "harus memilih akun yang diatur")
cek("tidak ada di pemilih akun" in h["langkah_gagal"]["galat"] and "tidakada" not in h["langkah_gagal"]["galat"],
    f"pesan akun salah: {h['langkah_gagal']}")
print("12. pilih akun Google tertentu + akun salah: OK ->", h["langkah_gagal"]["galat"][:60])

print(f"\nSEMUA {lulus} CEK LULUS (hasil uji di {TMP})")
