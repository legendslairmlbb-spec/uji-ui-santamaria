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
cek(nama_langkah == ["buka_aplikasi", "login_owner_isi_identitas", "login_owner_isi_sandi", "login_owner_tekan_masuk",
                     "gerbang_buka_autostart", "gerbang_centang_autostart", "gerbang_sudah_aktif",
                     "gerbang_periksa_ulang", "gulir_beranda", "beranda_bawah"], f"urutan langkah: {nama_langkah}")
cek(SIM.gerbang_ok and all(l["berhasil"] for l in r["langkah"]), "gerbang izin aplikasi harus terlewati")
cek(("adb", "shell pm clear id.co.santamaria.app") in SIM.panggilan, "harus pm clear karena masih login akun lain")
cek(("adb", "shell pm grant id.co.santamaria.app android.permission.POST_NOTIFICATIONS") in SIM.panggilan
    and ("klik", "Izinkan") not in SIM.panggilan, "izin notifikasi harus diberikan lewat adb, tanpa dialog")
cek(any("SYSTEM_ALERT_WINDOW" in x.get("appops", []) for x in r["izin_otomatis"]), f"app-ops: {r['izin_otomatis']}")
cek(SIM.login_sebagai == "dummy.owner@contoh.test", "harus login sebagai owner")
cek(r["langkah"][0]["startup"] == {"total_time_ms": 812, "launch_state": "COLD"}, "startup harus terbaca")
masuk = r["langkah"][3]  # login_owner_tekan_masuk
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
    {"aksi": "tangkap", "nama": "tetap_jalan"}]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "gagal_langkah" and h["langkah_gagal"]["langkah"] == "menu_tidak_ada",
    f"langkah gagal harus dilaporkan: {h}")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
cek(h["jumlah_langkah"] == 2 and h["jumlah_gagal"] == 1 and h["langkah_gagal"]["screenshot"]
    and r["langkah"][-1]["langkah"] == "tetap_jalan", f"rencana lanjut setelah langkah gagal: {h}")
rencana["langkah"][0]["berhenti_jika_gagal"] = True
h = json_terakhir(jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))[1])
cek(h["jumlah_langkah"] == 1, f"berhenti_jika_gagal menghentikan rencana: {h}")
print("6. langkah gagal tidak menghentikan rencana: OK ->", h["langkah_gagal"]["galat"])

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

# ---------------------------------------------------------------- 13. izin khusus sudah diberikan -> tidak ke Pengaturan
rencana = {"aplikasi": "santamaria", "judul": "mengambang", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"}, {"aksi": "login", "nama": "login", "peran": "consumer"},
    {"aksi": "tap", "nama": "aktifkan_mengambang", "target": {"label": "Aktifkan tampilan mengambang"},
     "tunggu_muncul": {"label": "Mode mengambang aktif"}}]}
n0 = len(SIM.panggilan)
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
cek(h["status"] == "selesai" and ("buka_pengaturan", "overlay") not in SIM.panggilan[n0:], f"tanpa Pengaturan: {h}\n{err[-1500:]}")
iz = r["izin_otomatis"][0]
cek(iz["saat"] == "awal uji" and "tanpa_optimasi_baterai" in iz["lain"], f"ringkasan izin: {iz}")
print("13. izin lewat adb (runtime + app-ops + baterai): OK ->", iz)

# ---------------------------------------------------------------- 14. beri_izin_otomatis=false: dialog + Pengaturan ditangani
rencana = {"aplikasi": "santamaria", "judul": "izin_manual", "beri_izin_otomatis": False, "langkah": [
    {"aksi": "reset_aplikasi", "nama": "reset"}, {"aksi": "buka_aplikasi", "nama": "buka"},
    {"aksi": "login", "nama": "login", "peran": "consumer"},
    {"aksi": "tap", "nama": "aktifkan_mengambang", "target": {"label": "Aktifkan tampilan mengambang"}},
    {"aksi": "tap", "nama": "buka_laporan", "target": {"label": "Laporan"}, "tunggu_muncul": {"label": "Laporan Keuangan"}}]}
