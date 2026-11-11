#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agen_uji.py - Executor uji UI/UX untuk aplikasi Santa Maria, Yayasan Santa Maria, dan Wasiat.

Peran skrip ini dalam pipeline n8n:
  1. LLM Perancang melihat layar (perintah `lihat`) lalu menyusun rencana uji berformat JSON.
  2. Skrip ini MENJALANKAN rencana itu di HP lewat Appium (perintah `jalankan`) dan menyimpan
     screenshot, struktur layar (XML), waktu respons, serta metrik aksesibilitas/kerumitan
     di setiap langkah.
  3. LLM Evaluator menilai hasilnya, lalu membuat laporan atau meminta uji ulang yang spesifik.

Skrip ini satu-satunya komponen yang menyentuh HP dan memegang akun uji. LLM cukup menyebut
nama peran (misalnya "owner"); kata sandi tidak pernah masuk ke rencana maupun ke LLM.

Perintah:
  python agen_uji.py skema [--app santamaria]                  format rencana, untuk prompt LLM
  python agen_uji.py lihat --app santamaria [--peran owner]     observasi layar untuk LLM Perancang
  python agen_uji.py jalankan rencana.json [--rekam]            jalankan rencana uji
  python agen_uji.py analisis layar.xml [--density 2.75]        analisis file XML tanpa HP

Output untuk n8n: progres ditulis ke stderr, dan baris stdout berisi satu objek JSON ringkas.
Hasil lengkap disimpan di ~/.n8n-files/uji-ui/ (folder yang secara default boleh dibaca n8n 2.x).
Kode keluar: 0 = uji selesai (berhasil atau gagal, lihat "status"), 2 = masalah persiapan.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

APPIUM_URL = os.environ.get("APPIUM_URL", "http://127.0.0.1:4723")
FOLDER_DASAR = Path(os.environ.get("UJI_UI_FOLDER") or Path.home() / ".n8n-files" / "uji-ui")
FILE_AKUN = Path(os.environ.get("UJI_UI_AKUN") or Path(__file__).resolve().with_name("akun.json"))
MIN_TARGET_DP = 48          # pedoman Android: area sentuh minimal 48 x 48 dp
MAKS_LANGKAH = 40
UIA = "-android uiautomator"  # = AppiumBy.ANDROID_UIAUTOMATOR
XPATH = "xpath"               # = AppiumBy.XPATH

AKSI = ("buka_aplikasi", "login", "reset_aplikasi", "tap", "ketik", "gulir", "kembali", "tunggu", "tangkap",
        "atur_skala_font")
ARAH = {"bawah": "down", "atas": "up", "kiri": "left", "kanan": "right"}
LABEL_TOMBOL_LOGIN = ("Masuk", "Login", "Log In", "Sign In", "Masuk Sekarang")
# Tombol lanjut/persetujuan di layar Google saat login SSO (dicocokkan persis atau sebagai awalan).
LABEL_LANJUT_SSO = ("Lanjutkan", "Continue", "Lanjut", "Setuju", "I agree", "Agree", "Izinkan", "Allow",
                    "Berikutnya", "Next", "OK")
LABEL_IZIN = ("Izinkan", "Allow", "Saat aplikasi digunakan", "While using the app",
              "Hanya kali ini", "Only this time")
POLA_DIALOG_IZIN = 'packageNameMatches(".*(permissioncontroller|packageinstaller).*")'
KATA_KOLOM_KODE = re.compile(r"\bkode\b|organisasi|instansi", re.I)
XPATH_KOLOM_SANDI = '//android.widget.EditText[@password="true"]'
FILE_STATUS_LOGIN = FOLDER_DASAR / "status_login.json"
# Tombol yang mengubah data, membuka aplikasi lain, atau mengakhiri sesi. Diblokir kecuali diizinkan.
KATA_BERISIKO = re.compile(
    r"\b(hapus|delete|remove|bayar|pay|kirim|send|submit|simpan|save|keluar|logout|log ?out|sign ?out|"
    r"whatsapp|telepon|panggil|call|setujui|approve|tolak|reject|batalkan|konfirmasi|checkout|"
    r"pesan sekarang|beli)\b", re.I)

CATATAN_METODE = (
    "respon_ms = waktu dari aksi (tap/ketik) sampai kondisi tujuan terpenuhi (tunggu_muncul/"
    "tunggu_hilang), diukur dengan polling Appium; sudah termasuk overhead Appium (kira-kira "
    "0,1-0,5 detik), jadi bandingkan antar-run di HP yang sama, bukan sebagai angka absolut. "
    "sampai_stabil_ms = sampai struktur layar berhenti berubah (dicek tiap 0,4 detik). "
    "startup.total_time_ms = waktu cold start menurut Android (am start -W). "
    "Metrik aksesibilitas dihitung dari struktur elemen (UiAutomator), bukan dari pengujian TalkBack."
)


class Gagal(Exception):
    """Masalah persiapan: Appium mati, HP tidak terdeteksi, konfigurasi salah."""


class HentiTunggu(Exception):
    """Dilempar dari dalam kondisi tunggu untuk menghentikan langkah dengan pesan yang jelas."""


def log(*bagian):
    print(*bagian, file=sys.stderr, flush=True)


def _slug(teks, maks=60):
    s = re.sub(r"[^a-z0-9]+", "_", str(teks).lower()).strip("_")
    return (s or "langkah")[:maks]


# ====================================================================== adb & perangkat
def adb(*args, timeout=60):
    """Jalankan perintah adb dan kembalikan output-nya ('' kalau gagal)."""
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def info_perangkat(package):
    def prop(nama):
        return adb("shell", "getprop", nama).strip()

    dpi = re.findall(r"density:\s*(\d+)", adb("shell", "wm", "density"))  # override (kalau ada) di akhir
    versi = re.search(r"versionName=(\S+)", adb("shell", "dumpsys", "package", package))
    return {
        "merek": prop("ro.product.manufacturer"),
        "model": prop("ro.product.model"),
        "android": prop("ro.build.version.release"),
        "dpi": int(dpi[-1]) if dpi else None,
        "versi_aplikasi": versi.group(1) if versi else None,
    }


