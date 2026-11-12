"""Simulator HP + Appium untuk menguji agen_uji.py/orkestrator.py tanpa perangkat sungguhan.

Driver tiruan mengevaluasi selector UiAutomator dan XPath yang dipakai skrip terhadap XML layar:
layar login asli Santa Maria (dari Inspector), plus beranda, memuat, laporan, dialog izin, dan launcher.
Pemakaian: import simulator; simulator.pasang(agen_uji)  (sebelum menjalankan perintah apa pun).
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

PKG = "id.co.santamaria.app"
PKG_WASIAT = "id.co.santamaria.wasiat"
PKG_GMS = "com.google.android.gms"
PKG_SETELAN = "com.android.settings"
PKG_KEAMANAN = "com.miui.securitycenter"
PKG_DEMO = "id.contoh.demo"
LABEL_APP = {PKG: "Santa Maria", PKG_WASIAT: "Wasiat", "id.co.santamaria.yayasan": "Yayasan Sosial Santa Maria",
             "com.android.chrome": "Chrome"}
# Daftar Autostart panjang (seperti HP sungguhan): aplikasi uji ada jauh di bawah, tidak terlihat tanpa Cari/gulir.
DAFTAR_AUTOSTART = (["com.android.chrome"] + [f"com.contoh.app{i:02d}" for i in range(1, 30)]
                    + ["id.co.santamaria.yayasan", PKG, PKG_WASIAT])
LABEL_APP.update({f"com.contoh.app{i:02d}": f"Aplikasi Contoh {i:02d}" for i in range(1, 30)})
NAV_Y = 2568  # bilah navigasi 3 tombol (48 dp) di bawah layar 1220x2712
IZIN_NOTIF = "android.permission.POST_NOTIFICATIONS"
IZIN_DIMINTA = [IZIN_NOTIF, "android.permission.CAMERA", "android.permission.ACCESS_FINE_LOCATION",
                "android.permission.ACCESS_BACKGROUND_LOCATION"]
FIXTURE = Path(__file__).resolve().parent / "fixtures"
LOGIN_XML = (FIXTURE / "login_santamaria.xml").read_text(encoding="utf-8")
AKUN = json.loads(Path(os.environ.get("UJI_UI_AKUN") or FIXTURE / "akun_uji.json").read_text(encoding="utf-8"))
PASSWORD = AKUN["password_default"]
EMAIL_VALID = {a["email"] for a in AKUN["aplikasi"]["santamaria"]["akun"].values()}


def node(cls, pkg=PKG, **a):
    attrs = {"class": cls, "package": pkg, "text": "", "content-desc": "", "clickable": "false",
             "enabled": "true", "displayed": "true", "bounds": "[0,0][10,10]"}
    attrs.update({k.replace("_", "-"): v for k, v in a.items()})
    isi = attrs.pop("anak", "")
    s = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f"<{cls} {s}>{isi}</{cls}>"


def layar(*anak, pkg=PKG):
    root = node("android.widget.FrameLayout", pkg=pkg, bounds="[0,0][1220,2712]", anak="".join(anak))
    return f'<hierarchy index="0" class="hierarchy" rotation="0" width="1220" height="2450">{root}</hierarchy>'


def beranda(tergulir):
    awal = 6 if tergulir else 1
    items = "".join(node("android.view.View", content_desc=f"Item {i}", bounds=f"[40,{600 + k * 200}][1180,{760 + k * 200}]")
                    for k, i in enumerate(range(awal, awal + 5)))
    return layar(
        node("android.view.View", content_desc="Beranda", heading="true", bounds="[0,120][1220,300]"),
        node("android.widget.Button", content_desc="Laporan", clickable="true", bounds="[40,400][600,560]"),
        node("android.widget.Button", content_desc="Pesanan&#10;3", clickable="true", bounds="[620,400][1180,560]"),
        node("android.widget.ImageButton", clickable="true", bounds="[1100,140][1180,220]"),  # ikon tanpa label, 29 dp
        node("android.widget.Button", content_desc="Keluar", clickable="true", bounds="[40,2420][1180,2560]"),
        node("android.widget.Button", content_desc="Aktifkan tampilan mengambang", clickable="true",
             bounds="[40,2260][1180,2400]"),
        node("android.view.View", scrollable="true", bounds="[0,600][1220,2400]", anak=items),
    )


LAYAR = {
    "home": beranda(False),
    "home_bawah": beranda(True),
    "memuat": layar(node("android.widget.ProgressBar", bounds="[560,1300][660,1400]"),
                    node("android.view.View", content_desc="Memuat laporan...", bounds="[300,1420][920,1500]")),
    "laporan": layar(
        node("android.view.View", content_desc="Laporan Keuangan", heading="true", bounds="[0,120][1220,300]"),
        node("android.widget.Button", content_desc="Unduh PDF", clickable="true", bounds="[40,400][600,560]")),
    "izin": layar(
        node("android.widget.TextView", pkg="com.google.android.permissioncontroller",
             text="Izinkan Santa Maria mengirim notifikasi?", bounds="[80,1200][1140,1300]"),
        node("android.widget.Button", pkg="com.google.android.permissioncontroller", text="Izinkan", clickable="true",
             resource_id="com.android.permissioncontroller:id/permission_allow_button", bounds="[80,1400][1140,1540]"),
        node("android.widget.Button", pkg="com.google.android.permissioncontroller", text="Jangan izinkan",
             clickable="true", bounds="[80,1560][1140,1700]"),
        pkg="com.google.android.permissioncontroller"),
    "launcher": layar(node("android.view.View", pkg="com.miui.home", content_desc="Layar utama HP"), pkg="com.miui.home"),
    "mengambang": layar(
        node("android.view.View", content_desc="Mode mengambang aktif", heading="true", bounds="[0,120][1220,300]"),
        node("android.widget.Button", content_desc="Selesai", clickable="true", bounds="[40,400][1180,560]")),
    # ---- aplikasi Wasiat (login Google SSO)
    "w_login": layar(
        node("android.view.View", pkg=PKG_WASIAT, content_desc="Wasiat", heading="true", bounds="[0,300][1220,500]"),
        node("android.view.View", pkg=PKG_WASIAT, content_desc="Buat dan simpan wasiat dengan aman", bounds="[80,600][1140,700]"),
        node("android.widget.Button", pkg=PKG_WASIAT, content_desc="Masuk dengan Google", clickable="true",
             bounds="[80,2200][1140,2340]"), pkg=PKG_WASIAT),
    "w_pemilih": layar(
        node("android.widget.TextView", pkg=PKG_GMS, text="Pilih akun", bounds="[80,1500][1140,1580]"),
        node("android.widget.LinearLayout", pkg=PKG_GMS, clickable="true", bounds="[0,1600][1220,1760]", anak=(
            node("android.widget.TextView", pkg=PKG_GMS, text="Pengguna Uji", bounds="[160,1610][1100,1680]")
            + node("android.widget.TextView", pkg=PKG_GMS, text="uji.wasiat@gmail.com", bounds="[160,1690][1100,1750]"))),
        node("android.widget.LinearLayout", pkg=PKG_GMS, clickable="true", bounds="[0,1780][1220,1940]", anak=(
            node("android.widget.TextView", pkg=PKG_GMS, text="Akun Lain", bounds="[160,1790][1100,1860]")
            + node("android.widget.TextView", pkg=PKG_GMS, text="lain@gmail.com", bounds="[160,1870][1100,1930]"))),
        node("android.widget.TextView", pkg=PKG_GMS, text="Tambahkan akun lain", clickable="true", bounds="[0,1960][1220,2100]"),
        pkg=PKG_GMS),
    "w_memuat": layar(node("android.widget.ProgressBar", pkg=PKG_WASIAT, bounds="[560,1300][660,1400]"), pkg=PKG_WASIAT),
    "w_home": layar(
        node("android.view.View", pkg=PKG_WASIAT, content_desc="Beranda Wasiat", heading="true", bounds="[0,120][1220,300]"),
        node("android.widget.Button", pkg=PKG_WASIAT, content_desc="Buat Wasiat", clickable="true", bounds="[40,400][1180,560]"),
        node("android.widget.Button", pkg=PKG_WASIAT, content_desc="Profil", clickable="true", bounds="[40,600][1180,760]"),
        node("android.widget.ImageButton", pkg=PKG_WASIAT, clickable="true", bounds="[1100,140][1180,220]"),
        pkg=PKG_WASIAT),
}


def layar_pengaturan(aktif):
    """Halaman Pengaturan sistem 'Tampilkan di atas aplikasi lain' untuk Santa Maria."""
    baris = node("android.widget.LinearLayout", pkg=PKG_SETELAN, clickable="true", bounds="[0,700][1220,900]", anak=(
        node("android.widget.TextView", pkg=PKG_SETELAN, text="Izinkan tampilan di atas aplikasi lain",
             bounds="[60,740][980,860]")
        + node("android.widget.LinearLayout", pkg=PKG_SETELAN, bounds="[1000,740][1180,860]", anak=node(
            "android.widget.Switch", pkg=PKG_SETELAN, checkable="true", checked="true" if aktif else "false",
            clickable="true", bounds="[1020,760][1160,840]"))))
    lain = node("android.widget.LinearLayout", pkg=PKG_SETELAN, bounds="[0,920][1220,1100]", anak=(
        node("android.widget.TextView", pkg=PKG_SETELAN, text="Sembunyikan ikon notifikasi", bounds="[60,960][980,1060]")
        + node("android.widget.Switch", pkg=PKG_SETELAN, checkable="true", checked="true", clickable="true",
               bounds="[1020,980][1160,1060]")))
    return layar(node("android.widget.TextView", pkg=PKG_SETELAN, text="Tampilkan di atas aplikasi lain",
                      bounds="[60,300][1160,420]"),
                 node("android.widget.TextView", pkg=PKG_SETELAN, text="Santa Maria", bounds="[60,480][1160,560]"),
                 baris, lain, pkg=PKG_SETELAN)


def layar_info_app(pkg, aktif, tanpa_autostart):
    """Halaman Info aplikasi MIUI/HyperOS dengan sakelar 'Mulai otomatis'."""
    baris = "" if tanpa_autostart else node(
        "android.widget.LinearLayout", pkg=PKG_KEAMANAN, clickable="true", bounds="[0,700][1220,900]", anak=(
            node("android.widget.LinearLayout", pkg=PKG_KEAMANAN, bounds="[60,720][960,880]", anak=(
                node("android.widget.TextView", pkg=PKG_KEAMANAN, text="Mulai otomatis", bounds="[60,720][960,790]")
                + node("android.widget.TextView", pkg=PKG_KEAMANAN,
                       text="Izinkan aplikasi berjalan otomatis di latar belakang dan dimulai oleh aplikasi lain",
                       bounds="[60,800][960,880]")))
            + node("miuix.slidingwidget.widget.SlidingButton", pkg=PKG_KEAMANAN, checkable="true",
                   checked="true" if aktif else "false", clickable="true", bounds="[1020,760][1160,840]")))
    return layar(node("android.widget.TextView", pkg=PKG_KEAMANAN, text="Info aplikasi", bounds="[60,150][1160,260]"),
                 node("android.widget.TextView", pkg=PKG_KEAMANAN, text=LABEL_APP.get(pkg, pkg), bounds="[60,400][1160,500]"),
                 baris,
                 node("android.widget.TextView", pkg=PKG_KEAMANAN, text="Penghemat baterai", clickable="true",
                      bounds="[60,950][1160,1050]"), pkg=PKG_KEAMANAN)


def baris_autostart():
    """Baris yang terlihat di daftar Autostart: [(paket, y_atas)]. Tanpa Cari hanya 8 baris pertama yang tampil."""
    if SIM.cari_teks:
        paket = [x for x in DAFTAR_AUTOSTART if SIM.cari_teks.lower() in LABEL_APP[x].lower()]
    else:
        paket = DAFTAR_AUTOSTART[:8]
    return [(x, 400 + i * 200) for i, x in enumerate(paket[:10])]


def layar_daftar_autostart(aktif):
    """Daftar 'Mulai otomatis di latar belakang' di aplikasi Keamanan, dengan ikon Cari."""
    baris = ""
    for pkg, y in baris_autostart():  # struktur seperti HyperOS asli: baris = Switch yang berisi Switch geser
        nyala = "true" if pkg in aktif else "false"
        baris += node("android.widget.Switch", pkg=PKG_KEAMANAN, content_desc=LABEL_APP[pkg], checkable="true",
                      checked=nyala, clickable="true", bounds=f"[37,{y}][1183,{y + 180}]", anak=(
            node("android.widget.ImageView", pkg=PKG_KEAMANAN, resource_id="com.miui.securitycenter:id/icon",
                 bounds=f"[87,{y + 30}][211,{y + 150}]")
            + node("android.widget.TextView", pkg=PKG_KEAMANAN, text=LABEL_APP[pkg],
                   resource_id="com.miui.securitycenter:id/title", bounds=f"[261,{y + 50}][960,{y + 130}]")
            + node("android.widget.Switch", pkg=PKG_KEAMANAN, checkable="true", checked=nyala, clickable="true",
                   resource_id="com.miui.securitycenter:id/sliding_button", bounds=f"[1020,{y + 50}][1160,{y + 130}]")))
    atas = (node("android.widget.EditText", pkg=PKG_KEAMANAN, text=SIM.cari_teks, clickable="true",
                 bounds="[60,150][900,260]")
            + node("android.widget.TextView", pkg=PKG_KEAMANAN, text="Batal", clickable="true", bounds="[950,150][1160,260]")
            if SIM.cari_aktif else
            node("android.widget.TextView", pkg=PKG_KEAMANAN, text="Mulai otomatis di latar belakang", bounds="[60,150][900,260]")
            + node("android.widget.ImageView", pkg=PKG_KEAMANAN, content_desc="Cari", clickable="true",
                   bounds="[960,150][1060,260]"))
    return layar(atas, node("androidx.recyclerview.widget.RecyclerView", pkg=PKG_KEAMANAN, scrollable="true",
                            bounds="[0,380][1220,2400]", anak=baris), pkg=PKG_KEAMANAN)


def layar_gerbang(lembar, centang, konfirmasi):
    """Halaman 'Izin aplikasi' milik Santa Maria + lembar konfirmasi Autostart (seperti di HP sungguhan)."""
    status = "Aktif" if konfirmasi else "Belum aktif"
    isi = [node("android.view.View", content_desc="Izin aplikasi", bounds="[0,150][1220,260]"),
           node("android.widget.Button", clickable="true", bounds="[40,600][1180,1450]",
                content_desc=f"IZIN YANG PERLU DIAKTIFKAN&#10;Aktifkan Autostart (HP), HP Anda memblokir aplikasi. {status}"),
           node("android.widget.Button", content_desc="Periksa ulang", clickable="true", bounds="[40,2380][1180,2530]")]
    if lembar:
        isi = isi[:1] + [
            node("android.view.View", content_desc="Aktifkan autostart", bounds="[40,450][1180,560]"),
            node("android.widget.CheckBox", checkable="true", checked="true" if centang else "false", clickable="true",
                 content_desc="Saya sudah mengaktifkan autostart untuk Santa Maria", bounds="[40,1950][1180,2060]"),
            node("android.widget.Button", content_desc="Sudah aktif", clickable="true",
                 enabled="true" if centang else "false", bounds="[60,2100][1160,2250]"),
            node("android.widget.Button", content_desc="Buka pengaturan", clickable="true", bounds="[300,2280][900,2400]")]
    return layar(*isi)


class Sim:
    def __init__(self):
        self.login_sebagai = "dummy.admin@contoh.test"  # HP masih login akun lain
        self.izin_tertunda = False
        self.layar = "launcher"
        self.tergulir = False
        self.keyboard = False
        self.ketikan = {}
        self.transisi = None          # (layar_tujuan, waktu, fungsi)
        self.jeda_laporan = 1.2
        self.panggilan = []
        self.mulai_rekam = None
        self.app = "santamaria"
        self.wasiat_login = None
        self.izin_runtime = set()
        self.appops = set()
        self.overlay_ui = False   # diizinkan lewat sakelar di Pengaturan
        self.autostart = set()    # paket yang Autostart-nya aktif (Xiaomi)
        self.info_pkg = None
        self.info_tanpa_autostart = False  # simulasi HyperOS yang tidak menaruh sakelar di Info aplikasi
        self.cari_aktif = False
        self.cari_teks = ""
        self.daftar_autostart_tidak_ada = False  # simulasi HP yang tidak punya halaman daftar Autostart
        self.gerbang_ok = False   # konfirmasi Autostart di dalam aplikasi (tersimpan di data aplikasi)
        self.gerbang_lembar = False
        self.gerbang_centang = False
        self.gerbang_konfirmasi = False
        self.perangkat = [("SIM0001", "device")]  # isi 'adb devices'
        self.adb_tidak_ada = False                # simulasi adb tidak terpasang di komputer
        self.pulih_setelah_reconnect = False      # 'adb reconnect offline' memulihkan HP yang offline
        self.udid_terakhir = None                 # udid yang dikirim ke Appium
        self.logcat = []                          # (epoch, pid, tingkat, tag, pesan)
        self.tema = "no"                          # cmd uimode night
        self.pesawat = False                      # mode pesawat
        self.d_pesanan = [f"Pesanan #{i}" for i in range(1, 7)]   # aplikasi demo
        self.d_tergulir, self.d_fingerprint, self.d_detail, self.d_form = False, False, None, ""
        self.d_kalender, self.d_kal_geser, self.d_kal_pilih, self.d_kal_masukan = False, 0, [], False
        self.reset_login()

    def reset_login(self):
        self.pohon_login = ET.fromstring(LOGIN_XML.encode("utf-8"))
        self.ketikan = {}

    def jadwal(self, tujuan, jeda, fungsi=None):
        self.transisi = (tujuan, time.perf_counter() + jeda, fungsi)

    def buka(self):
        self.transisi = None
        if self.app == "wasiat":
            self.layar = "w_home" if self.wasiat_login else "w_login"
            return
        if self.app == "demo":
            self.layar, self.d_tergulir = "d_home", False
            return
        if self.izin_tertunda and IZIN_NOTIF in self.izin_runtime:
            self.izin_tertunda = False  # izin sudah diberikan lewat adb -> tidak ditanya lagi
        if self.izin_tertunda:
            self.layar = "izin"
        elif self.login_sebagai:
            self.layar, self.tergulir = "home", False
        else:
            self.layar = "login"
            self.reset_login()

    def pohon(self):
        if self.transisi and time.perf_counter() >= self.transisi[1]:
            tujuan, _, fungsi = self.transisi
            self.transisi = None
            if fungsi:
                fungsi()
            self.layar, self.tergulir = tujuan, False
        if self.layar.startswith("d_"):
            return ET.fromstring(demo_layar().encode("utf-8"))
        if self.layar == "login":
            return self.pohon_login
        if self.layar == "pengaturan":
            return ET.fromstring(layar_pengaturan(self.overlay_ui).encode("utf-8"))
        if self.layar == "info_app":
            return ET.fromstring(layar_info_app(self.info_pkg, self.info_pkg in self.autostart,
                                                self.info_tanpa_autostart).encode("utf-8"))
        if self.layar == "daftar_autostart":
            return ET.fromstring(layar_daftar_autostart(self.autostart).encode("utf-8"))
        if self.layar == "home" and self.app == "santamaria" and not self.gerbang_ok:
            return ET.fromstring(layar_gerbang(self.gerbang_lembar, self.gerbang_centang,
                                               self.gerbang_konfirmasi).encode("utf-8"))
        kunci = "home_bawah" if (self.layar == "home" and self.tergulir) else self.layar
        return ET.fromstring(LAYAR[kunci].encode("utf-8"))


SIM = Sim()
METODE = re.compile(r'\.?(\w+)\((?:"((?:[^"\\]|\\.)*)"|([^)]*))\)')


def cocok(el, panggilan):
    def a(k):
        return el.get(k) or ""
    for nama, arg in panggilan:
        v = arg.replace('\\"', '"').replace("\\\\", "\\")
        uji = {
            "className": lambda: a("class") == v, "description": lambda: a("content-desc") == v,
            "text": lambda: a("text") == v, "descriptionContains": lambda: v in a("content-desc"),
            "textContains": lambda: v in a("text"), "resourceId": lambda: a("resource-id") == v,
            "resourceIdMatches": lambda: re.fullmatch(v, a("resource-id")) is not None,
            "packageName": lambda: a("package") == v,
            "packageNameMatches": lambda: re.fullmatch(v, a("package")) is not None,
            "clickable": lambda: a("clickable") == v.strip(), "instance": lambda: True,
            "checkable": lambda: a("checkable") == v.strip(), "focused": lambda: a("focused") == v.strip(),
            "descriptionMatches": lambda: re.fullmatch(v, a("content-desc")) is not None,
            "textMatches": lambda: re.fullmatch(v, a("text")) is not None,
        }
        if nama not in uji:
            raise ValueError(f"metode UiSelector belum didukung simulator: {nama}")
        if not uji[nama]():
            return False
    return True


class El:
    def __init__(self, e):
        self.e = e

    def get_attribute(self, n):
        return self.e.get(n)

    def click(self):
        cd, tx, kls = self.e.get("content-desc") or "", self.e.get("text") or "", self.e.get("class")
        SIM.panggilan.append(("klik", cd or tx))
        if self.e.get("enabled") == "false":
            return  # tombol nonaktif tidak bereaksi
        if SIM.layar.startswith("d_"):
            demo_klik(cd)
            return
        if SIM.layar == "daftar_autostart":
            if cd == "Cari":
                SIM.cari_aktif = True
            elif tx == "Batal":
                SIM.cari_aktif, SIM.cari_teks = False, ""
            return
        if SIM.layar == "home" and SIM.app == "santamaria" and not SIM.gerbang_ok:
            if cd.startswith("IZIN YANG PERLU DIAKTIFKAN"):
                SIM.gerbang_lembar = True
            elif cd.startswith("Saya sudah mengaktifkan"):
                SIM.gerbang_centang = not SIM.gerbang_centang
            elif cd == "Sudah aktif":
                SIM.gerbang_konfirmasi, SIM.gerbang_lembar = True, False
            elif cd == "Periksa ulang" and SIM.gerbang_konfirmasi:
                SIM.gerbang_ok = True
            return
        if SIM.layar == "izin" and tx == "Izinkan":
            SIM.izin_tertunda = False
            SIM.izin_runtime.add(IZIN_NOTIF)
            SIM.buka()
        elif SIM.layar == "login":
            if kls == "android.widget.Button" and cd in ("Konsumen", "Personel"):
                for b in SIM.pohon_login.iter():
                    if b.get("class") == "android.widget.Button" and b.get("content-desc") in ("Konsumen", "Personel"):
                        b.set("selected", "true" if b.get("content-desc") == cd else "false")
            elif kls == "android.widget.Button" and cd == "Masuk":
                if SIM.ketikan.get("sandi") == PASSWORD and SIM.ketikan.get("identitas") in EMAIL_VALID:
                    identitas = SIM.ketikan.get("identitas")
                    SIM.jadwal("home", 0.5, lambda: setattr(SIM, "login_sebagai", identitas))
            elif kls == "android.widget.EditText":
                SIM.keyboard = True
        elif SIM.layar == "home" and cd == "Laporan":
            SIM.layar = "memuat"
            SIM.jadwal("laporan", SIM.jeda_laporan)
        elif SIM.layar == "w_login" and cd == "Masuk dengan Google":
            SIM.jadwal("w_pemilih", 0.3)
        elif SIM.layar == "home" and cd == "Aktifkan tampilan mengambang":
            if "SYSTEM_ALERT_WINDOW" in SIM.appops or SIM.overlay_ui:
                SIM.layar = "mengambang"
            else:
                SIM.panggilan.append(("buka_pengaturan", "overlay"))
                SIM.layar = "pengaturan"
        elif SIM.layar == "home" and cd == "Keluar":
            SIM.login_sebagai = None
            SIM.buka()

    def clear(self):
        self.e.set("text", "")
        if SIM.layar == "d_form":
            SIM.d_form = ""

    def send_keys(self, t):
        if SIM.layar == "d_form":
            SIM.d_form = t
            SIM.keyboard = True
            return
        if SIM.layar == "daftar_autostart":
            SIM.cari_teks = t
            SIM.keyboard = True
            SIM.panggilan.append(("cari_autostart", t))
            return
        if self.e.get("password") == "true":
            SIM.ketikan["sandi"] = t
            self.e.set("text", "*" * len(t))
        else:
            SIM.ketikan["identitas"] = t
            self.e.set("text", t)
        SIM.keyboard = True


def video_palsu(detik):
    """MP4 sungguhan (pola uji ffmpeg) sepanjang durasi rekaman, supaya ekstraksi frame bisa diuji."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return b"video palsu"
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "v.mp4"
        subprocess.run([ff, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=270x600:rate=5",
                        "-t", f"{max(detik, 1):.2f}", "-pix_fmt", "yuv420p", str(out)], check=True)
        return out.read_bytes()