n0 = len(SIM.panggilan)
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
nama_langkah = [l["langkah"] for l in r["langkah"]]
cek(h["status"] == "selesai", f"izin manual: {h}\n{nama_langkah}\n{err[-2500:]}")
cek(("klik", "Izinkan") in SIM.panggilan[n0:], "dialog izin notifikasi harus ditekan Izinkan")
cek(not any(p[0] == "adb" and "pm grant" in p[1] for p in SIM.panggilan[n0:]) and r["izin_otomatis"] == [],
    "tidak boleh ada pm grant saat beri_izin_otomatis=false")
cek(nama_langkah[-3:] == ["layar_sistem_5", "izinkan_di_pengaturan_5", "buka_laporan"], f"urutan: {nama_langkah}")
izin = r["langkah"][-2]
cek(izin["keluar_ke_pengaturan"] == "com.android.settings" and izin["aksi"] == "izin_pengaturan" and izin["berhasil"]
    and "Izinkan tampilan di atas aplikasi lain" in izin["catatan"], f"langkah izin: {izin}")
cek(SIM.overlay_ui and ("sakelar_overlay", True) in SIM.panggilan[n0:], "sakelar yang benar harus dinyalakan")
print("14. alur izin manual + Pengaturan: OK ->", izin["catatan"][:80])

# ---------------------------------------------------------------- 15. menunggu kondisi lalu dibawa ke Pengaturan -> berhenti cepat
rencana["langkah"][3]["tunggu_muncul"] = {"label": "Mode mengambang aktif"}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
tap = next(l for l in r["langkah"] if l["langkah"] == "aktifkan_mengambang")
cek(h["status"] == "selesai" and tap.get("keluar_ke_pengaturan") and tap["respon_ms"] < 4000
    and "kondisi tujuan belum diperiksa" in tap["catatan"], f"berhenti cepat: {tap}\n{err[-1500:]}")
print("15. kondisi + Pengaturan berhenti cepat: OK ->", tap["respon_ms"], "ms")

# ---------------------------------------------------------------- 16. Autostart Xiaomi lewat daftar + Cari, sekali saja
buka_daftar = [x for x in SIM.panggilan if x[0] == "adb" and "com.miui.securitycenter/" in x[1]]
cek(("autostart_daftar", "id.co.santamaria.app") in SIM.panggilan and ("autostart_daftar", "id.co.santamaria.wasiat") in SIM.panggilan
    and ("cari_autostart", "Santa Maria") in SIM.panggilan, "Autostart harus dinyalakan lewat daftar + fitur Cari")
cek("id.co.santamaria.yayasan" not in SIM.autostart, "baris 'Yayasan Sosial Santa Maria' tidak boleh ikut dinyalakan")
cek(len(buka_daftar) == 2, f"daftar Autostart hanya dibuka sekali per aplikasi (cache): {len(buka_daftar)}")
cek(not any(x[0] == "adb" and "APPLICATION_DETAILS_SETTINGS" in x[1] for x in SIM.panggilan), "Info aplikasi tidak perlu dibuka")
cache = json.loads((Path(TMP) / "autostart.json").read_text(encoding="utf-8"))
cek(cache["id.co.santamaria.app"]["status"] == "dinyalakan", f"cache autostart: {cache}")
print("16. Autostart lewat Cari + cache: OK ->", {k: v["status"] for k, v in cache.items()})

# ---------------------------------------------------------------- 17. cadangan: halaman Info aplikasi
SIM.autostart.discard("id.co.santamaria.app")
SIM.daftar_autostart_tidak_ada = True
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria", "--paksa")
h = json_terakhir(baris)
SIM.daftar_autostart_tidak_ada = False
cek(kode == 0 and h["aplikasi"]["santamaria"]["autostart"] == "dinyalakan"
    and ("autostart_info", "id.co.santamaria.app") in SIM.panggilan, f"cadangan Info aplikasi: {h}\n{err[-1500:]}")
cek(SIM.layar == "launcher", "setelah persiapan kembali ke layar utama HP")
kode, baris, err = jalankan_cli("siapkan")
h = json_terakhir(baris)
cek(h["aplikasi"]["yayasan"]["status"] == "tidak_terpasang" and h["aplikasi"]["santamaria"]["autostart"] == "tersimpan",
    f"siapkan semua: {h}")