def baca_status_login():
    try:
        return json.loads(FILE_STATUS_LOGIN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tulis_status_login(package, peran):
    """Catat peran yang sedang login di aplikasi (None = tidak ada sesi login)."""
    data = baca_status_login()
    if peran:
        data[package] = {"peran": peran, "waktu": datetime.now().isoformat(timespec="seconds")}
    else:
        data.pop(package, None)
    try:
        FILE_STATUS_LOGIN.parent.mkdir(parents=True, exist_ok=True)
        FILE_STATUS_LOGIN.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log("  status login tidak bisa disimpan:", e)


def nilai_rahasia(cfg):
    """Semua nilai akun (email, HP, kode, kata sandi) yang tidak boleh terkirim ke LLM."""
    nilai = {cfg.get("password_default")}
    for app in cfg.get("aplikasi", {}).values():
        for akun in app.get("akun", {}).values():
            nilai.update(str(akun[k]) for k in ("email", "hp", "kode", "password", "google_akun") if akun.get(k))
    return {v for v in nilai if v and len(v) >= 4}


def baca_skala_font():
    try:
        return float(adb("shell", "settings", "get", "system", "font_scale").strip())
    except ValueError:
        return 1.0


def label_berisiko(label):
    return bool(label) and KATA_BERISIKO.search(label) is not None


# ====================================================================== analisis XML
KELAS_INTERAKTIF = {
    "android.widget.Button", "android.widget.ImageButton", "android.widget.EditText",
    "android.widget.CheckBox", "android.widget.RadioButton", "android.widget.Switch",
    "android.widget.ToggleButton", "android.widget.Spinner",
}


def _bounds(teks):
    angka = [int(x) for x in re.findall(r"-?\d+", teks or "")]
    return angka[:4] if len(angka) >= 4 else None


def _label(el):
    # Untuk kolom input, isi 'text' adalah nilai ketikan, bukan label.
    if el.get("class") == "android.widget.EditText":
        return (el.get("content-desc") or "").strip()
    return (el.get("text") or el.get("content-desc") or "").strip()


def _ukuran_dp(el, density):
    b = _bounds(el.get("bounds"))
    if not b or not density:
        return None
    return round((b[2] - b[0]) / density, 1), round((b[3] - b[1]) / density, 1)


def _root(xml):
    return ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)


def analisis_xml(xml, density=None, maks_daftar=25):
    """Hitung metrik aksesibilitas dan kerumitan dari page source Appium."""
    root = _root(xml)
    semua = []  # (elemen, kedalaman)

    def jelajah(el, dalam):
        if el.tag != "hierarchy":
            semua.append((el, dalam))
        for anak in el:
            jelajah(anak, dalam + (0 if el.tag == "hierarchy" else 1))

    jelajah(root, 1)
    terlihat = [el for el, _ in semua if el.get("displayed", "true") == "true"]

    def ringkas(el):
        info = {"kelas": (el.get("class") or "").split(".")[-1], "label": _label(el) or None,
                "hint": el.get("hint") or None, "bounds": el.get("bounds")}
        u = _ukuran_dp(el, density)
        if u:
            info["ukuran_dp"] = f"{u[0]}x{u[1]}"
        return info

    interaktif = [el for el in terlihat
                  if el.get("clickable") == "true" or el.get("long-clickable") == "true"
                  or el.get("class") in KELAS_INTERAKTIF]
    id_interaktif = {id(el) for el in interaktif}

    tanpa_label, hanya_hint, target_kecil, tombol_mati = [], [], [], []
    for el in interaktif:
        if not _label(el):
            if el.get("class") == "android.widget.EditText" and el.get("hint"):
                hanya_hint.append(ringkas(el))
            else:
                tanpa_label.append(ringkas(el))
        u = _ukuran_dp(el, density)
        if u and el.get("clickable") == "true" and (u[0] < MIN_TARGET_DP or u[1] < MIN_TARGET_DP):
            target_kecil.append(ringkas(el))
        if (el.get("class") in ("android.widget.Button", "android.widget.ImageButton")
                and el.get("clickable") != "true" and el.get("enabled") == "true"):
            tombol_mati.append(ringkas(el))

    teks = [_label(el) for el in terlihat if _label(el) and id(el) not in id_interaktif]
    return {
        "jumlah_node": len(semua),
        "kedalaman_maks": max((d for _, d in semua), default=0),
        "elemen_terlihat": len(terlihat),
        "elemen_interaktif": len(interaktif),
        "elemen_teks": len(teks),
        "jumlah_heading": sum(1 for el in terlihat if el.get("heading") == "true"),
        "rasio_interaktif_berlabel": (round(1 - len(tanpa_label) / len(interaktif), 2)
                                      if interaktif else None),
        "density": density,
        "masalah": {
            "interaktif_tanpa_label": tanpa_label[:maks_daftar],
            "input_label_hanya_dari_hint": hanya_hint[:maks_daftar],
            "target_sentuh_di_bawah_48dp": target_kecil[:maks_daftar],
            "tombol_tidak_bisa_diklik": tombol_mati[:maks_daftar],
        },
        "teks_di_layar": teks[:60],
    }


def daftar_elemen(xml, density=None, maks=80, kotak=False):
    """Daftar elemen ringkas untuk LLM Perancang: hanya yang terlihat dan bermakna.
    kotak=True menambahkan bounds [x1, y1, x2, y2] (dipakai untuk menggambar nomor di screenshot)."""
    hasil = []
    for el in _root(xml).iter():
        if el.tag == "hierarchy" or el.get("displayed", "true") != "true":
            continue
        kelas = el.get("class") or ""
        label, hint = _label(el), el.get("hint")
        bisa_klik = el.get("clickable") == "true"
        kolom = kelas == "android.widget.EditText"
        if not (label or hint or bisa_klik or kolom or el.get("scrollable") == "true"):
            continue
        item = {"no": len(hasil) + 1, "kelas": kelas.split(".")[-1], "label": label or None}
        if hint:
            item["hint"] = hint
        if kolom and el.get("password") != "true" and el.get("text"):
            item["isi"] = el.get("text")
        for attr, kunci in (("clickable", "bisa_diklik"), ("selected", "terpilih"),
                            ("password", "kata_sandi"), ("scrollable", "bisa_digulir"),
                            ("checked", "tercentang")):
            if el.get(attr) == "true":
                item[kunci] = True
        if el.get("enabled") == "false":
            item["nonaktif"] = True
        if el.get("resource-id"):
            item["id"] = el.get("resource-id")
        b = _bounds(el.get("bounds"))
        if b:
            item["pusat"] = [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]
            if kotak:
                item["kotak"] = b
        u = _ukuran_dp(el, density)
        if u:
            item["ukuran_dp"] = f"{u[0]}x{u[1]}"
        hasil.append(item)
        if len(hasil) >= maks:
            break
    return hasil


def elemen_di_titik(xml, x, y):
    """Label elemen terdalam yang bisa diklik dan memuat titik (x, y)."""
    terbaik = None
    for el in _root(xml).iter():
        b = _bounds(el.get("bounds"))
        if b and el.get("clickable") == "true" and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            luas = (b[2] - b[0]) * (b[3] - b[1])
            if terbaik is None or luas <= terbaik[0]:
                terbaik = (luas, el.get("content-desc") or el.get("text") or "")
    return terbaik[1] if terbaik else ""


# ====================================================================== target elemen
def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _kelas_penuh(k):
    if "." in k:
        return k
    return "android.view.View" if k == "View" else f"android.widget.{k}"


