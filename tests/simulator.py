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
    # ---- aplikasi Wasiat (login Google SSO)
    "w_login": layar(
        node("android.view.View", pkg=PKG_WASIAT, content_desc="Wasiat", heading="true", bounds="[0,300][1220,500]"),
        node("android.view.View", pkg=PKG_WASIAT, content_desc="Buat dan simpan wasiat dengan aman", bounds="[80,600][1140,700]"),
        node("android.widget.Button", pkg=PKG_WASIAT, content_desc="Masuk dengan Google", clickable="true",
             bounds="[80,2200][1140,2340]"), pkg=PKG_WASIAT),
    "w_pemilih": layar(
        node("android.widget.TextView", pkg=PKG_GMS, text="Pilih akun", bounds="[80,1500][1140,1580]"),
        node("android.widget.LinearLayout", pkg=PKG_GMS, clickable="true", bounds="[0,1600][1220,1760]", anak=(
            node("android.widget.TextView", pkg=PKG_GMS, text="Nevin Uji", bounds="[160,1610][1100,1680]")
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
        if self.layar == "login":
            return self.pohon_login
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
        if SIM.layar == "izin" and tx == "Izinkan":
            SIM.izin_tertunda = False
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
        elif SIM.layar == "home" and cd == "Keluar":
            SIM.login_sebagai = None
            SIM.buka()

    def clear(self):
        self.e.set("text", "")

    def send_keys(self, t):
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
        if SIM.layar.startswith("w_"):
            SIM.pohon()  # jalankan transisi yang sudah jatuh tempo
            return PKG_GMS if SIM.layar == "w_pemilih" else PKG_WASIAT
        return {"launcher": "com.miui.home", "izin": "com.google.android.permissioncontroller"}.get(SIM.layar, PKG)

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

    def back(self):
        SIM.transisi = None
        SIM.layar = "home" if SIM.layar in ("laporan", "memuat") else "launcher"

    def get_window_size(self):
        return {"width": 1220, "height": 2712}

    def execute_script(self, nama, args):
        SIM.panggilan.append(("script", nama, args.get("direction")))
        if nama == "mobile: scrollGesture" and SIM.layar == "home":
            SIM.tergulir = args["direction"] == "down"
        return False

    def tap(self, posisi, duration=None):
        SIM.panggilan.append(("tap", posisi))
        x, y = posisi[0]
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

    def start_recording_screen(self, **o):
        SIM.panggilan.append(("rekam_mulai", o))
        SIM.mulai_rekam = time.perf_counter()

    def stop_recording_screen(self, **o):
        detik = time.perf_counter() - (SIM.mulai_rekam or time.perf_counter())
        return base64.b64encode(video_palsu(detik)).decode()

    def quit(self):
        SIM.panggilan.append(("quit",))


def adb_palsu(*args, timeout=60):
    s = " ".join(args)
    SIM.panggilan.append(("adb", s))
    jawaban = {"getprop ro.product.manufacturer": "Xiaomi\n", "getprop ro.product.model": "Redmi Note 13 Pro 5G\n",
               "getprop ro.build.version.release": "14\n", "wm density": "Physical density: 440\n"}
    for k, v in jawaban.items():
        if k in s:
            return v
    if "dumpsys package" in s:
        return "    versionName=1.0.5\n" if PKG_WASIAT in s else ("    versionName=2.2.27\n" if PKG in s else "")
    if "am force-stop" in s:
        SIM.layar = "launcher"
        SIM.transisi = None
        return ""
    if "pm clear" in s and PKG_WASIAT in s:
        SIM.wasiat_login = None
        return "Success\n"
    if "pm clear" in s:
        SIM.login_sebagai, SIM.izin_tertunda = None, True
        SIM.reset_login()
        return "Success\n"
    if "am start" in s:
        SIM.app = "wasiat" if PKG_WASIAT in s else "santamaria"
        SIM.buka()
        return "Status: ok\nLaunchState: COLD\nTotalTime: 812\nWaitTime: 820\nComplete\n" if "-W" in s else "Starting\n"
    return ""


def pasang(agen_uji):
    """Arahkan agen_uji ke simulator: adb palsu + appium.webdriver.Remote palsu."""
    import appium.webdriver as wd_asli
    agen_uji.adb = adb_palsu
    wd_asli.Remote = Driver
    return SIM