print("17. cadangan Info aplikasi + siapkan semua: OK")

# ---------------------------------------------------------------- 18. sudah dinyalakan manual -> terdeteksi, tidak disentuh
f_cache = Path(TMP) / "autostart.json"
data = json.loads(f_cache.read_text(encoding="utf-8"))
data.pop("id.co.santamaria.app")
f_cache.write_text(json.dumps(data), encoding="utf-8")
n_tap = sum(1 for x in SIM.panggilan if x[0] in ("autostart_daftar", "autostart_info") and x[1] == "id.co.santamaria.app")
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria")
h = json_terakhir(baris)
n_tap2 = sum(1 for x in SIM.panggilan if x[0] in ("autostart_daftar", "autostart_info") and x[1] == "id.co.santamaria.app")
cek(h["aplikasi"]["santamaria"]["autostart"] == "sudah" and n_tap2 == n_tap, f"harus terdeteksi sudah aktif: {h}")
print("18. Autostart manual terdeteksi 'sudah': OK")

# ---------------------------------------------------------------- 19. gagal dicatat, tidak dicoba ulang tiap uji
data = json.loads(f_cache.read_text(encoding="utf-8"))
data.pop("id.co.santamaria.app")
f_cache.write_text(json.dumps(data), encoding="utf-8")
SIM.daftar_autostart_tidak_ada = SIM.info_tanpa_autostart = True
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria")
h = json_terakhir(baris)
cek(h["aplikasi"]["santamaria"]["autostart"] == "gagal"
    and json.loads(f_cache.read_text(encoding="utf-8"))["id.co.santamaria.app"]["status"] == "gagal"
    and any(Path(TMP, "persiapan").glob("autostart_info_*.png")), f"gagal + bukti: {h}")
n0 = len(SIM.panggilan)
rencana = {"aplikasi": "santamaria", "judul": "setelah_gagal", "langkah": [{"aksi": "buka_aplikasi", "nama": "buka"}]}
kode, baris, err = jalankan_cli("jalankan", "-", stdin=json.dumps(rencana))
cek(json_terakhir(baris)["status"] == "selesai" and not any(
    x[0] == "adb" and ("APPLICATION_DETAILS" in x[1] or "securitycenter/" in x[1]) for x in SIM.panggilan[n0:]),
    "uji biasa tidak boleh mencoba Autostart lagi setelah gagal")
SIM.daftar_autostart_tidak_ada = SIM.info_tanpa_autostart = False
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria")
cek(json_terakhir(baris)["aplikasi"]["santamaria"]["autostart"] == "sudah", "siapkan harus mencoba ulang yang gagal")
print("19. gagal dicatat + dicoba ulang lewat siapkan: OK")

# ---------------------------------------------------------------- 20. struktur asli HyperOS (XML dari HP POCO)
class DriverXML:
    def __init__(self, xml):
        self.page_source = xml


cfg_asli = ag.muat_konfigurasi(os.environ["UJI_UI_AKUN"])
fix = Path(__file__).resolve().parent / "fixtures"
pw = ag.Pelaksana(DriverXML((fix / "hyperos_autostart_cari_wasiat.xml").read_text(encoding="utf-8")),
                  cfg_asli, "wasiat", Path(TMP), {"dpi": 480})
sw = pw.cari_sakelar_baris(lambda t: t == "Wasiat")
cek(sw is not None and sw.get("resource-id", "").endswith("sliding_button") and sw.get("checked") == "false",
    f"Wasiat (HyperOS): {sw.attrib if sw is not None else None}")
ps = ag.Pelaksana(DriverXML((fix / "hyperos_autostart_cari_santamaria.xml").read_text(encoding="utf-8")),
                  cfg_asli, "santamaria", Path(TMP), {"dpi": 480})