def kandidat_locator(t):
    """Ubah target dari rencana menjadi daftar locator yang dicoba berurutan."""
    if not isinstance(t, dict) or not t:
        raise ValueError('target harus berupa objek, misalnya {"label": "Masuk"}')
    kls = f'className("{_kelas_penuh(t["kelas"])}")' if t.get("kelas") else None
    akhir = f'.instance({int(t["urutan"])})' if t.get("urutan") is not None else ""
    hasil = []
    if t.get("id"):
        hasil.append((UIA, f'new UiSelector().resourceId("{_esc(t["id"])}"){akhir}'))
    for kunci, metode in (("label", ("description", "text")),
                          ("label_mengandung", ("descriptionContains", "textContains"))):
        if t.get(kunci):
            for m in metode:
                bagian = [b for b in (kls, f'{m}("{_esc(t[kunci])}")') if b]
                hasil.append((UIA, "new UiSelector()." + ".".join(bagian) + akhir))
    if t.get("hint"):
        h = str(t["hint"])
        kutip = "'" if '"' in h else '"'
        hasil.append((XPATH, f"(//*[@hint={kutip}{h}{kutip}])[{int(t.get('urutan') or 0) + 1}]"))
    if not hasil and kls:
        hasil.append((UIA, f"new UiSelector().{kls}{akhir}"))
    if not hasil and "koordinat" not in t:
        raise ValueError("target butuh salah satu: label, label_mengandung, hint, id, kelas, atau koordinat")
    return hasil


# ====================================================================== validasi rencana
def validasi_rencana(r, cfg, izinkan_berisiko=False):
    if not isinstance(r, dict):
        return ["rencana harus berupa objek JSON"]
    salah = []
    apps = cfg.get("aplikasi", {})
    app = r.get("aplikasi")
    if app not in apps:
        salah.append(f'"aplikasi" harus salah satu dari: {", ".join(apps)}')
    langkah = r.get("langkah")
    if not isinstance(langkah, list) or not langkah:
        return salah + ['"langkah" harus berupa daftar yang tidak kosong']
    if len(langkah) > MAKS_LANGKAH:
        salah.append(f"maksimal {MAKS_LANGKAH} langkah per rencana (ada {len(langkah)})")
    akun = apps.get(app, {}).get("akun", {})
    sandi = {cfg.get("password_default")} | {a.get("password") for a in akun.values()}
    sandi.discard(None)
    ada_login = bool(r.get("peran"))
    for i, s in enumerate(langkah, 1):
        if not isinstance(s, dict):
            salah.append(f"langkah {i}: harus berupa objek")
            continue
        aksi = s.get("aksi")
        if aksi not in AKSI:
            salah.append(f"langkah {i}: aksi '{aksi}' tidak dikenal (pilihan: {', '.join(AKSI)})")
            continue
        try:
            if aksi == "tap":
                t = s.get("target")
                if isinstance(t, dict) and "koordinat" in t:
                    k = t["koordinat"]
                    if not (isinstance(k, list) and len(k) == 2 and all(isinstance(x, (int, float)) for x in k)):
                        raise ValueError("koordinat harus [x, y]")
                else:
                    kandidat_locator(t)
            if aksi == "ketik":
                kandidat_locator(s.get("target"))
            for k in ("tunggu_muncul", "tunggu_hilang"):
                if k in s:
                    kandidat_locator(s[k])
        except ValueError as e:
            salah.append(f"langkah {i}: {e}")
        if aksi == "tap" and not izinkan_berisiko and isinstance(s.get("target"), dict):
            lb = " ".join(str(s["target"].get(k) or "") for k in ("label", "label_mengandung"))
            if label_berisiko(lb):
                salah.append(f"langkah {i}: tap '{lb.strip()}' diblokir karena bisa mengubah data/keluar dari "
                             "aplikasi; uji tanpa menekan tombol itu")
        if aksi == "login":
            if s.get("peran") not in akun:
                salah.append(f"langkah {i}: peran '{s.get('peran')}' tidak ada untuk aplikasi {app} "
                             f"(pilihan: {', '.join(akun)})")
            ada_login = True
        if aksi == "ketik":
            if "teks" not in s and s.get("dari_akun") not in ("email", "hp", "kode", "password"):
                salah.append(f"langkah {i}: 'ketik' butuh 'teks' atau 'dari_akun' (email/hp/kode/password)")
            if "dari_akun" in s and not ada_login:
                salah.append(f"langkah {i}: 'dari_akun' butuh langkah login sebelumnya atau 'peran' di rencana")
            if s.get("teks") in sandi:
                salah.append(f"langkah {i}: jangan tulis kata sandi di rencana; pakai \"dari_akun\": \"password\"")
        if aksi == "gulir" and s.get("arah", "bawah") not in ARAH:
            salah.append(f"langkah {i}: arah gulir harus salah satu dari: {', '.join(ARAH)}")
        if aksi == "atur_skala_font":
            try:
                if not 0.85 <= float(s.get("skala", 1.0)) <= 2.0:
                    raise ValueError
            except (TypeError, ValueError):
                salah.append(f"langkah {i}: 'skala' harus angka 0.85-2.0")
        if aksi == "tunggu":
            try:
                if not 0 < float(s.get("detik", 1)) <= 30:
                    raise ValueError
            except (TypeError, ValueError):
                salah.append(f"langkah {i}: 'detik' harus angka 0-30")
    return salah