class Driver:
    def __init__(self, *a, **k):
        SIM.panggilan.append(("sesi_appium",))
        SIM.udid_terakhir = getattr(k.get("options"), "udid", None)

    def find_elements(self, by, nilai):
        simpul = [e for e in SIM.pohon().iter() if e.tag != "hierarchy"]
        if by == "-android uiautomator":
            assert nilai.startswith("new UiSelector()"), nilai
            panggilan = [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
                         for m in METODE.finditer(nilai[len("new UiSelector()"):])]
            hasil = [e for e in simpul if cocok(e, panggilan)]
            ke = [int(arg) for n, arg in panggilan if n == "instance"]
            if ke:
                hasil = hasil[ke[0]:ke[0] + 1]
        elif by == "xpath":
            m = re.fullmatch(r'\(//\*\[@hint=(["\'])(.*)\1\]\)\[(\d+)\]', nilai)
            if m:
                hasil = [e for e in simpul if e.get("hint") == m.group(2)]
                i = int(m.group(3)) - 1
                hasil = hasil[i:i + 1]
            elif nilai == '//android.widget.EditText[@password="true"]':
                hasil = [e for e in simpul if e.get("class") == "android.widget.EditText" and e.get("password") == "true"]
            else:
                raise ValueError(f"xpath belum didukung simulator: {nilai}")
        else:
            raise ValueError(by)
        return [El(e) for e in hasil]

    def find_element(self, by, nilai):
        r = self.find_elements(by, nilai)
        if not r:
            raise LookupError(nilai)
        return r[0]

    @property
    def page_source(self):
        return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>" + ET.tostring(SIM.pohon(), encoding="unicode")

    @property
    def current_package(self):
        if SIM.layar.startswith("d_"):
            return PKG_DEMO
        if SIM.layar.startswith("w_"):
            SIM.pohon()  # jalankan transisi yang sudah jatuh tempo
            return PKG_GMS if SIM.layar == "w_pemilih" else PKG_WASIAT
        return {"launcher": "com.miui.home", "izin": "com.google.android.permissioncontroller",
                "pengaturan": PKG_SETELAN, "info_app": PKG_KEAMANAN, "daftar_autostart": PKG_KEAMANAN}.get(SIM.layar, PKG)

    def get_screenshot_as_file(self, p):
        """PNG sungguhan seukuran layar HP (latar putih, kotak abu-abu per elemen berlabel)."""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            Path(p).write_bytes(b"\x89PNG\r\n\x1a\n")
            return True
        im = Image.new("RGB", (1220, 2712), "white")
        g = ImageDraw.Draw(im)
        for e in SIM.pohon().iter():
            b = [int(x) for x in re.findall(r"-?\d+", e.get("bounds") or "")]
            if len(b) == 4 and (e.get("content-desc") or e.get("text")) and b[2] - b[0] < 1200:
                g.rectangle(b, fill=(200, 200, 200))
                g.text((b[0] + 10, b[1] + 10), (e.get("content-desc") or e.get("text"))[:30], fill=(150, 150, 150))
        im.save(p)
        return True

    def is_keyboard_shown(self):
        return SIM.keyboard

    def hide_keyboard(self):
        SIM.keyboard = False
        if SIM.layar == "daftar_autostart" and SIM.cari_aktif:  # seperti HyperOS: Back keluar dari mode Cari
            SIM.cari_aktif, SIM.cari_teks = False, ""

    def back(self):
        SIM.transisi = None
        if SIM.keyboard:  # Back menutup papan ketik dulu
            SIM.keyboard = False
            return
        if SIM.layar.startswith("d_"):
            demo_kembali()
            return
        SIM.layar = "home" if SIM.layar in ("laporan", "memuat", "pengaturan", "mengambang") else "launcher"

    def update_settings(self, pengaturan):
        SIM.panggilan.append(("pengaturan_appium", dict(pengaturan)))

    def get_window_size(self):
        return {"width": 1220, "height": 2712}

    def execute_script(self, nama, args=None):
        args = args or {}
        if nama == "mobile: getSystemBars":
            return {"statusBar": {"visible": True, "x": 0, "y": 0, "width": 1220, "height": 110},
                    "navigationBar": {"visible": True, "x": 0, "y": NAV_Y, "width": 1220, "height": 2712 - NAV_Y}}
        SIM.panggilan.append(("script", nama, args.get("direction")))
        if nama == "mobile: scrollGesture":
            if SIM.layar == "home":
                SIM.tergulir = args["direction"] == "down"
            elif SIM.app == "demo":
                demo_gulir(args["direction"])
        return False

    def tap(self, posisi, duration=None):
        SIM.panggilan.append(("tap", posisi))
        x, y = posisi[0]
        if SIM.layar == "info_app" and 1020 <= x <= 1160 and 760 <= y <= 840 and not SIM.info_tanpa_autostart:
            SIM.autostart.add(SIM.info_pkg)
            SIM.panggilan.append(("autostart_info", SIM.info_pkg))
            return
        if SIM.layar == "daftar_autostart" and 37 <= x <= 1183:  # ketuk di mana pun pada baris = sakelar
            for pkg, atas in baris_autostart():
                if atas <= y <= atas + 180:
                    SIM.autostart.add(pkg)
                    SIM.panggilan.append(("autostart_daftar", pkg))
            return
        if SIM.layar == "pengaturan" and 1020 <= x <= 1160 and 760 <= y <= 840:
            SIM.overlay_ui = True
            SIM.panggilan.append(("sakelar_overlay", True))
            return
        if y >= NAV_Y:  # bilah navigasi 3 tombol HP: kiri = terbaru, tengah = Home, kanan = Back
            SIM.panggilan.append(("tombol_sistem", "home" if 407 <= x < 813 else "back" if x >= 813 else "terbaru"))
            if 407 <= x < 813:
                SIM.transisi, SIM.layar = None, "launcher"
            elif x >= 813:
                self.back()
            return
        if y < 110:
            return
        if SIM.layar == "w_pemilih":
            for e in SIM.pohon().iter():
                b = [int(v) for v in re.findall(r"-?\d+", e.get("bounds") or "")]
                if e.get("clickable") == "true" and len(b) == 4 and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    email = next((c.get("text") for c in e.iter() if "@" in (c.get("text") or "")), None)
                    if email:
                        SIM.panggilan.append(("pilih_akun_google", email))
                        SIM.layar = "w_memuat"
                        SIM.jadwal("w_home", 0.8, lambda: setattr(SIM, "wasiat_login", email))
                    return
        kena = []  # elemen yang bisa diklik di titik itu; yang digambar paling atas = terakhir dalam urutan dokumen,
        for e in SIM.pohon().iter():  # kecuali lapisan penutup dialog (selebar layar, tanpa isi): selalu paling bawah
            b = [int(v) for v in re.findall(r"-?\d+", e.get("bounds") or "")]
            if e.get("clickable") == "true" and len(b) == 4 and b[0] <= x < b[2] and b[1] <= y < b[3]:
                kena.append((e, b == [0, 0, 1220, 2712] and len(e) == 0))
        biasa = [e for e, penutup in kena if not penutup]
        if biasa:
            El(biasa[-1]).click()
        elif kena:
            SIM.panggilan.append(("tap_penghalang", SIM.layar))
            El(kena[-1][0]).click()

    def start_recording_screen(self, **o):
        SIM.panggilan.append(("rekam_mulai", o))
        SIM.mulai_rekam = time.perf_counter()

    def stop_recording_screen(self, **o):
        detik = time.perf_counter() - (SIM.mulai_rekam or time.perf_counter())
        return base64.b64encode(video_palsu(detik)).decode()

    def quit(self):
        SIM.panggilan.append(("quit",))