s1 = ps.cari_sakelar_baris(lambda t: t == "Santa Maria")
s2 = ps.cari_sakelar_baris(lambda t: t == "Yayasan Sosial Santa Maria")
cek(s1 is not None and s1.get("checked") == "true" and s1.get("bounds") == "[981,359][1133,458]",
    f"Santa Maria harus terbaca menyala di baris pertama: {s1.attrib if s1 is not None else None}")
cek(s2 is not None and s2.get("checked") == "false" and s2.get("bounds") == "[981,551][1133,650]",
    f"Yayasan Sosial Santa Maria harus terbaca mati di baris kedua: {s2.attrib if s2 is not None else None}")
print("20. XML asli HyperOS: OK -> Santa Maria menyala, Yayasan & Wasiat mati, tombol geser terpilih")

# ---------------------------------------------------------------- 21. sambungan adb: galat adb bukan "tidak terpasang"
SIM.perangkat = []  # kabel lepas / USB debugging mati: adb menulis "no devices/emulators found" di stderr
kode, baris, err = jalankan_cli("siapkan", "--paksa")
h = json_terakhir(baris)
cek(kode == 2 and "tidak terdeteksi" in h["pesan"] and "tidak terpasang" not in err,
    f"HP tidak tersambung harus dilaporkan jelas, bukan 'tidak terpasang': {h}\n{err[-600:]}")
cek(ag.info_perangkat(simulator.PKG)["model"] == "", "pesan galat adb tidak boleh terbaca sebagai nama model HP")
SIM.perangkat = [("SIM0001", "unauthorized")]
h = json_terakhir(jalankan_cli("siapkan")[1])
cek("unauthorized" in h.get("pesan", "") and "Izinkan" in h["pesan"], f"unauthorized: {h}")
SIM.perangkat, SIM.pulih_setelah_reconnect = [("SIM0001", "offline")], True
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria")
cek(kode == 0 and json_terakhir(baris)["aplikasi"]["santamaria"]["status"] == "ok"
    and ("adb", "reconnect offline") in SIM.panggilan, f"offline dipulihkan lewat reconnect: {baris[-1:]}\n{err[-600:]}")
SIM.pulih_setelah_reconnect = False
SIM.perangkat = [("SIM0001", "device"), ("adb-SIM0001-xY._adb-tls-connect._tcp", "device")]
kode, baris, err = jalankan_cli("siapkan", "--app", "santamaria")
cek(kode == 0 and json_terakhir(baris)["aplikasi"]["santamaria"]["status"] == "ok" and ag.SERIAL_AKTIF == "SIM0001"
    and os.environ.get("ANDROID_SERIAL") == "SIM0001" and SIM.udid_terakhir == "SIM0001",
    f"HP lewat USB dipilih walau juga tersambung lewat Wi-Fi: {err[-600:]}")
SIM.perangkat = [("SIM0001", "device"), ("SIM0002", "device")]
h = json_terakhir(jalankan_cli("siapkan")[1])
cek("SIM0001" in h.get("pesan", "") and "serial" in h["pesan"], f"dua HP USB harus minta serial: {h}")
cfg_dua = ag.muat_konfigurasi(os.environ["UJI_UI_AKUN"])
cfg_dua.setdefault("hp", {})["serial"] = "SIM0002"
cek(ag.pastikan_hp(cfg_dua) == "SIM0002", "serial dari aplikasi.json dipakai")
SIM.perangkat = [("SIM0001", "device")]
SIM.adb_tidak_ada = True
h = json_terakhir(jalankan_cli("siapkan")[1])
SIM.adb_tidak_ada = False
cek("Platform-Tools" in h.get("pesan", ""), f"adb tidak ada: {h}")
kode, baris, err = jalankan_cli("siapkan")
h = json_terakhir(baris)
cek(h["aplikasi"]["yayasan"]["status"] == "tidak_terpasang" and h["aplikasi"]["wasiat"]["status"] == "ok"
    and "id.co.santamaria.yayasan tidak terpasang" in err, f"HP tersambung: hanya yang benar-benar belum dipasang: {h}")
print("21. sambungan adb: OK -> kosong / unauthorized / offline / USB+Wi-Fi / dua HP / adb tidak ada")