# ====================================================================== pelaksana
class Pelaksana:
    def __init__(self, driver, cfg, kode_app, folder, info, izinkan_berisiko=False):
        self.d = driver
        self.cfg = cfg
        self.app = cfg["aplikasi"][kode_app]
        self.pkg = self.app["package"]
        self.folder = folder
        self.density = info["dpi"] / 160 if info.get("dpi") else None
        self.izinkan_berisiko = izinkan_berisiko
        self.langkah = []
        self.peran_aktif = None
        self.xml_terakhir = None
        self.t_acuan = time.perf_counter()  # titik nol waktu (= awal rekaman layar, kalau direkam)
        self.aksi_sekarang = None
        self.rencana_ke = None
        self.skala_font_awal = None
        self.sso = self.app.get("login_metode") == "google_sso"

    # ---------- utilitas
    def cari(self, target):
        for loc in kandidat_locator(target):
            try:
                el = self.d.find_elements(*loc)
            except Exception:
                el = []
            if el:
                return el[0]
        return None

    def ada(self, target):
        return self.cari(target) is not None

    def jumlah(self, ekspresi):
        try:
            return len(self.d.find_elements(UIA, f"new UiSelector().{ekspresi}"))
        except Exception:
            return 0

    @staticmethod
    def tunggu(kondisi, batas, jeda=0.1):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < batas:
            try:
                if kondisi():
                    return True
            except HentiTunggu:
                raise
            except Exception:
                pass
            time.sleep(jeda)
        return False

    def tunggu_elemen(self, target, batas):
        hasil = {}

        def ketemu():
            hasil["el"] = self.cari(target)
            return hasil["el"] is not None

        self.tunggu(ketemu, batas, jeda=0.2)
        return hasil.get("el")

    def tunggu_stabil(self, batas=8.0, jeda=0.4):
        t0 = time.perf_counter()
        sebelumnya = None
        while time.perf_counter() - t0 < batas:
            sekarang = re.sub(r'\s(focused|a11y-focused)="(true|false)"', "", self.d.page_source)
            if sekarang == sebelumnya:
                return True
            sebelumnya = sekarang
            time.sleep(jeda)
        return False

    def paket_aktif(self):
        try:
            return self.d.current_package
        except Exception:
            return None

    def tutup_keyboard(self):
        try:
            if self.d.is_keyboard_shown():
                self.d.hide_keyboard()
        except Exception:
            pass

    def ada_dialog_izin(self):
        return self.jumlah(POLA_DIALOG_IZIN) > 0

    def tutup_dialog_izin(self, maks=4):
        """Tekan 'Izinkan' pada dialog izin sistem Android (notifikasi, lokasi, dll.)."""
        for _ in range(maks):
            if not self.ada_dialog_izin():
                return
            tombol = None
            for lb in LABEL_IZIN:
                el = self.d.find_elements(UIA, f'new UiSelector().{POLA_DIALOG_IZIN}.text("{lb}")')
                if el:
                    tombol = el[0]
                    break
            if tombol is None:
                el = self.d.find_elements(UIA, 'new UiSelector().resourceIdMatches(".*permission_allow.*")')
                tombol = el[0] if el else None
            if tombol is None:
                return
            log("  menekan tombol izin sistem:", tombol.get_attribute("text"))
            tombol.click()
            time.sleep(0.8)

    # ---------- pencatatan langkah
    def ambil(self, dasar):
        hasil = {"screenshot": None, "xml": None, "metrik": None}
        try:
            png = self.folder / f"{dasar}.png"
            self.d.get_screenshot_as_file(str(png))
            hasil["screenshot"] = png.name
        except Exception as e:  # noqa: BLE001
            log("  gagal mengambil screenshot:", e)
        try:
            xml = self.d.page_source
            (self.folder / f"{dasar}.xml").write_text(xml, encoding="utf-8")
            hasil["xml"] = f"{dasar}.xml"
            hasil["metrik"] = analisis_xml(xml, self.density)
            self.xml_terakhir = xml
        except Exception as e:  # noqa: BLE001
            log("  gagal mengambil struktur layar:", e)
        return hasil

    def catat_langkah(self, nama, aksi=None, sampai=None, batas=15.0, stabil=True,
                      catatan="", tambahan=None):
        """Jalankan satu aksi, ukur waktunya, lalu simpan screenshot + XML + metrik."""
        nama = _slug(nama)
        t0 = time.perf_counter()
        t_mulai = round((t0 - self.t_acuan) * 1000)
        berhasil, galat, respon, stabil_dt = True, None, None, None
        try:
            if aksi:
                aksi()
            if sampai:
                berhasil = self.tunggu(sampai, batas)
                if not berhasil:
                    galat = f"kondisi tujuan tidak terpenuhi dalam {batas:g} detik"
            respon = time.perf_counter() - t0
            if stabil and berhasil:
                self.tunggu_stabil()
                stabil_dt = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001 - kegagalan apa pun dicatat sebagai hasil uji
            berhasil = False
            pesan = str(e).splitlines()[0][:300] if str(e) else ""
            galat = pesan if isinstance(e, (HentiTunggu, RuntimeError)) and pesan else f"{type(e).__name__}: {pesan}"
            respon = time.perf_counter() - t0

        no = len(self.langkah) + 1
        tangkapan = self.ambil(f"{no:02d}_{_slug(nama)}")
        rekaman = {
            "no": no, "langkah": nama, "aksi": self.aksi_sekarang, "rencana_ke": self.rencana_ke,
            "berhasil": berhasil, "t_mulai_ms": t_mulai,
            "respon_ms": round(respon * 1000) if (sampai and respon is not None) else None,
            "sampai_stabil_ms": round(stabil_dt * 1000) if stabil_dt is not None else None,
            "durasi_aksi_ms": round(respon * 1000) if (not sampai and respon is not None) else None,
            "galat": galat, "catatan": catatan or None, **(tambahan or {}), **tangkapan,
        }
        self.langkah.append(rekaman)
        m = (tangkapan["metrik"] or {}).get("masalah", {})
        waktu = rekaman["respon_ms"] if rekaman["respon_ms"] is not None else rekaman["sampai_stabil_ms"]
        log(f"[{no:02d}] {nama:<30} {'OK   ' if berhasil else 'GAGAL'} waktu={waktu} ms "
            f"tanpa_label={len(m.get('interaktif_tanpa_label', []))} "
            f"target_kecil={len(m.get('target_sentuh_di_bawah_48dp', []))}"
            + (f"  ({galat})" if galat else ""))
        return berhasil

    def gagal_langkah(self, nama, pesan):
        def lempar():
            raise RuntimeError(pesan)
        self.catat_langkah(nama, aksi=lempar, stabil=False)
        return False

    # ---------- eksekusi rencana
    def jalankan(self, rencana):
        for i, s in enumerate(rencana["langkah"], 1):
            if not self.satu_langkah(i, s) and not s.get("lanjut_jika_gagal"):
                return False
        return all(l["berhasil"] for l in self.langkah)

    def kondisi_dari(self, s):
        muncul, hilang = s.get("tunggu_muncul"), s.get("tunggu_hilang")
        if not muncul and not hilang:
            return None
        return lambda: (not muncul or self.ada(muncul)) and (not hilang or not self.ada(hilang))

    def satu_langkah(self, i, s):
        aksi = s["aksi"]
        self.aksi_sekarang, self.rencana_ke = aksi, i
        nama = _slug(s.get("nama") or f"{aksi}_{i}")
        batas = float(s.get("batas_detik", 15))
        sampai = self.kondisi_dari(s)
        catatan = s.get("catatan", "")

        if aksi == "buka_aplikasi":
            return self.buka_aplikasi(nama, s.get("cold_start", True), sampai, batas)
        if aksi == "login":
            return self.login(nama, s["peran"], s.get("bersihkan_jika_perlu", True),
                              s.get("paksa_login_ulang", False))
        if aksi == "reset_aplikasi":
            def reset():
                adb("shell", "pm", "clear", self.pkg)
                tulis_status_login(self.pkg, None)
            return self.catat_langkah(nama, aksi=reset, stabil=False, catatan="data aplikasi dihapus (pm clear)")
        if aksi == "kembali":
            return self.catat_langkah(nama, aksi=self.d.back, sampai=sampai, batas=batas, catatan=catatan)
        if aksi == "tunggu":
            return self.catat_langkah(nama, aksi=lambda: time.sleep(float(s.get("detik", 1))),
                                      stabil=False, catatan=catatan)
        if aksi == "tangkap":
            return self.catat_langkah(nama, stabil=False, catatan=catatan)
        if aksi == "atur_skala_font":
            skala = float(s.get("skala", 1.0))

            def atur():
                if self.skala_font_awal is None:
                    self.skala_font_awal = baca_skala_font()
                adb("shell", "settings", "put", "system", "font_scale", f"{skala:g}")
            return self.catat_langkah(nama, aksi=atur, sampai=sampai, batas=batas,
                                      catatan=(catatan + f" | skala font sistem {skala:g}").strip(" |"))
        if aksi == "gulir":
            return self.catat_langkah(nama, aksi=lambda: self.gulir(s.get("arah", "bawah"),
                                                                     int(s.get("jumlah", 1))),
                                      sampai=sampai, batas=batas, catatan=catatan)

        target = s["target"]
        if aksi == "tap" and "koordinat" in target:
            x, y = (int(v) for v in target["koordinat"])
            if not self.izinkan_berisiko:
                try:
                    lb = elemen_di_titik(self.d.page_source, x, y)
                except Exception:
                    lb = ""
                if label_berisiko(lb):
                    return self.gagal_langkah(nama, f"tap diblokir: elemen '{lb}' bisa mengubah data/keluar dari aplikasi")
            return self.catat_langkah(nama, aksi=lambda: self.d.tap([(x, y)]), sampai=sampai,
                                      batas=batas, catatan=(catatan + " | tap koordinat").strip(" |"))
        # Cari elemen dulu (di luar pengukuran waktu), baru lakukan aksi.
        el = self.tunggu_elemen(target, float(s.get("batas_cari_detik", 10)))
        if el is None:
            return self.gagal_langkah(nama, f"elemen tidak ditemukan: {json.dumps(target, ensure_ascii=False)}")
        if aksi == "tap":
            if not self.izinkan_berisiko:
                try:
                    lb = el.get_attribute("content-desc") or el.get_attribute("text") or ""
                except Exception:
                    lb = ""
                if label_berisiko(lb):
                    return self.gagal_langkah(nama, f"tap diblokir: elemen '{lb}' bisa mengubah data/keluar dari aplikasi")
            return self.catat_langkah(nama, aksi=el.click, sampai=sampai, batas=batas, catatan=catatan)

        # aksi == "ketik"
        rahasia = s.get("dari_akun") == "password"
        nilai = s["teks"] if "teks" in s else self.nilai_akun(s["dari_akun"])
        if nilai is None:
            return self.gagal_langkah(nama, f"nilai '{s.get('dari_akun')}' tidak ada di akun peran aktif")

        def isi():
            el.click()
            if s.get("hapus_dulu", True):
                try:
                    el.clear()
                except Exception:
                    pass
            el.send_keys(nilai)
            if s.get("tutup_keyboard"):
                self.tutup_keyboard()

        return self.catat_langkah(nama, aksi=isi, sampai=sampai, batas=batas, stabil=False,
                                  catatan=(catatan + " | input; durasi termasuk waktu mengetik Appium"
                                           + (" | nilai rahasia" if rahasia else "")).strip(" |"))

    def nilai_akun(self, kunci):
        akun = self.app.get("akun", {}).get(self.peran_aktif or "", {})
        if kunci == "password":
            return akun.get("password") or self.cfg.get("password_default")
        return akun.get(kunci)

    def gulir(self, arah, jumlah):
        ukuran = self.d.get_window_size()
        area = {"left": int(ukuran["width"] * 0.1), "top": int(ukuran["height"] * 0.25),
                "width": int(ukuran["width"] * 0.8), "height": int(ukuran["height"] * 0.5)}
        for _ in range(max(1, min(jumlah, 10))):
            self.d.execute_script("mobile: scrollGesture", {**area, "direction": ARAH[arah], "percent": 0.8})

    # ---------- aksi khusus
    def buka_aplikasi(self, nama, cold, sampai, batas):
        if cold:
            adb("shell", "am", "force-stop", self.pkg)
            time.sleep(1.0)
        startup = {}

        def buka():
            out = adb("shell", "am", "start", "-W", "-n", f'{self.pkg}/{self.app["activity"]}')
            m = re.search(r"TotalTime:\s*(\d+)", out)
            startup["total_time_ms"] = int(m.group(1)) if m else None
            m = re.search(r"LaunchState:\s*(\w+)", out)
            startup["launch_state"] = m.group(1) if m else None

        def siap():
            return self.jumlah(f'packageName("{self.pkg}").clickable(true)') > 0 or self.ada_dialog_izin()

        ok = self.catat_langkah(
            nama, aksi=buka, sampai=sampai or siap, batas=max(batas, 30), tambahan={"startup": startup},
            catatan=("cold start; " if cold else "") + "respon_ms = sampai layar pertama punya elemen yang bisa diklik",
        )
        self.tutup_dialog_izin()
        return ok

    def form_login_ada(self):
        try:
            return len(self.d.find_elements(XPATH, XPATH_KOLOM_SANDI)) > 0
        except Exception:
            return False

    def kolom_login(self):
        kolom = {"identitas": None, "sandi": None, "kode": None}
        for el in self.d.find_elements(UIA, 'new UiSelector().className("android.widget.EditText")'):
            if el.get_attribute("password") == "true":
                kolom["sandi"] = kolom["sandi"] or el
                continue
            teks = " ".join(filter(None, [el.get_attribute("hint"), el.get_attribute("content-desc")]))
            if kolom["kode"] is None and "@" not in teks and KATA_KOLOM_KODE.search(teks):
                kolom["kode"] = el
            elif kolom["identitas"] is None:
                kolom["identitas"] = el
        return kolom

    def tombol_login(self):
        for lb in LABEL_TOMBOL_LOGIN:
            for t in ({"kelas": "Button", "label": lb}, {"label": lb}):
                el = self.cari(t)
                if el is not None and el.get_attribute("clickable") == "true":
                    return el
        return None

    def tombol_sso(self):
        lb = _esc(self.app.get("label_tombol_sso", "Google"))
        for metode in ("descriptionContains", "textContains"):
            try:
                el = self.d.find_elements(UIA, f'new UiSelector().packageName("{self.pkg}").{metode}("{lb}")')
            except Exception:
                el = []
            if el:
                return el[0]
        return None

    def layar_login_ada(self):
        return self.tombol_sso() is not None if self.sso else self.form_login_ada()

    def langkah_sso(self, target, keadaan):
        """Tangani satu layar milik Google (pemilih akun / persetujuan) saat login SSO."""
        luar = [el for el in _root(self.d.page_source).iter()
                if el.tag != "hierarchy" and (el.get("package") or "") not in ("", self.pkg) and _bounds(el.get("bounds"))]
        if not luar:
            return
        if any(el.get("password") == "true" for el in luar):
            raise HentiTunggu("Google meminta kata sandi: masuk dulu ke akun Google di HP (Setelan > Akun), lalu ulangi")

        def teks(el):
            return (el.get("text") or el.get("content-desc") or "").strip()

        def klik(el):
            b = _bounds(el.get("bounds"))
            self.d.tap([((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)])
            keadaan["aksi"] += 1
            time.sleep(0.8)

        akun_google = [el for el in luar if "@" in teks(el)]
        if target and akun_google:
            akun_google = [el for el in akun_google if target.lower() in teks(el).lower()]
            if not akun_google and not keadaan["email_diklik"]:
                raise HentiTunggu("akun Google yang diatur di akun.json (google_akun) tidak ada di pemilih akun HP")
        if akun_google and keadaan["email_diklik"] < 2:
            keadaan["email_diklik"] += 1
            klik(akun_google[0])
            return
        for lb in LABEL_LANJUT_SSO:
            for el in luar:
                t = teks(el).lower()
                if t == lb.lower() or t.startswith(lb.lower() + " "):
                    klik(el)
                    return
        if keadaan["aksi"] > 8:
            raise HentiTunggu("alur login Google tidak dikenali (cek screenshot langkah ini)")

    def login_google(self, nama, peran, akun):
        tombol = self.tombol_sso()
        if tombol is None:
            return self.gagal_langkah(f"{nama}_google", "tombol masuk dengan Google tidak ditemukan "
                                                         "(atur 'label_tombol_sso' di akun.json)")
        target = str(akun.get("google_akun") or "").strip()
        keadaan = {"email_diklik": 0, "aksi": 0}

        def selesai():
            paket = self.paket_aktif()
            if paket == self.pkg:
                return (self.tombol_sso() is None
                        and self.jumlah(f'packageName("{self.pkg}").clickable(true)') > 0)
            if paket and re.search(r"permissioncontroller|packageinstaller", paket):
                self.tutup_dialog_izin()
                return False
            self.langkah_sso(target, keadaan)
            return False

        ok = self.catat_langkah(
            f"{nama}_google", aksi=tombol.click, sampai=selesai, batas=60, tambahan={"privasi": True},
            catatan=f"login {peran} lewat Google SSO; waktu termasuk pemilihan akun oleh skrip; "
                    "layar Google tidak disimpan/dikirim ke LLM")
        if ok:
            tulis_status_login(self.pkg, peran)
            self.tutup_dialog_izin()
        return ok

    def login(self, nama, peran, bersihkan, paksa_ulang=False):
        akun = self.app.get("akun", {}).get(peran)
        if akun is None:
            return self.gagal_langkah(nama, f"peran '{peran}' tidak ada di akun.json")
        self.peran_aktif = peran

        # 1. Pastikan aplikasi di depan dan form login tampil.
        if self.paket_aktif() != self.pkg:
            adb("shell", "am", "start", "-n", f'{self.pkg}/{self.app["activity"]}')
            self.tunggu(lambda: self.paket_aktif() == self.pkg, 15)
        self.tutup_dialog_izin()
        siap = self.tunggu(lambda: self.layar_login_ada()
                           or self.jumlah(f'packageName("{self.pkg}").clickable(true)') > 0, 15)
        if siap and not self.layar_login_ada():
            self.tunggu(self.layar_login_ada, 2)  # beri waktu kalau aplikasi baru mengalihkan ke login
        if not self.layar_login_ada():
            sebelumnya = baca_status_login().get(self.pkg, {}).get("peran")
            if sebelumnya == peran and not paksa_ulang:
                log(f"  masih login sebagai {peran} dari uji sebelumnya; login tidak diulang")
                return self.catat_langkah(f"{nama}_sudah_login", stabil=False,
                                          catatan=f"sesi login {peran} sebelumnya masih aktif; login tidak diulang")
            if not bersihkan:
                return self.gagal_langkah(nama, "form login tidak ditemukan (kemungkinan masih login)")
            log("  form login tidak ada (kemungkinan masih login akun lain); menghapus data aplikasi lalu membuka ulang")
            adb("shell", "pm", "clear", self.pkg)
            tulis_status_login(self.pkg, None)
            adb("shell", "am", "start", "-W", "-n", f'{self.pkg}/{self.app["activity"]}')
            self.tunggu(lambda: self.layar_login_ada() or self.ada_dialog_izin(), 30)
            self.tutup_dialog_izin()
            if not self.tunggu(self.layar_login_ada, 20):
                return self.gagal_langkah(nama, "layar login tidak muncul setelah data aplikasi dihapus")

        if self.sso:
            return self.login_google(nama, peran, akun)

        # 2. Tab peran (misalnya Konsumen/Personel di aplikasi Santa Maria).
        tab = akun.get("tab_login") or self.app.get("tab_login_default")
        target_tab = {"kelas": "Button", "label": tab} if tab else None
        if target_tab and self.ada(target_tab) and self.cari(target_tab).get_attribute("selected") != "true":
            ok = self.catat_langkah(f"{nama}_pilih_tab_{tab}", aksi=lambda: self.cari(target_tab).click(),
                                    sampai=lambda: self.cari(target_tab).get_attribute("selected") == "true",
                                    batas=10)
            if not ok:
                return False

        # 3. Isi form. Nilai diambil dari akun.json, tidak pernah dari rencana.
        kolom = self.kolom_login()
        if kolom["identitas"] is None or kolom["sandi"] is None:
            return self.gagal_langkah(nama, "struktur form login tidak dikenali (butuh kolom identitas dan kata sandi)")
        cara = akun.get("login_pakai") or self.app.get("login_pakai", "email")
        identitas = akun.get(cara) or akun.get("email") or akun.get("hp")
        sandi = akun.get("password") or self.cfg.get("password_default")
        if not identitas or not sandi:
            return self.gagal_langkah(nama, f"akun '{peran}' belum lengkap di akun.json")

        def isi(bagian, nilai):
            def _isi():
                el = self.kolom_login()[bagian]
                el.click()
                try:
                    el.clear()
                except Exception:
                    pass
                el.send_keys(nilai)
            return _isi

        catatan_input = "input; durasi termasuk waktu mengetik Appium"
        if not self.catat_langkah(f"{nama}_isi_identitas", aksi=isi("identitas", identitas),
                                  stabil=False, catatan=catatan_input):
            return False
        if kolom["kode"] is not None and akun.get("kode"):
            if not self.catat_langkah(f"{nama}_isi_kode", aksi=isi("kode", akun["kode"]),
                                      stabil=False, catatan=catatan_input):
                return False
        if not self.catat_langkah(f"{nama}_isi_sandi", aksi=isi("sandi", sandi), stabil=False,
                                  catatan=catatan_input + " | nilai rahasia"):
            return False
        self.tutup_keyboard()

        # 4. Tekan tombol login dan ukur sampai form login hilang.
        tombol = self.tombol_login()
        if tombol is None:
            return self.gagal_langkah(f"{nama}_tekan_masuk", "tombol login tidak ditemukan")
        ok = self.catat_langkah(f"{nama}_tekan_masuk", aksi=tombol.click,
                                sampai=lambda: not self.form_login_ada(), batas=30,
                                catatan=f"login sebagai {peran}; respon_ms = sampai form login hilang")
        if ok:
            tulis_status_login(self.pkg, peran)
            self.tutup_dialog_izin()
        else:
            log("  masih di form login: cek screenshot langkah terakhir (pesan error atau akun salah?)")
        return ok

    # ---------- rekaman layar
    def mulai_rekam(self):
        # Maks. 180 detik: rekaman lebih panjang butuh ffmpeg di PATH server Appium untuk menyambung potongan.
        try:
            self.d.start_recording_screen(timeLimit="180", bitRate="2000000")
            return True
        except Exception as e:  # noqa: BLE001
            log("rekaman layar tidak bisa dimulai:", str(e)[:200])
            return False

    def selesai_rekam(self):
        try:
            data = self.d.stop_recording_screen()
            if data:
                (self.folder / "rekaman.mp4").write_bytes(base64.b64decode(data))
                return "rekaman.mp4"
        except Exception as e:  # noqa: BLE001
            log("rekaman layar gagal disimpan:", str(e)[:200])
        return None


# ====================================================================== perintah
def muat_konfigurasi(path):
    path = Path(path)
    if not path.exists():
        raise Gagal(f"file akun tidak ditemukan: {path} (salin akun.contoh.json menjadi akun.json lalu isi akunnya)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Gagal(f"akun.json tidak valid: {e}") from e


def sambung_appium():
    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
    except ImportError as e:
        raise Gagal("library Appium belum terpasang; jalankan: pip install Appium-Python-Client") from e
    opsi = UiAutomator2Options()
    opsi.no_reset = True
    opsi.new_command_timeout = 600
    try:
        return webdriver.Remote(APPIUM_URL, options=opsi)
    except Exception as e:  # noqa: BLE001
        raise Gagal(f"gagal menyambung ke Appium di {APPIUM_URL}: {str(e).splitlines()[0][:200]} "
                    "(pastikan 'appium' sedang jalan dan sesi Inspector sudah ditutup)") from e


def jalankan_rencana(rencana, cfg, rekam=False, folder=None, izinkan_berisiko=False):
    salah = validasi_rencana(rencana, cfg, izinkan_berisiko)
    if salah:
        return {"status": "rencana_tidak_valid", "berhasil": False, "kesalahan": salah}

    kode_app = rencana["aplikasi"]
    app = cfg["aplikasi"][kode_app]
    info = info_perangkat(app["package"])
    if not info["model"]:
        raise Gagal("HP tidak terdeteksi oleh adb; cek dengan: adb devices")
    if not info["versi_aplikasi"]:
        raise Gagal(f"aplikasi {app['package']} tidak terpasang di HP")

    folder = Path(folder) if folder else (
        FOLDER_DASAR / f"{datetime.now():%Y%m%d_%H%M%S}_{kode_app}_{_slug(rencana.get('judul', 'uji'))}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rencana.json").write_text(json.dumps(rencana, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"Menyambung ke Appium di {APPIUM_URL} ...")
    driver = sambung_appium()
    p = Pelaksana(driver, cfg, kode_app, folder, info, izinkan_berisiko)
    p.peran_aktif = rencana.get("peran")
    mulai = datetime.now().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    berhasil, video = False, None
    sedang_rekam = rekam and p.mulai_rekam()
    p.t_acuan = time.perf_counter()
    try:
        berhasil = p.jalankan(rencana)
    finally:
        if sedang_rekam:
            video = p.selesai_rekam()
        if p.skala_font_awal is not None:
            adb("shell", "settings", "put", "system", "font_scale", f"{p.skala_font_awal:g}")
            log(f"skala font sistem dikembalikan ke {p.skala_font_awal:g}")
        ringkasan = {
            "aplikasi": {"kode": kode_app, "nama": app.get("nama"), "package": app["package"],
                         "versi": info.get("versi_aplikasi")},
            "perangkat": info,
            "judul": rencana.get("judul"),
            "tujuan": rencana.get("tujuan"),
            "peran": p.peran_aktif,
            "waktu_mulai": mulai,
            "berhasil": berhasil,
            "durasi_total_ms": round((time.perf_counter() - t0) * 1000),
            "rekaman": video,
            "izinkan_aksi_berisiko": izinkan_berisiko,
            "catatan_metode": CATATAN_METODE,
            "langkah": p.langkah,
            "layar_akhir": daftar_elemen(p.xml_terakhir, p.density) if p.xml_terakhir else [],
        }
        (folder / "ringkasan.json").write_text(json.dumps(ringkasan, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
        try:
            driver.quit()
        except Exception:
            pass

    gagal = next((l for l in p.langkah if not l["berhasil"]), None)
    log(f"\n{'BERHASIL' if berhasil else 'TIDAK BERHASIL'}. Hasil: {folder}")
    return {
        "status": "selesai" if berhasil else "gagal_langkah",
        "berhasil": berhasil,
        "folder": str(folder),
        "ringkasan": str(folder / "ringkasan.json"),
        "jumlah_langkah": len(p.langkah),
        "langkah_gagal": ({"no": gagal["no"], "langkah": gagal["langkah"], "galat": gagal["galat"],
                           "screenshot": gagal["screenshot"]} if gagal else None),
        "rekaman": video,
    }


def perintah_lihat(args, cfg):
    langkah = []
    if args.peran:
        langkah += [{"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
                    {"aksi": "login", "nama": "login", "peran": args.peran}]
    elif args.buka:
        langkah.append({"aksi": "buka_aplikasi", "nama": "buka_aplikasi"})
    langkah.append({"aksi": "tangkap", "nama": "layar"})
    hasil = jalankan_rencana({"aplikasi": args.app, "judul": f"lihat_{args.peran or 'layar'}",
                              "langkah": langkah}, cfg)
    if hasil.get("folder"):
        r = json.loads(Path(hasil["ringkasan"]).read_text(encoding="utf-8"))
        akhir = r["langkah"][-1]
        hasil["layar"] = {
            "screenshot": str(Path(hasil["folder"]) / akhir["screenshot"]) if akhir["screenshot"] else None,
            "elemen": r["layar_akhir"],
            "metrik": akhir["metrik"],
        }
    return hasil


SKEMA = """FORMAT RENCANA UJI (JSON) UNTUK agen_uji.py

{
  "aplikasi": "<kode aplikasi>",
  "judul": "nama_singkat_tanpa_spasi",
  "tujuan": "apa yang ingin dinilai dari uji ini",
  "peran": "(opsional) peran aktif kalau rencana tidak diawali login",
  "langkah": [ ... maksimal 40 langkah ... ]
}

AKSI YANG TERSEDIA
- {"aksi":"buka_aplikasi","nama":"..."}                      tutup paksa lalu buka aplikasi; mengukur waktu startup
- {"aksi":"login","nama":"...","peran":"<peran>"}            login sebagai peran; email & kata sandi diisi otomatis
- {"aksi":"reset_aplikasi","nama":"..."}                     hapus data aplikasi (logout paksa)
- {"aksi":"tap","nama":"...","target":{...}}
- {"aksi":"ketik","nama":"...","target":{...},"teks":"..."}  atau "dari_akun":"email|hp|kode|password"
- {"aksi":"gulir","nama":"...","arah":"bawah|atas|kiri|kanan","jumlah":1}
- {"aksi":"kembali","nama":"..."}                            tombol back Android
- {"aksi":"tunggu","nama":"...","detik":2}
- {"aksi":"tangkap","nama":"..."}                            hanya ambil screenshot dan struktur layar
- {"aksi":"atur_skala_font","nama":"...","skala":1.3}        ukuran huruf sistem 0.85-2.0 (uji teks besar);
                                                             dikembalikan otomatis di akhir uji

TARGET ELEMEN (utamakan "label" persis dari daftar elemen layar)
  {"label":"Teks persis"}            {"label_mengandung":"potongan teks"}
  {"hint":"placeholder kolom"}       {"id":"resource-id"}
  {"kelas":"EditText","urutan":0}    "kelas"/"urutan" boleh digabung dengan "label"
  {"koordinat":[x,y]}                hanya untuk tap, pilihan terakhir (pakai "pusat" dari daftar elemen)

OPSI PER LANGKAH
  "tunggu_muncul": {target}    elemen yang harus muncul setelah aksi (dipakai mengukur waktu respons)
  "tunggu_hilang": {target}    elemen yang harus hilang setelah aksi
  "batas_detik": 15            batas waktu tunggu
  "lanjut_jika_gagal": true    rencana tetap lanjut walau langkah ini gagal
  "paksa_login_ulang": true    (khusus login) login ulang walau sesi peran yang sama masih aktif
  "catatan": "..."             alasan langkah ini, untuk evaluator

ATURAN
1. Pakai label persis seperti di daftar elemen layar terbaru; jangan menebak elemen yang belum terlihat.
2. Kalau label berisi baris baru, pakai "label_mengandung" dengan potongan satu baris.
3. Jangan pernah menulis kata sandi atau data akun; untuk login pakai aksi "login".
   Tombol yang mengubah data atau keluar dari aplikasi (hapus, simpan, kirim, bayar, keluar,
   WhatsApp, telepon, konfirmasi, ...) diblokir oleh pelaksana.
4. Setiap tap yang berpindah layar sebaiknya punya "tunggu_muncul" supaya waktu responsnya terukur.
5. Beri "nama" yang deskriptif: huruf kecil dan garis bawah.
6. Keluarkan HANYA objek JSON rencana, tanpa teks lain.

CONTOH
{
  "aplikasi": "santamaria",
  "judul": "login_owner_buka_menu",
  "tujuan": "Aksesibilitas dan waktu respons dari login owner sampai beranda",
  "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
    {"aksi": "login", "nama": "login_owner", "peran": "owner"},
    {"aksi": "gulir", "nama": "gulir_beranda", "arah": "bawah"},
    {"aksi": "tangkap", "nama": "beranda_bawah"}
  ]
}
"""


def perintah_skema(args, cfg):
    teks = SKEMA
    apps = cfg.get("aplikasi", {})
    pilihan = [args.app] if args.app else list(apps)
    teks += "\nAPLIKASI DAN PERAN YANG TERSEDIA\n"
    for k in pilihan:
        if k in apps:
            teks += f'- "{k}" ({apps[k].get("nama", k)}): {", ".join(apps[k].get("akun", {}))}\n'
    return teks


def pakai_utf8():
    """Windows: stdout ke n8n/pipe default-nya cp1252; paksa UTF-8 supaya teks Indonesia & simbol aman."""
    for aliran in (sys.stdout, sys.stderr):
        try:
            aliran.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Executor uji UI/UX aplikasi Santa Maria, Yayasan, dan Wasiat")
    p.add_argument("--akun", default=str(FILE_AKUN), help="lokasi akun.json")
    sub = p.add_subparsers(dest="perintah", required=True)

    ps = sub.add_parser("skema", help="cetak format rencana uji untuk prompt LLM")
    ps.add_argument("--app")

    pl = sub.add_parser("lihat", help="observasi layar untuk LLM Perancang")
    pl.add_argument("--app", required=True)
    pl.add_argument("--peran", help="login sebagai peran ini dulu")
    pl.add_argument("--buka", action="store_true", help="buka aplikasi dulu (tanpa login)")

    pj = sub.add_parser("jalankan", help="jalankan rencana uji (file JSON, atau - untuk stdin)")
    pj.add_argument("rencana")
    pj.add_argument("--rekam", action="store_true", help="rekam layar selama uji (maks. 3 menit)")
    pj.add_argument("--izinkan-berisiko", action="store_true",
                    help="izinkan tap tombol yang mengubah data (hapus, simpan, kirim, bayar, keluar, ...)")

    pa = sub.add_parser("analisis", help="analisis file XML struktur layar, tanpa HP")
    pa.add_argument("xml")
    pa.add_argument("--density", type=float, help="faktor density = dpi/160, misalnya 2.75")

    args = p.parse_args(argv)
    pakai_utf8()
    try:
        if args.perintah == "analisis":
            xml = Path(args.xml).read_text(encoding="utf-8")
            print(json.dumps(analisis_xml(xml, args.density), ensure_ascii=False, indent=2))
            return 0
        cfg = muat_konfigurasi(args.akun)
        if args.perintah == "skema":
            print(perintah_skema(args, cfg))
            return 0
        if args.perintah == "lihat":
            if args.app not in cfg.get("aplikasi", {}):
                raise Gagal(f"aplikasi '{args.app}' tidak ada di akun.json")
            hasil = perintah_lihat(args, cfg)
        else:
            sumber = sys.stdin.read() if args.rencana == "-" else Path(args.rencana).read_text(encoding="utf-8")
            try:
                rencana = json.loads(sumber)
            except json.JSONDecodeError as e:
                hasil = {"status": "rencana_tidak_valid", "berhasil": False,
                         "kesalahan": [f"rencana bukan JSON yang valid: {e}"]}
            else:
                hasil = jalankan_rencana(rencana, cfg, rekam=args.rekam, izinkan_berisiko=args.izinkan_berisiko)
        print(json.dumps(hasil, ensure_ascii=False))
        return 0
    except Gagal as e:
        print(json.dumps({"status": "error", "berhasil": False, "pesan": str(e)}, ensure_ascii=False))
        log(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