# ====================================================================== aplikasi demo (penjelajah otomatis + CRUD)
def _d(cls, **a):
    return node(cls, pkg=PKG_DEMO, **a)


def _nav_demo(aktif):
    hasil = ""
    for (x1, x2), nama in zip(((48, 423), (423, 797), (797, 1172)), ("Dasbor", "Peta", "Profil")):
        tombol = _d("android.widget.Button", content_desc=nama, clickable="true", selected="true" if nama == aktif else "false",
                    bounds=f"[{x1},2324][{x2},2540]")
        hasil += _d("android.view.View", clickable="true", bounds=f"[{x1},2324][{x2},2540]", anak=tombol)
    return hasil


def _geser(b, dy, batas_atas=600):
    x1, y1, x2, y2 = b
    return None if y2 - dy <= batas_atas else f"[{x1},{max(y1 - dy, batas_atas)}][{x2},{y2 - dy}]"


def demo_layar():
    L = SIM.layar
    judul = lambda teks: _d("android.view.View", content_desc=teks, heading="true", bounds="[160,120][1000,260]")
    kembali = _d("android.widget.Button", content_desc="Kembali", clickable="true", bounds="[20,130][140,250]")
    if L == "d_home":
        dy = 600 if SIM.d_tergulir else 0
        isi = ""
        for k, j in enumerate(SIM.d_pesanan):
            b = _geser((40, 620 + k * 180, 1180, 780 + k * 180), dy)
            if b:
                isi += _d("android.view.View", content_desc=j, clickable="true", bounds=b)
        y = 620 + len(SIM.d_pesanan) * 180 + 20  # tombol sesudah daftar; "Laporan Bulanan" di bawah bilah navigasi HP
        for lb, b0 in (("Tombol Mati", (40, y, 1180, y + 140)),
                       ("Laporan Bulanan", (40, max(2580, y + 180), 1180, max(2580, y + 180) + 120))):
            b = _geser(b0, dy)
            if b:
                isi += _d("android.widget.Button", content_desc=lb, clickable="true", bounds=b)
        return layar(judul("Dasbor Demo"),
                     _d("android.widget.Button", content_desc="Notifikasi", clickable="true", bounds="[1040,130][1180,250]"),
                     _d("android.widget.EditText", hint="Cari pesanan", clickable="true", bounds="[40,280][1180,420]"),
                     _d("android.widget.Button", content_desc="Tambah Pesanan", clickable="true", bounds="[40,440][600,580]"),
                     _d("android.widget.Button", content_desc="Muat ulang", clickable="true", bounds="[620,440][1180,580]"),
                     _d("android.view.View", scrollable="true", bounds="[0,600][1220,2540]", anak=isi),
                     _nav_demo("Dasbor"), pkg=PKG_DEMO)
    if L == "d_peta":
        tanggal = (_d("android.widget.Button", content_desc="Tanggal", clickable="true", bounds="[40,460][600,600]")
                   if SIM.d_kalender else "")
        return layar(judul("Peta"),
                     _d("android.widget.Button", content_desc="Filter Nama", clickable="true", bounds="[40,300][600,440]"),
                     _d("android.widget.Button", content_desc="Sinkronkan", clickable="true", bounds="[620,300][1180,440]"),
                     tanggal, _nav_demo("Peta"), pkg=PKG_DEMO)
    if L == "d_kalender":
        return layar_kalender()
    if L == "d_profil":
        return layar(judul("Profil"),
                     _d("android.widget.Switch", content_desc="Fingerprint", clickable="true", checkable="true",
                        checked="true" if SIM.d_fingerprint else "false", bounds="[40,300][1180,440]"),
                     _d("android.widget.Button", content_desc="Ubah kata sandi", clickable="true", bounds="[40,460][1180,600]"),
                     _d("android.widget.Button", content_desc="Keluar", clickable="true", bounds="[40,620][1180,760]"),
                     _nav_demo("Profil"), pkg=PKG_DEMO)
    if L == "d_notif":
        return layar(kembali, judul("Notifikasi"),
                     _d("android.view.View", content_desc="Total tagihan: NaN", bounds="[40,300][1180,400]"), pkg=PKG_DEMO)
    if L == "d_laporan":
        return layar(kembali, judul("Laporan Bulanan"),
                     _d("android.view.View", content_desc="Sudah 9251 hari di atas toleransi", bounds="[40,300][1180,400]"),
                     pkg=PKG_DEMO)
    if L == "d_detail":
        return layar(kembali, judul(f"Detail {SIM.d_detail}"),
                     _d("android.widget.Button", content_desc="Hapus", clickable="true", bounds="[40,300][600,440]"),
                     _d("android.widget.Button", content_desc="Selesaikan", clickable="true", bounds="[620,300][1180,440]"),
                     pkg=PKG_DEMO)
    if L == "d_form":
        return layar(kembali, judul("Tambah Pesanan"),
                     _d("android.widget.EditText", hint="Judul pesanan", text=SIM.d_form, clickable="true",
                        bounds="[40,300][1180,440]"),
                     _d("android.widget.Button", content_desc="Simpan", clickable="true", bounds="[40,460][1180,600]"),
                     pkg=PKG_DEMO)
    if L == "d_sandi":
        return layar(kembali, judul("Ubah Kata Sandi"),
                     _d("android.widget.EditText", hint="Kata sandi lama", password="true", clickable="true", bounds="[40,300][1180,440]"),
                     _d("android.widget.EditText", hint="Kata sandi baru", password="true", clickable="true", bounds="[40,460][1180,600]"),
                     _d("android.widget.Button", content_desc="Simpan", clickable="true", bounds="[40,620][1180,760]"),
                     pkg=PKG_DEMO)
    if L == "d_sheet":  # lembar bawah modal: layar di bawahnya tidak ada di pohon aksesibilitas
        opsi = "".join(_d("android.view.View", content_desc=n, clickable="true", bounds=f"[0,{1500 + k * 150}][1220,{1640 + k * 150}]")
                       for k, n in enumerate(("Semua", "Ani", "Budi", "Citra", "Dewi")))
        return layar(_d("android.view.View", content_desc="Pilih Nama", heading="true", bounds="[40,1380][1180,1480]"), opsi,
                     PENGHALANG, pkg=PKG_DEMO)
    raise KeyError(L)