# ---------------------------------------------------------------- 22. penjelajah otomatis (aplikasi demo)
cfg_demo = json.loads(Path(os.environ["UJI_UI_AKUN"]).read_text(encoding="utf-8"))
cfg_demo["aplikasi"]["demo"] = {"nama": "Demo", "package": simulator.PKG_DEMO, "activity": ".MainActivity", "akun": {},
                                "beri_izin_otomatis": False}
F_DEMO = Path(TMP) / "akun_demo.json"
F_DEMO.write_text(json.dumps(cfg_demo), encoding="utf-8")
simulator.demo_reset()
n0 = len(SIM.panggilan)
kode, baris, err = jalankan_cli("--akun", str(F_DEMO), "jelajahi", "--app", "demo", "--batas-ketukan", "80")
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "selesai", f"jelajahi demo: {h}\n{err[-2000:]}")
j = json.loads((Path(h["folder"]) / "jelajah.json").read_text(encoding="utf-8"))
judul = " | ".join(L["judul"] for L in j["layar"])
cek(len(j["layar"]) == 9 and all(x in judul for x in ("Notifikasi", "Tambah Pesanan", "Laporan Bulanan", "Pilih Nama",
                                                       "Ubah Kata Sandi")), f"semua layar demo ditemukan: {judul}")
st = {e["label"]: e for e in j["elemen"]}
cek(st["Laporan Bulanan"]["status"] == "dijelajah", "menu di bawah bilah navigasi dijangkau dengan menggulir")
cek(sum(1 for e in j["elemen"] if e["status"] == "diwakili") >= 4 and st["Pesanan #2"]["status"] == "dijelajah",
    "daftar sejenis diwakili 2 contoh")
cek(all(st[x]["status"] == "diblokir" for x in ("Simpan", "Hapus", "Selesaikan", "Keluar")), "tombol pengubah data diblokir")
cek(st["Sinkronkan"]["hasil"] == "crash" and st["Muat ulang"]["hasil"] == "tanpa_perubahan", "crash + tap tanpa perubahan")
baru = SIM.panggilan[n0:]
cek(not any(x[0] == "tombol_sistem" for x in baru), "tidak boleh ada ketukan di bilah navigasi HP")
cek(not any(x[0] in ("demo_hapus", "demo_buat", "demo_selesaikan", "demo_keluar", "demo_ganti_sandi") for x in baru),
    "penjelajah tidak boleh mengubah data")
cek(SIM.d_fingerprint is False and len(SIM.d_pesanan) == 6, "sakelar dikembalikan, data utuh")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
log_lk = {g["jenis"] for l in r["langkah"] for g in l.get("log_galat") or []}
janggal = {x["alasan"].split(":")[0] for l in r["langkah"] for x in (l.get("metrik") or {}).get("masalah", {}).get("teks_janggal", [])}
dup = sum(len((l.get("metrik") or {}).get("masalah", {}).get("duplikat_klik", [])) for l in r["langkah"])
cek({"crash", "galat_flutter"} <= log_lk and len(janggal) == 2 and dup >= 3, f"log={log_lk} janggal={janggal} D={dup}")
cek(sum(1 for l in r["langkah"] if l.get("screenshot")) < len(r["langkah"]) / 2, "screenshot hanya untuk layar baru")
print(f"22. penjelajah otomatis: OK -> {j['statistik']}")

# ---------------------------------------------------------------- 23. ketukan aman: elemen di bawah bilah navigasi
simulator.demo_reset()
rencana = {"aplikasi": "demo", "judul": "laporan_bawah", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"},
    {"aksi": "tap", "nama": "buka_laporan_bulanan", "target": {"label": "Laporan Bulanan"},
     "tunggu_muncul": {"label": "Laporan Bulanan", "kelas": "View"}}]}
n0 = len(SIM.panggilan)
kode, baris, err = jalankan_cli("--akun", str(F_DEMO), "jalankan", "-", stdin=json.dumps(rencana))
h = json_terakhir(baris)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
tap = r["langkah"][-1]
cek(h["status"] == "selesai" and tap.get("digulir") and tap["titik_ketuk"][1] < simulator.NAV_Y
    and not any(x[0] == "tombol_sistem" for x in SIM.panggilan[n0:]), f"gulir dulu lalu ketuk: {tap}\n{err[-1500:]}")
print("23. ketukan aman (gulir dulu, bukan bilah navigasi): OK ->", tap["titik_ketuk"], "digulir", tap["digulir"])

# ---------------------------------------------------------------- 24. CRUD dengan pemulihan + pengaman
salah = ag.validasi_rencana({"aplikasi": "demo", "langkah": [
    {"aksi": "tap", "target": {"label": "Simpan"}},
    {"aksi": "tap", "target": {"label": "Kirim via WhatsApp"}, "ubah_data": "buat"},
    {"aksi": "tap", "target": {"label": "Simpan"}, "ubah_data": "buat"}]}, cfg_demo, izinkan_berisiko=True)
cek(any("tambahkan \"ubah_data\"" in x for x in salah) and any("selalu diblokir" in x for x in salah)
    and any("pemulihan" in x for x in salah), f"validasi CRUD: {salah}")
simulator.demo_reset()
crud = {"aplikasi": "demo", "judul": "crud_pesanan", "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka"},
    {"aksi": "tap", "nama": "tambah", "target": {"label": "Tambah Pesanan"}},
    {"aksi": "ketik", "nama": "isi_judul", "target": {"hint": "Judul pesanan"}, "teks": "Pesanan {penanda}"},
    {"aksi": "tap", "nama": "simpan", "target": {"label": "Simpan"}, "ubah_data": "buat", "objek": "pesanan"},
    {"aksi": "pastikan_ada", "nama": "pesanan_tampil", "target": {"label_mengandung": "{penanda}"}},
    {"aksi": "tap", "nama": "buka_pesanan_lama", "target": {"label": "Pesanan #1"}},
    {"aksi": "tap", "nama": "hapus_data_lama", "target": {"label": "Hapus"}, "ubah_data": "hapus", "objek": "pesanan lama"},
    {"aksi": "kembali", "nama": "kembali_ke_dasbor"},
    {"aksi": "tap", "nama": "tab_profil", "target": {"label": "Profil"}},
    {"aksi": "tap", "nama": "sakelar_sidik_jari", "target": {"label": "Fingerprint"}},
    {"aksi": "tap", "nama": "buka_ubah_sandi", "target": {"label": "Ubah kata sandi"}},
    {"aksi": "tap", "nama": "simpan_sandi", "target": {"label": "Simpan"}, "ubah_data": "ubah", "objek": "kata sandi"},
    {"aksi": "atur_tema", "nama": "tema_gelap", "tema": "gelap"},
    {"aksi": "atur_jaringan", "nama": "tanpa_internet", "status": "mati"}],
    "pemulihan": [
    {"aksi": "buka_aplikasi", "nama": "buka_lagi"},
    {"aksi": "tap", "nama": "buka_data_uji", "target": {"label_mengandung": "{penanda}"}},
    {"aksi": "tap", "nama": "hapus_data_uji", "target": {"label": "Hapus"}, "ubah_data": "hapus", "objek": "pesanan"},
    {"aksi": "pastikan_tidak_ada", "nama": "data_uji_hilang", "target": {"label_mengandung": "{penanda}"}}]}
kode, baris, _ = jalankan_cli("--akun", str(F_DEMO), "jalankan", "-", stdin=json.dumps(crud))
cek(json_terakhir(baris)["status"] == "rencana_tidak_valid", "CRUD tanpa izin ubah data harus ditolak")
n0 = len(SIM.panggilan)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err_crud:
    h = ag.jalankan_rencana(crud, cfg_demo, izinkan_berisiko=True)
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
nama = {l["langkah"]: l for l in r["langkah"]}
dibuat = [x[1] for x in SIM.panggilan[n0:] if x[0] == "demo_buat"]
dihapus = [x[1] for x in SIM.panggilan[n0:] if x[0] == "demo_hapus"]
cek(len(dibuat) == 1 and dibuat[0] == f"Pesanan {r['penanda']}" and dihapus == dibuat and len(SIM.d_pesanan) == 6,
    f"data uji dibuat lalu dihapus lagi: dibuat={dibuat} dihapus={dihapus}\n{err_crud.getvalue()[-2000:]}")