PENGHALANG = node("android.view.View", pkg=PKG_DEMO, content_desc="Tutup", clickable="true", dismissable="true",
                  bounds="[0,0][1220,2712]")  # ModalBarrier Flutter: terakhir di urutan dokumen, digambar paling bawah
HARI_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
BULAN_ID = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
            "November", "Desember")
KOLOM_KAL = ((24, 191), (191, 359), (359, 526), (526, 694), (694, 861), (861, 1029), (1029, 1196))


def layar_kalender():
    """Pemilih rentang tanggal layar penuh seperti showDateRangePicker Flutter (struktur dari HP sungguhan):
    kepala tetap, kisi tanggal di ScrollView, lapisan penutup 'Tutup' paling akhir di urutan dokumen."""
    import datetime as _dt
    pilih = "Beralih ke kalender" if SIM.d_kal_masukan else "Beralih ke masukan"
    kepala = (_d("android.widget.Button", content_desc="Tutup", clickable="true", bounds="[0,120][168,264]")
              + _d("android.widget.Button", content_desc="Simpan", clickable="true" if len(SIM.d_kal_pilih) == 2 else "false",
                   enabled="true" if len(SIM.d_kal_pilih) == 2 else "false", bounds="[902,120][1196,264]")
              + _d("android.view.View", content_desc="Pilih rentang tanggal Tanggal Mulai to Tanggal Akhir",
                   bounds="[216,264][1028,480]")
              + _d("android.widget.Button", content_desc=pilih, clickable="true", bounds="[1052,300][1196,444]"))
    if SIM.d_kal_masukan:
        isi = (_d("android.widget.EditText", hint="Tanggal Mulai", clickable="true", bounds="[40,560][1180,700]")
               + _d("android.widget.EditText", hint="Tanggal Akhir", clickable="true", bounds="[40,740][1180,880]"))
    else:
        bulan, tahun, y, blok = 11 + SIM.d_kal_geser, 2026, 606, ""
        while bulan < 1:
            bulan, tahun = bulan + 12, tahun - 1
        while bulan > 12:
            bulan, tahun = bulan - 12, tahun + 1
        while y < 2570:
            awal = _dt.date(tahun, bulan, 1)
            hari_ke = (awal.weekday() + 1) % 7  # kolom pertama = Minggu
            n_hari = ((_dt.date(tahun + bulan // 12, bulan % 12 + 1, 1)) - awal).days
            baris = (hari_ke + n_hari + 6) // 7
            for d in range(1, n_hari + 1):
                pos = hari_ke + d - 1
                x1, x2 = KOLOM_KAL[pos % 7]
                y1 = y + 174 + (pos // 7) * 150
                if y1 >= 606 and y1 + 126 <= 2570:
                    tgl = _dt.date(tahun, bulan, d)
                    lb = f"{d}, {HARI_ID[tgl.weekday()]}, {d} {BULAN_ID[bulan - 1]} {tahun}"
                    if tgl == _dt.date(2026, 11, 12):
                        lb += ", Hari ini"
                    blok += _d("android.view.View", content_desc=lb, clickable="true", bounds=f"[{x1},{y1}][{x2},{y1 + 126}]")
            y += 174 + baris * 150 + 12
            bulan, tahun = (1, tahun + 1) if bulan == 12 else (bulan + 1, tahun)
        isi = _d("android.widget.ScrollView", scrollable="true", bounds="[0,606][1220,2570]", anak=blok)
    dialog = _d("android.view.View", bounds="[0,0][1220,2712]",
                anak=_d("android.view.View", bounds="[0,0][1220,480]", anak=kepala) + isi)
    return layar(dialog, PENGHALANG, pkg=PKG_DEMO)


def demo_gulir(arah):
    if SIM.layar == "d_home":
        SIM.d_tergulir = arah == "down"
    elif SIM.layar == "d_kalender" and not SIM.d_kal_masukan:
        SIM.d_kal_geser = max(-6, min(6, SIM.d_kal_geser + (1 if arah == "down" else -1)))


def demo_log(tingkat, tag, pesan):
    SIM.logcat.append((time.time(), 4242, tingkat, tag, pesan))


def demo_klik(cd):
    L = SIM.layar
    tab = {"Dasbor": "d_home", "Peta": "d_peta", "Profil": "d_profil"}
    if cd in tab and L in ("d_home", "d_peta", "d_profil"):
        SIM.layar = tab[cd]
    elif cd == "Kembali":
        demo_kembali()
    elif L == "d_home":
        if cd == "Notifikasi":
            SIM.layar = "d_notif"
        elif cd == "Tambah Pesanan":
            SIM.layar, SIM.d_form = "d_form", ""
        elif cd == "Muat ulang":
            demo_log("I", "flutter", "DioException [bad response]: status code 500 saat memuat /api/pesanan")
        elif cd == "Laporan Bulanan":
            SIM.layar = "d_laporan"
        elif cd in SIM.d_pesanan:
            SIM.layar, SIM.d_detail = "d_detail", cd
    elif L == "d_kalender":
        if cd == "Tutup":
            SIM.layar = "d_peta"
        elif cd in ("Beralih ke masukan", "Beralih ke kalender"):
            SIM.d_kal_masukan = cd == "Beralih ke masukan"
        elif cd == "Simpan":
            SIM.panggilan.append(("demo_simpan_tanggal", list(SIM.d_kal_pilih)))
            SIM.layar = "d_peta"
        elif re.match(r"\d+, ", cd):
            SIM.d_kal_pilih = (SIM.d_kal_pilih + [cd])[-2:] if len(SIM.d_kal_pilih) < 2 else [cd]
            SIM.panggilan.append(("demo_pilih_tanggal", cd))
    elif L == "d_peta":
        if cd == "Tanggal":
            SIM.layar, SIM.d_kal_geser, SIM.d_kal_pilih, SIM.d_kal_masukan = "d_kalender", 0, [], False
        elif cd == "Filter Nama":
            SIM.layar = "d_sheet"
        elif cd == "Sinkronkan":
            demo_log("E", "AndroidRuntime", "FATAL EXCEPTION: main")
            demo_log("E", "AndroidRuntime", "Process: id.contoh.demo, PID: 4242")
            demo_log("E", "AndroidRuntime", "java.lang.IllegalStateException: sinkronisasi gagal")
            SIM.layar = "launcher"
    elif L == "d_sheet":
        SIM.layar = "d_peta"
    elif L == "d_profil":
        if cd == "Fingerprint":
            SIM.d_fingerprint = not SIM.d_fingerprint
        elif cd == "Ubah kata sandi":
            SIM.layar = "d_sandi"
        elif cd == "Keluar":
            SIM.panggilan.append(("demo_keluar",))
    elif L == "d_form" and cd == "Simpan":
        if SIM.d_form.strip():
            SIM.d_pesanan.append(SIM.d_form.strip())
            SIM.panggilan.append(("demo_buat", SIM.d_form.strip()))
        SIM.layar = "d_home"
    elif L == "d_detail":
        if cd == "Hapus":
            SIM.d_pesanan.remove(SIM.d_detail)
            SIM.panggilan.append(("demo_hapus", SIM.d_detail))
            SIM.layar = "d_home"
        elif cd == "Selesaikan":
            SIM.panggilan.append(("demo_selesaikan", SIM.d_detail))
    elif L == "d_sandi" and cd == "Simpan":
        SIM.panggilan.append(("demo_ganti_sandi",))


def demo_kembali():
    SIM.layar = {"d_notif": "d_home", "d_laporan": "d_home", "d_detail": "d_home", "d_form": "d_home",
                 "d_sheet": "d_peta", "d_kalender": "d_peta", "d_sandi": "d_profil"}.get(SIM.layar, "launcher")


def demo_reset():
    SIM.d_pesanan = [f"Pesanan #{i}" for i in range(1, 7)]
    SIM.d_tergulir, SIM.d_fingerprint, SIM.d_detail, SIM.d_form = False, False, None, ""
    SIM.d_kalender, SIM.d_kal_geser, SIM.d_kal_pilih, SIM.d_kal_masukan = False, 0, [], False


def adb_palsu(*args, timeout=60):
    s = " ".join(args)
    SIM.panggilan.append(("adb", s))
    jawaban = {"getprop ro.product.manufacturer": "Xiaomi\n", "getprop ro.product.model": "Redmi Note 13 Pro 5G\n",
               "getprop ro.build.version.release": "14\n", "wm density": "Physical density: 440\n"}
    for k, v in jawaban.items():
        if k in s:
            return v
    if s == "shell date +%s":
        return f"{int(time.time())}\n"
    if s.startswith("shell pidof"):
        return "4242\n" if args[-1] in (PKG, PKG_WASIAT, PKG_DEMO) else ""
    if args[:1] == ("logcat",) or args[:1] == ["logcat"]:
        sejak = float(args[args.index("-T") + 1]) if "-T" in args else 0.0
        return "".join(f"{w:.3f}  {pid}  {pid} {lv} {tag}: {msg}\n" for w, pid, lv, tag, msg in SIM.logcat if w >= sejak)
    if s == "shell settings get secure navigation_mode":
        return "0\n"
    if s == "shell cmd uimode night":
        return f"Night mode: {SIM.tema}\n"
    if s.startswith("shell cmd uimode night "):
        SIM.tema = args[-1]
        return f"Night mode: {SIM.tema}\n"
    if s == "shell settings get global airplane_mode_on":
        return "1\n" if SIM.pesawat else "0\n"
    if s.startswith("shell cmd connectivity airplane-mode"):
        SIM.pesawat = args[-1] == "enable"
        return ""
    if s.startswith("shell pm path"):
        return f"package:/data/app/~~sim/{args[-1]}-1/base.apk\n" if args[-1] in (PKG, PKG_WASIAT, PKG_DEMO) else ""
    if "dumpsys package" in s:
        if PKG_DEMO in s:
            return "    versionName=0.9.0\n"
        if PKG_WASIAT in s:
            return "    versionName=1.0.5\n"
        if PKG not in s:
            return ""
        diminta = IZIN_DIMINTA + ["android.permission.SYSTEM_ALERT_WINDOW",
                                  "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"]
        return ("    versionName=2.2.27\n    requested permissions:\n"
                + "".join(f"      {x}\n" for x in ["android.permission.INTERNET"] + diminta)
                + "    install permissions:\n      android.permission.INTERNET: granted=true\n"
                + "    User 0: installed=true\n      runtime permissions:\n"
                + "".join(f"        {x}: granted={'true' if x in SIM.izin_runtime else 'false'}, flags=[ ]\n"
                          for x in IZIN_DIMINTA))
    if s.startswith("shell pm grant"):
        izin = args[-1]
        if "BACKGROUND" in izin and "android.permission.ACCESS_FINE_LOCATION" not in SIM.izin_runtime:
            return "Exception occurred while executing 'grant': java.lang.SecurityException\n"
        SIM.izin_runtime.add(izin)
        return ""
    if s.startswith("shell appops set"):
        SIM.appops.add(args[-2])
        return ""
    if "am force-stop" in s:
        SIM.layar = "launcher"
        SIM.transisi = None
        return ""
    if "pm clear" in s and PKG_WASIAT in s:
        SIM.wasiat_login = None
        return "Success\n"
    if "pm clear" in s:
        SIM.gerbang_ok = SIM.gerbang_lembar = SIM.gerbang_centang = SIM.gerbang_konfirmasi = False
        SIM.izin_runtime.clear()
        SIM.appops.clear()
        SIM.overlay_ui = False
        SIM.login_sebagai, SIM.izin_tertunda = None, True
        SIM.reset_login()
        return "Success\n"
    if "APPLICATION_DETAILS_SETTINGS" in s:
        SIM.layar, SIM.info_pkg = "info_app", s.split("package:")[-1].strip()
        return "Starting: Intent { act=android.settings.APPLICATION_DETAILS_SETTINGS }\n"
    if "am start" in s and PKG_KEAMANAN in s:
        if SIM.daftar_autostart_tidak_ada:
            return "Error: Activity class {com.miui.securitycenter/...} does not exist.\n"
        SIM.layar, SIM.cari_aktif, SIM.cari_teks = "daftar_autostart", False, ""
        return "Starting: Intent { cmp=com.miui.securitycenter/... }\n"
    if "input keyevent KEYCODE_HOME" in s:
        SIM.layar = "launcher"
        return ""
    if "am start" in s:
        SIM.app = "wasiat" if PKG_WASIAT in s else "demo" if PKG_DEMO in s else "santamaria"
        SIM.buka()
        return "Status: ok\nLaunchState: COLD\nTotalTime: 812\nWaitTime: 820\nComplete\n" if "-W" in s else "Starting\n"
    return ""


def adb_mentah_palsu(args, timeout=60):
    """Lapisan adb paling bawah -> (kode_keluar, stdout, stderr), meniru adb sungguhan: status sambungan
    dari SIM.perangkat; galat adb (HP tidak ada / unauthorized / offline / lebih dari satu) masuk stderr."""
    args = [str(a) for a in args]
    s = " ".join(args)
    if SIM.adb_tidak_ada:
        return None, "", "[WinError 2] The system cannot find the file specified"
    if args[:1] == ["devices"]:
        SIM.panggilan.append(("adb", s))
        return 0, "List of devices attached\n" + "".join(f"{a}\t{b}\n" for a, b in SIM.perangkat) + "\n", ""
    if args[:1] == ["reconnect"]:
        SIM.panggilan.append(("adb", s))
        if SIM.pulih_setelah_reconnect:
            SIM.perangkat = [(a, "device" if b == "offline" else b) for a, b in SIM.perangkat]
        return 0, "", ""
    siap = [a for a, b in SIM.perangkat if b == "device"]
    if not siap:
        SIM.panggilan.append(("adb", s))
        status = {b for _, b in SIM.perangkat}
        return 1, "", ("adb.exe: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set\n"
                       if "unauthorized" in status else "adb.exe: device offline\n" if "offline" in status
                       else "adb.exe: no devices/emulators found\n")
    if len(siap) > 1 and os.environ.get("ANDROID_SERIAL") not in siap:
        SIM.panggilan.append(("adb", s))
        return 1, "", "adb.exe: more than one device/emulator\n"
    return 0, adb_palsu(*args, timeout=timeout), ""


def pasang(agen_uji):
    """Arahkan agen_uji ke simulator: adb palsu + appium.webdriver.Remote palsu."""
    import appium.webdriver as wd_asli
    agen_uji._adb_mentah = adb_mentah_palsu
    agen_uji.JEDA_CEK_HP = 0
    wd_asli.Remote = Driver
    return SIM