cek(not nama["hapus_data_lama"]["berhasil"] and "data buatan uji" in nama["hapus_data_lama"]["galat"]
    and "Pesanan #1" in SIM.d_pesanan, f"hapus data lama harus diblokir: {nama['hapus_data_lama']}")
cek(not nama["simpan_sandi"]["berhasil"] and "mengunci akun" in nama["simpan_sandi"]["galat"]
    and ("demo_ganti_sandi",) not in SIM.panggilan[n0:], "simpan kata sandi baru harus diblokir")
cek("sakelar_sidik_jari_kembalikan" in nama and SIM.d_fingerprint is False, "sakelar dikembalikan otomatis")
cek(SIM.tema == "no" and SIM.pesawat is False, "tema & jaringan dikembalikan di akhir uji")
pd = r["perubahan_data"]
cek(r["pemulihan_lengkap"] and [x["status_pulih"] for x in pd if x["jenis"] == "buat"] == ["dipulihkan"]
    and all(l.get("fase") == "pemulihan" for l in r["langkah"] if l["langkah"].startswith("pulihkan_")),
    f"catatan perubahan data: {pd}")
print("24. CRUD + pemulihan + pengaman: OK ->", [(x["jenis"], x["status_pulih"]) for x in pd])

# ---------------------------------------------------------------- 25. klasifikasi risiko label
contoh_risiko = {
    "Hapus": "data_lama", "Ya, Hapus": "data_lama", "Konfirmasi": "ubah_data", "Keluar": "ubah_data",
    "Hapus filter": None, "Buka Kirim Pesanan ke SO": None, "Setujui Pengajuan\nRp 50.000": "data_lama",
    "Pesanan #12\nRp 50.000\nMenunggu Konfirmasi": None, "Pengajuan Kas Kecil\nProses": None,
    "Budi Santoso\nbudi@email.com": None, "Kirim via Email": "selalu", "Hubungi via WhatsApp": "selalu",
    "Akun\nHapus akun": "selalu", "Bayar Sekarang": "selalu",
}
beda = {k: (ag.jenis_risiko(k), v) for k, v in contoh_risiko.items() if ag.jenis_risiko(k) != v}
cek(not beda, f"klasifikasi risiko label (didapat, seharusnya): {beda}")
print("25. klasifikasi risiko label: OK ->", len(contoh_risiko), "label")


# ---------------------------------------------------------------- 26. dialog kalender Flutter + lapisan penutup
# XML asli dari HP: pemilih rentang tanggal layar penuh. Lapisan penutup 'Tutup' (ModalBarrier) muncul paling akhir
# di urutan dokumen padahal digambar paling bawah; dulu semua tanggal dianggap tertutup dan penjelajah berputar-putar.
zona_hp = [[0, 0, 1220, 110], [0, 2580, 1220, 2712]]
xml_kal = (fix / "kalender_rentang_flutter.xml").read_text(encoding="utf-8")
root_kal = ag._root(xml_kal)
urut_k, induk_k = ag.pohon_induk(root_kal)
W_k, H_k = ag.ukuran_layar_xml(root_kal)
penghalang = [el for el in urut_k if ag.adalah_penghalang(el, W_k, H_k)]
cek(len(penghalang) == 1 and ag._label(penghalang[0]) == "Tutup", f"lapisan penutup dikenali: {len(penghalang)}")
bisa = [el for el in urut_k if el.get("clickable") == "true" and el not in penghalang]
tertutup = [ag._label(el) for el in bisa if ag.titik_terlihat(root_kal, el, zona_hp, urut_k, induk_k)[0] is None]
cek(len(bisa) >= 60 and not tertutup, f"tombol & tanggal di dialog harus bisa diketuk: {tertutup[:5]}")
cek(ag.titik_terlihat(root_kal, penghalang[0], zona_hp, urut_k, induk_k)[0] is None,
    "lapisan penutup dialog layar penuh tidak punya area terbuka untuk diketuk")
el_kal = ag.daftar_elemen(xml_kal, 3.0, maks=200, kotak=True, zona=zona_hp)
cek(sum(1 for e in el_kal if e.get("penghalang")) == 1 and not any(e.get("tertutup") for e in el_kal),
    "daftar elemen: penghalang ditandai, tidak ada yang 'tertutup'")
cek(not ag.analisis_xml(xml_kal, 3.0, zona=zona_hp)["masalah"]["elemen_bertumpuk"], "penghalang bukan elemen bertumpuk")
cek(ag._norm_label("1, Minggu, 1 November 2026") == ag._norm_label("27, Rabu, 27 Desember 2026") == "#, hr, # bln #"
    and ag._norm_label("Pesanan #12") == "pesanan #", "normalisasi tanggal")
xml_kal2 = (fix / "kalender_rentang_flutter_digulir.xml").read_text(encoding="utf-8")
cek(ag.mirip(ag.tanda_layar(root_kal), ag.tanda_layar(ag._root(xml_kal2))) >= 0.8,
    "kalender yang digulir tetap dikenali sebagai layar yang sama")
cek(ag.ada_lapisan_modal(xml_kal) and not ag.ada_lapisan_modal(simulator.LOGIN_XML), "deteksi dialog modal")

# Penjelajah di aplikasi demo dengan dialog kalender yang sama strukturnya.
simulator.demo_reset()
SIM.d_kalender = True
n0 = len(SIM.panggilan)
t0 = __import__("time").perf_counter()
kode, baris, err = jalankan_cli("--akun", str(F_DEMO), "jelajahi", "--app", "demo", "--batas-ketukan", "80")
h = json_terakhir(baris)
cek(kode == 0 and h["status"] == "selesai", f"jelajahi demo + kalender: {h}\n{err[-2000:]}")
j = json.loads((Path(h["folder"]) / "jelajah.json").read_text(encoding="utf-8"))
L_kal = [L["id"] for L in j["layar"] if "Pilih rentang tanggal" in L["judul"]]
cek(len(L_kal) >= 1, f"layar kalender ditemukan: {[L['judul'] for L in j['layar']]}")
el_j = [e for e in j["elemen"] if e["layar"] in L_kal]
st_kal = {}
for e in el_j:
    st_kal.setdefault(e["status"], []).append(e["label"])
tgl = [e for e in el_j if e["label"] and e["label"][0].isdigit()]
cek(not st_kal.get("tak_terjangkau") and not st_kal.get("dilewati"), f"tidak ada yang tak terjangkau: {st_kal}")
cek(1 <= sum(1 for e in tgl if e["status"] == "dijelajah") <= 2 and sum(1 for e in tgl if e["status"] == "diwakili") >= 20,
    f"kisi tanggal diwakili 2 contoh: {[(e['label'][:12], e['status']) for e in tgl[:10]]}")
cek(any(e["label"] == "Tutup" and e["status"] == "dijelajah" for e in el_j)
    and any(e["label"] == "Beralih ke masukan" and e["status"] == "dijelajah" for e in el_j), f"tombol kepala dijelajah: {st_kal}")
baru = SIM.panggilan[n0:]
cek(not any(x[0] == "tap_penghalang" for x in baru), "lapisan penutup tidak diketuk")
cek(not any(x[0] == "demo_simpan_tanggal" for x in baru) and len([x for x in baru if x[0] == "demo_pilih_tanggal"]) <= 2,
    "tanggal dipilih paling banyak 2 kali, Simpan tidak ditekan")
r = json.loads(Path(h["ringkasan"]).read_text(encoding="utf-8"))
menuju = [l["langkah"] for l in r["langkah"] if l["langkah"].startswith(tuple(f"jelajah_menuju_{x}" for x in L_kal))]
cek(len(menuju) <= 4 and j["statistik"]["pemulihan_jalur"] <= 12, f"tidak berputar-putar: menuju={len(menuju)} {j['statistik']}")
SIM.d_kalender = False
print(f"26. dialog kalender + lapisan penutup: OK -> {len(bisa)} elemen terjangkau, {j['statistik']}")

print(f"\nSEMUA {lulus} CEK LULUS (hasil uji di {TMP})")
