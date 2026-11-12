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
FILE_APLIKASI = Path(os.environ.get("UJI_UI_APLIKASI") or Path(__file__).resolve().with_name("aplikasi.json"))
MIN_TARGET_DP = 48          # pedoman Android: area sentuh minimal 48 x 48 dp
MAKS_LANGKAH = 40
UIA = "-android uiautomator"  # = AppiumBy.ANDROID_UIAUTOMATOR
XPATH = "xpath"               # = AppiumBy.XPATH

AKSI = ("buka_aplikasi", "login", "reset_aplikasi", "tap", "ketik", "gulir", "kembali", "tunggu", "tangkap",
        "atur_skala_font", "atur_tema", "atur_jaringan", "pastikan_ada", "pastikan_tidak_ada")
JENIS_UBAH_DATA = ("buat", "ubah", "hapus", "status", "lain", "biarkan")
ARAH = {"bawah": "down", "atas": "up", "kiri": "left", "kanan": "right"}
LABEL_TOMBOL_LOGIN = ("Masuk", "Login", "Log In", "Sign In", "Masuk Sekarang")
# Tombol lanjut/persetujuan di layar Google saat login SSO (dicocokkan persis atau sebagai awalan).
LABEL_LANJUT_SSO = ("Lanjutkan", "Continue", "Lanjut", "Setuju", "I agree", "Agree", "Izinkan", "Allow",
                    "Berikutnya", "Next", "OK")
LABEL_IZIN = ("Izinkan", "Allow", "Saat aplikasi digunakan", "While using the app",
              "Hanya kali ini", "Only this time")
POLA_DIALOG_IZIN = 'packageNameMatches(".*(permissioncontroller|packageinstaller).*")'
# Aplikasi sistem tempat izin khusus diatur (Pengaturan, pengelola izin vendor, penghemat baterai, dll.).
POLA_PENGATURAN = re.compile(r"settings|securitycenter|permissioncontroller|permissionmanager|safecenter|"
                             r"packageinstaller|devicecare|powerkeeper|\.lool", re.I)
POLA_IZINKAN = re.compile(r"izinkan|allow|aktifkan|enable|permit|notifikasi|notification|sepanjang waktu|all the time|"
                          r"mulai otomatis|autostart|auto-start|"
                          r"di atas aplikasi lain|over other apps|semua file|all files|akses|access|tanpa batasan|"
                          r"no restrictions|unrestricted", re.I)
POLA_TOLAK = re.compile(r"\b(jangan|tolak|deny|don't|do not|nonaktifkan|disable|batasi|restricted|hapus|delete)\b", re.I)
# Autostart Xiaomi/Redmi/POCO: izin khusus MIUI/HyperOS, tidak bisa lewat adb -> dinyalakan lewat tampilan.
MEREK_XIAOMI = ("xiaomi", "redmi", "poco")
POLA_AUTOSTART = re.compile(r"mulai otomatis|mulai sendiri|jalankan otomatis|autostart|auto-start|auto start", re.I)
KOMPONEN_AUTOSTART = ("com.miui.securitycenter/com.miui.permcenter.autostart.AutoStartManagementActivity",
                      "com.miui.securitycenter/com.miui.powercenter.autostart.AutoStartManagementActivity")
FILE_AUTOSTART = FOLDER_DASAR / "autostart.json"
# Izin khusus yang tidak bisa diberikan dengan 'pm grant'; diberikan lewat app-ops.
IZIN_APPOPS = {
    "android.permission.SYSTEM_ALERT_WINDOW": "SYSTEM_ALERT_WINDOW",
    "android.permission.MANAGE_EXTERNAL_STORAGE": "MANAGE_EXTERNAL_STORAGE",
    "android.permission.SCHEDULE_EXACT_ALARM": "SCHEDULE_EXACT_ALARM",
    "android.permission.REQUEST_INSTALL_PACKAGES": "REQUEST_INSTALL_PACKAGES",
    "android.permission.PACKAGE_USAGE_STATS": "GET_USAGE_STATS",
    "android.permission.WRITE_SETTINGS": "WRITE_SETTINGS",
    "android.permission.USE_FULL_SCREEN_INTENT": "USE_FULL_SCREEN_INTENT",
}
KATA_KOLOM_KODE = re.compile(r"\bkode\b|organisasi|instansi", re.I)
XPATH_KOLOM_SANDI = '//android.widget.EditText[@password="true"]'
FILE_STATUS_LOGIN = FOLDER_DASAR / "status_login.json"
# Tombol yang SELALU diblokir, juga saat CRUD diizinkan: efek ke luar aplikasi yang tidak bisa ditarik kembali
# (uang, pesan/panggilan ke orang lain) dan tindakan yang menghapus/menonaktifkan akun.
KATA_SELALU_BLOKIR = re.compile(
    r"\b(bayar|pay|payment|checkout|beli|buy|top ?up|whatsapp|telepon|panggil|call|sms|e-?mail|"
    r"hapus akun|delete account|nonaktifkan akun|deactivate account|tutup akun)\b", re.I)
# Kata kerja yang menyimpan/mengubah data atau mengakhiri sesi. Diblokir kecuali boleh_ubah_data; saat diizinkan,
# langkahnya wajib berlabel "ubah_data" supaya tercatat dan dipulihkan.
KATA_UBAH_DATA = re.compile(
    r"\b(simpan|save|kirim|send|submit|ajukan|unggah|upload|hapus|delete|remove|buang|setujui|approve|tolak|reject|"
    r"batalkan|konfirmasi|confirm|akui|acknowledge|selesaikan|resolve|abaikan|dismiss|terima|accept|proses|"
    r"tugaskan|assign|tandai|arsipkan|archive|keluar|logout|log ?out|sign ?out|pesan sekarang)\b", re.I)
# Mengubah data yang SUDAH ADA tidak bisa dipulihkan: hanya boleh pada data buatan uji (layar memuat penandanya).
KATA_UBAH_DATA_LAMA = re.compile(
    r"\b(hapus|delete|remove|buang|setujui|approve|tolak|reject|batalkan|akui|acknowledge|selesaikan|resolve|"
    r"abaikan|dismiss|terima|accept|proses|tugaskan|assign|arsipkan|archive)\b", re.I)
KATA_KELUAR_AKUN = re.compile(r"\b(keluar|logout|log ?out|sign ?out)\b", re.I)
# Label navigasi yang kebetulan memuat kata di atas (mis. "Buka Kirim Pesanan ke SO", "Hapus filter").
KATA_AMAN = re.compile(r"^(buka|lihat|open|view|tampilkan|cari|filter)\b|\bhapus (filter|pencarian|teks|riwayat pencarian)"
                       r"|\breset filter\b", re.I)
KATA_BERISIKO = KATA_UBAH_DATA  # nama lama, dipakai modul lain
POLA_2FA = re.compile(r"verifikasi (2|dua) langkah|\b2fa\b|two[- ]factor|autentikator|authenticator", re.I)
KATA_KIRIM_FORM = re.compile(r"\b(simpan|save|ubah|ganti|perbarui|update|change|kirim|submit|konfirmasi|aktifkan|enable|"
                             r"verifikasi|lanjut|next)\b", re.I)

CATATAN_METODE = (
    "respon_ms = waktu dari aksi (tap/ketik) sampai kondisi tujuan terpenuhi (tunggu_muncul/tunggu_hilang), diukur "
    "dengan polling Appium (termasuk overhead sekitar 0,1-0,5 detik), jadi bandingkan antar-run di HP yang sama. "
    "mulai_berubah_ms = saat struktur layar pertama kali berbeda dari sebelum aksi (respons pertama yang terlihat); "
    "sampai_stabil_ms = saat keadaan akhir layar pertama kali terlihat (dicek tiap 0,3 detik), bukan saat kestabilannya "
    "dikonfirmasi. 'tanpa_perubahan' = aksi tidak mengubah layar sama sekali. Gulir tidak dinilai waktunya (durasi "
    "gestur). startup.total_time_ms = waktu cold start menurut Android (am start -W). Metrik aksesibilitas dihitung "
    "dari struktur elemen (UiAutomator), bukan dari pengujian TalkBack."
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
def cari_adb():
    """Pakai adb yang sama dengan Appium (ANDROID_HOME / ANDROID_SDK_ROOT\\platform-tools). Dua versi adb yang berbeda
    saling mematikan server adb, sehingga HP sesaat 'hilang'. Kalau tidak ada, pakai adb dari PATH."""
    if os.environ.get("ADB"):
        return os.environ["ADB"]
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        akar = os.environ.get(var)
        for nama in (("adb.exe", "adb") if akar else ()):
            p = Path(akar) / "platform-tools" / nama
            if p.is_file():
                return str(p)
    return "adb"


ADB = cari_adb()
SERIAL_PENGGUNA = (os.environ.get("ANDROID_SERIAL") or "").strip()  # HP yang dipilih pengguna sendiri, kalau ada
SERIAL_AKTIF = None  # HP yang sedang dipakai (diisi pastikan_hp, dipakai juga oleh Appium)
JEDA_CEK_HP = 2.0    # detik antar-percobaan membaca 'adb devices'


def _adb_mentah(args, timeout=60):
    """Jalankan adb -> (kode_keluar, stdout, stderr). Kode None = adb tidak bisa dijalankan sama sekali."""
    try:
        r = subprocess.run([ADB, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except OSError as e:
        return None, "", str(e)
    except subprocess.TimeoutExpired:
        return None, "", f"adb tidak menjawab dalam {timeout} detik"


def adb(*args, timeout=60):
    """Jalankan perintah adb, kembalikan stdout+stderr ('' kalau adb tidak bisa dijalankan).
    Untuk aksi yang keluarannya dicek polanya (am start, pm grant, ...)."""
    kode, out, err = _adb_mentah(args, timeout)
    return "" if kode is None else out + err


def adb_baca(*args, timeout=60):
    """Untuk MEMBACA nilai (getprop, dumpsys, pm path): hanya stdout, dan '' kalau perintahnya gagal.
    Pesan error adb seperti 'no devices/emulators found' tidak boleh terbaca sebagai nilai."""
    kode, out, _ = _adb_mentah(args, timeout)
    return out if kode == 0 else ""


def daftar_hp():
    """Isi 'adb devices' sebagai [(serial, status)], atau None kalau adb tidak bisa dijalankan."""
    kode, out, _ = _adb_mentah(["devices"], 30)
    if kode is None:
        return None
    hasil = []
    for baris in out.splitlines():
        baris = baris.strip()
        if not baris or baris.startswith(("*", "List of devices")):
            continue
        m = re.match(r"(\S+)\s+(.+)$", baris)
        if m:
            hasil.append((m.group(1), m.group(2).strip()))
    return hasil


def _serial_usb(serial):
    """HP lewat kabel USB, bukan emulator / adb Wi-Fi ('ip:port' atau '..._adb-tls-connect._tcp')."""
    return not (serial.startswith("emulator-") or ":" in serial or "._adb-tls" in serial)


SARAN_HP = {
    "kosong": ("HP tidak terdeteksi oleh adb (daftar 'adb devices' kosong). Cek: kabel USB tersambung (kabel data, "
               "bukan kabel cas saja), layar HP tidak terkunci, dan 'USB debugging' di Opsi pengembang masih aktif "
               "(HP Xiaomi bisa mematikannya sendiri setelah restart atau update)"),
    "unauthorized": ("HP terhubung tapi adb belum diizinkan (status 'unauthorized'). Buka kunci HP lalu ketuk 'Izinkan' "
                     "di dialog 'Izinkan USB debugging?' (centang 'Selalu izinkan dari komputer ini'). Kalau dialognya "
                     "tidak muncul: cabut-pasang kabel USB, atau di Opsi pengembang ketuk 'Cabut otorisasi USB "
                     "debugging' lalu colok ulang"),
    "offline": ("HP terdeteksi tapi 'offline'. Cabut-pasang kabel USB; kalau masih offline, jalankan 'adb kill-server' "
                "lalu coba lagi"),
}


def pastikan_hp(cfg=None, coba=4, jeda=None):
    """Pastikan ada tepat satu HP yang siap dipakai lewat adb, SEBELUM memeriksa aplikasi atau membuka Appium.
    HP terpilih dipasang di ANDROID_SERIAL (dipakai semua perintah adb berikutnya) dan dikirim ke Appium sebagai udid.
    Melempar Gagal berisi penyebab + cara memperbaikinya kalau HP belum siap."""
    global SERIAL_AKTIF
    jeda = JEDA_CEK_HP if jeda is None else jeda
    pilihan = SERIAL_PENGGUNA or str(((cfg or {}).get("hp") or {}).get("serial") or "").strip()
    hp = []
    for i in range(coba):
        hp = daftar_hp()
        if hp is None:
            raise Gagal(f"adb tidak bisa dijalankan ({ADB}). Pasang Android SDK Platform-Tools dan pastikan adb ada "
                        "di PATH, atau ANDROID_HOME sudah diatur")
        calon = [h for h in hp if h[0] == pilihan] if pilihan else hp
        if any(st == "device" for _, st in calon):
            break
        if any(st == "offline" for _, st in calon):
            _adb_mentah(["reconnect", "offline"], 30)
        if i < coba - 1:
            time.sleep(jeda)  # server adb yang baru menyala butuh beberapa detik untuk mengenali HP
    terlihat = ", ".join(f"{s} ({st})" for s, st in hp) or "tidak ada"
    if pilihan:
        if not any(s == pilihan for s, _ in hp):
            raise Gagal(f"HP dengan serial {pilihan} (dari ANDROID_SERIAL / \"serial\" di aplikasi.json) tidak "
                        f"terhubung; yang terdeteksi adb: {terlihat}")
        hp = [h for h in hp if h[0] == pilihan]
    siap = [s for s, st in hp if st == "device"]
    if not siap:
        status = {st for _, st in hp}
        for kunci in ("unauthorized", "offline"):
            if kunci in status:
                raise Gagal(SARAN_HP[kunci])
        if hp:
            raise Gagal(f"HP terdeteksi tapi statusnya belum 'device' ({terlihat}). Nyalakan ulang HP secara normal "
                        "lalu colok ulang")
        raise Gagal(SARAN_HP["kosong"])
    if len(siap) > 1:
        usb = [s for s in siap if _serial_usb(s)]
        if len(usb) != 1:
            raise Gagal(f"ada {len(siap)} perangkat terhubung ke adb ({terlihat}). Cabut yang tidak dipakai, atau isi "
                        "\"serial\" di bagian \"hp\" pada aplikasi.json dengan serial HP uji")
        log(f"Beberapa perangkat terhubung ({terlihat}); memakai HP lewat USB: {usb[0]}")
        siap = usb
    SERIAL_AKTIF = siap[0]
    os.environ["ANDROID_SERIAL"] = SERIAL_AKTIF
    return SERIAL_AKTIF


POLA_GALAT_ADB = re.compile(r"(?m)^(adb(\.exe)?: |error: )")  # galat dari adb sendiri, bukan dari perintah di HP


def aplikasi_terpasang(package):
    """Cek lewat 'pm path'. Kalau adb sendiri yang gagal (HP terputus), lempar Gagal: jangan dianggap tidak terpasang."""
    kode, out, err = _adb_mentah(["shell", "pm", "path", package], 60)
    if "package:" in out:
        return True
    if kode is None or POLA_GALAT_ADB.search(err):
        galat = (err.strip().splitlines() or ["adb tidak menjawab"])[0]
        raise Gagal(f"sambungan adb ke HP terputus saat memeriksa {package} ({galat}). Cek kabel USB dan buka kunci "
                    "layar HP, lalu jalankan lagi")
    return False


def info_perangkat(package):
    def prop(nama):
        return adb_baca("shell", "getprop", nama).strip()

    dpi = re.findall(r"density:\s*(\d+)", adb_baca("shell", "wm", "density"))  # override (kalau ada) di akhir
    versi = re.search(r"versionName=(\S+)", adb_baca("shell", "dumpsys", "package", package))
    return {
        "merek": prop("ro.product.manufacturer"),
        "model": prop("ro.product.model"),
        "android": prop("ro.build.version.release"),
        "dpi": int(dpi[-1]) if dpi else None,
        "versi_aplikasi": versi.group(1) if versi else None,
    }


def beri_izin_adb(package):
    """Berikan semua izin runtime yang diminta aplikasi + izin khusus (app-ops, baterai) lewat adb, tanpa UI.
    Supaya uji tidak tersangkut di dialog/Pengaturan izin. Dipanggil di awal uji dan setelah 'pm clear'."""
    out = adb_baca("shell", "dumpsys", "package", package)
    disebut = set(re.findall(r"([\w.]+\.permission\.[A-Z0-9_]+)", out))
    belum = list(dict.fromkeys(re.findall(r"([\w.]+\.permission\.[A-Z0-9_]+): granted=false", out)))
    belum.sort(key=lambda x: "BACKGROUND" in x)  # lokasi latar belakang hanya bisa setelah lokasi biasa
    hasil = {"runtime": [], "appops": [], "lain": [], "gagal": []}

    def gagal(teks):
        return any(k in teks for k in ("Exception", "Error", "not a changeable", "Unknown permission", "not requested"))

    for izin in belum:
        teks = adb("shell", "pm", "grant", package, izin)
        (hasil["gagal"] if gagal(teks) else hasil["runtime"]).append(izin.split(".")[-1])
    for izin, op in IZIN_APPOPS.items():
        if izin in disebut:
            teks = adb("shell", "appops", "set", package, op, "allow")
            (hasil["gagal"] if gagal(teks) else hasil["appops"]).append(op)
    if "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS" in disebut:
        adb("shell", "dumpsys", "deviceidle", "whitelist", f"+{package}")
        hasil["lain"].append("tanpa_optimasi_baterai")
    if "android.permission.ACCESS_NOTIFICATION_POLICY" in disebut:
        adb("shell", "cmd", "notification", "allow_dnd", package)
        hasil["lain"].append("akses_jangan_ganggu")
    diberi = hasil["runtime"] + hasil["appops"] + hasil["lain"]
    if diberi:
        log("  izin diberikan lewat adb:", ", ".join(diberi))
    if hasil["gagal"]:
        log("  izin yang gagal diberikan:", ", ".join(hasil["gagal"]),
            "(HP Xiaomi/Redmi/POCO: aktifkan 'USB debugging (Security settings)' di Opsi pengembang)")
    return hasil


def hp_xiaomi(info):
    return (info.get("merek") or "").strip().lower() in MEREK_XIAOMI


def baca_autostart():
    try:
        return json.loads(FILE_AUTOSTART.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def catat_autostart(package, status):
    data = baca_autostart()
    data[package] = {"status": status, "waktu": datetime.now().isoformat(timespec="seconds")}
    try:
        FILE_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        FILE_AUTOSTART.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log("  status autostart tidak bisa disimpan:", e)


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
        return float(adb_baca("shell", "settings", "get", "system", "font_scale").strip())
    except ValueError:
        return 1.0


POLA_ALAMAT_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def jenis_risiko(label):
    """None (aman), "selalu" (selalu diblokir), "ubah_data", atau "data_lama" (hanya pada data buatan uji).

    Alamat email yang tampil sebagai data tidak dihitung sebagai tombol email. Label multi-baris adalah kartu/baris
    daftar yang membuka detail: kata kerja hanya dinilai di baris pertamanya, karena baris berikutnya biasanya status
    ("Menunggu Konfirmasi", "Proses"). Larangan mutlak tetap dicek di seluruh label."""
    asli = POLA_ALAMAT_EMAIL.sub(" ", str(label or ""))
    lb = " ".join(asli.split())
    if not lb:
        return None
    if KATA_SELALU_BLOKIR.search(lb):
        return "selalu"
    kepala = next((" ".join(b.split()) for b in asli.splitlines() if b.strip()), lb)
    if KATA_AMAN.search(kepala):
        return None
    if KATA_UBAH_DATA.search(kepala):
        return "data_lama" if KATA_UBAH_DATA_LAMA.search(kepala) else "ubah_data"
    return None


def label_berisiko(label):
    return jenis_risiko(label) is not None


def tap_mengunci_akun(root, label):
    """Menyimpan form kata sandi baru atau mengaktifkan verifikasi 2 langkah bisa mengunci akun uji."""
    lb = " ".join(str(label or "").split())
    if not KATA_KIRIM_FORM.search(lb) or POLA_2FA.search(lb):
        return False  # bukan tombol kirim, atau justru menu untuk MEMBUKA layar 2FA
    kolom = [el for el in root.iter() if el.get("class") == "android.widget.EditText"]
    sandi = sum(1 for el in kolom if el.get("password") == "true")
    if sandi >= 2 or (sandi == 1 and len(kolom) == 1):  # form ganti kata sandi (form login punya kolom identitas)
        return True
    teks = " ".join(filter(None, (el.get("content-desc") or el.get("text") for el in root.iter())))
    return bool(POLA_2FA.search(teks) and re.search(r"\bqr\b|pindai|scan|masukkan kode|kode (6|enam) digit|"
                                                    r"kunci rahasia|secret", teks, re.I))


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


POLA_TEKS_JANGGAL = re.compile(
    r"\b(null|undefined|NaN|Infinity)\b|Instance of '|\[object Object\]|\{\{.*?\}\}|(?<![\w%])%[sd](?!\w)|"
    r"[Ll]orem ipsum|\bTODO\b|\bFIXME\b|Exception\b|\bError:|is not a subtype of|Null check operator")
POLA_HARI = re.compile(r"(\d[\d.]*)\s*(hari|days?)\b", re.I)
POLA_TAHUN = re.compile(r"\b\d{1,2}[\s/-](?:jan|feb|mar|apr|mei|may|jun|jul|agu|aug|sep|okt|oct|nov|des|dec)\w*[\s/-](\d{4})\b"
                        r"|\b(\d{4})-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/(\d{4})\b", re.I)


def alasan_teks_janggal(teks):
    """Teks yang hampir pasti cacat: nilai kosong/teknis bocor ke tampilan atau angka/tanggal yang mustahil."""
    m = POLA_TEKS_JANGGAL.search(teks)
    if m:
        return f"teks teknis/kosong di tampilan: '{m.group(0)}'"
    for m in POLA_HARI.finditer(teks):
        try:
            if float(m.group(1).replace(".", "")) > 3650:
                return f"durasi tidak masuk akal: '{m.group(0)}'"
        except ValueError:
            pass
    for m in POLA_TAHUN.finditer(teks):
        tahun = int(next(g for g in m.groups() if g))
        if tahun == 1970 or tahun < 1950 or tahun > 2100:
            return f"tanggal tidak masuk akal: '{m.group(0)}'"
    return None


def _scroll_terdekat(el, induk):
    return next((a for a in leluhur(el, induk) if a.get("scrollable") == "true"), None)


def terpotong(el, induk, W, H, zona=(), sumbu="xy"):
    """Elemen kemungkinan terpotong (bukan ukuran aslinya): menempel tepi kontainer gulir terdekat atau tepi layar,
    atau masuk ke bilah sistem. sumbu "y" = hanya periksa atas/bawah (untuk tinggi), "x" = kiri/kanan (untuk lebar)."""
    b = _bounds(el.get("bounds"))
    if not b:
        return False
    sc = _scroll_terdekat(el, induk)
    a = _bounds(sc.get("bounds")) if sc is not None else None
    if "y" in sumbu:
        if b[1] <= 0 or b[3] >= H - 1 or (a and (abs(b[1] - a[1]) <= 2 or abs(b[3] - a[3]) <= 2)):
            return True
        if any(_tumpang(b, z) and (z[1] <= 0 or z[3] >= H - 1) for z in zona):
            return True
    if "x" in sumbu:
        if b[0] <= 0 or b[2] >= W - 1 or (a and (abs(b[0] - a[0]) <= 2 or abs(b[2] - a[2]) <= 2)):
            return True
    return False


def ukuran_layar_xml(root):
    kotak = [_bounds(el.get("bounds")) for el in root.iter() if _bounds(el.get("bounds"))]
    return (max((b[2] for b in kotak), default=1080), max((b[3] for b in kotak), default=2400))


def analisis_xml(xml, density=None, maks_daftar=25, zona=None):
    """Hitung metrik aksesibilitas, kerumitan, dan cacat visual otomatis dari page source Appium.
    Elemen yang terpotong tepi layar/kontainer gulir atau masuk bilah sistem tidak dinilai ukuran sentuhnya."""
    root = _root(xml)
    zona = zona or []
    semua = []  # (elemen, kedalaman)

    def jelajah(el, dalam):
        if el.tag != "hierarchy":
            semua.append((el, dalam))
        for anak in el:
            jelajah(anak, dalam + (0 if el.tag == "hierarchy" else 1))

    jelajah(root, 1)
    _, induk = pohon_induk(root)
    W, H = ukuran_layar_xml(root)
    terlihat = [el for el, _ in semua if el.get("displayed", "true") == "true"]

    def ringkas(el, **lain):
        info = {"kelas": (el.get("class") or "").split(".")[-1], "label": _label(el) or None,
                "hint": el.get("hint") or None, "bounds": el.get("bounds")}
        u = _ukuran_dp(el, density)
        if u:
            info["ukuran_dp"] = f"{u[0]}x{u[1]}"
        info.update(lain)
        return info

    interaktif = [el for el in terlihat
                  if el.get("clickable") == "true" or el.get("long-clickable") == "true"
                  or el.get("class") in KELAS_INTERAKTIF]
    id_interaktif = {id(el) for el in interaktif}
    berlabel_per_kotak = {}
    for el in terlihat:
        if _label(el) and el.get("clickable") == "true":
            berlabel_per_kotak.setdefault(el.get("bounds"), []).append(_label(el))

    tanpa_label, hanya_hint, target_kecil, tombol_mati, duplikat = [], [], [], [], []
    for el in interaktif:
        if not _label(el):
            if el.get("class") == "android.widget.EditText" and el.get("hint"):
                hanya_hint.append(ringkas(el))
            elif el.get("clickable") == "true" and berlabel_per_kotak.get(el.get("bounds")):
                duplikat.append(ringkas(el, kembaran=berlabel_per_kotak[el.get("bounds")][0][:60]))
            else:
                tanpa_label.append(ringkas(el))
        u = _ukuran_dp(el, density)
        if u and el.get("clickable") == "true" and (u[0] < MIN_TARGET_DP or u[1] < MIN_TARGET_DP):
            sumbu = ("y" if u[1] < MIN_TARGET_DP else "") + ("x" if u[0] < MIN_TARGET_DP else "")
            if not terpotong(el, induk, W, H, zona, sumbu):
                target_kecil.append(ringkas(el))
        if (el.get("class") in ("android.widget.Button", "android.widget.ImageButton")
                and el.get("clickable") != "true" and el.get("enabled") == "true"):
            tombol_mati.append(ringkas(el))

    janggal, dilihat = [], set()
    for el in terlihat:
        for teks in {_label(el), (el.get("text") or "").strip()}:
            if teks and teks not in dilihat:
                dilihat.add(teks)
                alasan = alasan_teks_janggal(teks)
                if alasan:
                    janggal.append(ringkas(el, teks=teks[:120], alasan=alasan))

    daun = [el for el in terlihat if _label(el) and _bounds(el.get("bounds")) and not adalah_penghalang(el, W, H)
            and not any(_label(c) for c in el.iter() if c is not el)]
    bertumpuk = []
    for i, a in enumerate(daun):
        ba = _bounds(a.get("bounds"))
        anc_a = {id(x) for x in leluhur(a, induk)}
        for b_el in daun[i + 1:]:
            bb = _bounds(b_el.get("bounds"))
            if ba == bb or not _tumpang(ba, bb) or id(b_el) in anc_a or id(a) in {id(x) for x in leluhur(b_el, induk)}:
                continue
            luas_a, luas_b = (ba[2] - ba[0]) * (ba[3] - ba[1]), (bb[2] - bb[0]) * (bb[3] - bb[1])
            irisan = (min(ba[2], bb[2]) - max(ba[0], bb[0])) * (min(ba[3], bb[3]) - max(ba[1], bb[1]))
            kecil, besar = min(luas_a, luas_b), max(luas_a, luas_b)
            if kecil <= 0 or irisan < 0.25 * kecil or kecil < 0.2 * besar:
                continue  # irisan kecil, atau lencana kecil di atas ikon (pola wajar)
            if _scroll_terdekat(a, induk) is not _scroll_terdekat(b_el, induk):
                continue  # lapisan berbeda (konten bergulir di bawah bilah mengambang): wajar
            bertumpuk.append(ringkas(a, menumpuk=_label(b_el)[:60], bounds_lain=b_el.get("bounds")))
            if len(bertumpuk) >= 10:
                break

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
            "duplikat_klik": duplikat[:maks_daftar],
            "teks_janggal": janggal[:maks_daftar],
            "elemen_bertumpuk": bertumpuk[:maks_daftar],
        },
        "teks_di_layar": teks[:60],
    }


def daftar_elemen(xml, density=None, maks=80, kotak=False, zona=None):
    """Daftar elemen ringkas untuk LLM Perancang: hanya yang terlihat dan bermakna.
    kotak=True menambahkan bounds [x1, y1, x2, y2] (dipakai untuk menggambar nomor di screenshot) beserta tanda
    'terpotong' (menempel tepi layar/kontainer gulir) dan 'tertutup' (sebagian tertutup bilah sistem atau elemen
    lain yang digambar di atasnya); keduanya tidak dipakai untuk menilai kontras."""
    root = _root(xml)
    urut, induk = pohon_induk(root) if kotak else ([], {})
    W, H = ukuran_layar_xml(root) if kotak else (0, 0)
    hasil = []
    for el in root.iter():
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
                            ("checked", "tercentang"), ("checkable", "sakelar")):
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
                if adalah_penghalang(el, W, H):
                    item["penghalang"] = True  # lapisan di belakang dialog/lembar bawah; mengetuknya menutup dialog
                else:
                    if terpotong(el, induk, W, H, zona or []):
                        item["terpotong"] = True
                    _, porsi = titik_terlihat(root, el, zona or [], urut, induk, ambang=0.0)
                    if porsi < 0.9:
                        item["tertutup"] = True
        u = _ukuran_dp(el, density)
        if u:
            item["ukuran_dp"] = f"{u[0]}x{u[1]}"
        hasil.append(item)
        if len(hasil) >= maks:
            break
    return hasil


def sakelar_terluar(node):
    """Elemen checkable terluar di dalam node. Baris HyperOS adalah Switch yang berisi Switch lagi (tombol geser);
    keduanya dihitung satu sakelar."""
    hasil = []

    def jalan(el):
        if el.get("checkable") == "true" and _bounds(el.get("bounds")):
            hasil.append(el)
            return
        for anak in el:
            jalan(anak)
    jalan(node)
    return hasil


def tombol_sakelar(s):
    """Elemen yang diketuk untuk sakelar s: tombol geser di dalamnya kalau ada satu, selain itu s sendiri."""
    dalam = [x for x in s.iter() if x is not s and x.get("checkable") == "true" and _bounds(x.get("bounds"))]
    return dalam[0] if len(dalam) == 1 else s


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


# ====================================================================== logcat
POLA_LOGCAT = re.compile(r"^\s*(\d+\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]*?)\s*:\s?(.*)$")
POLA_LOG_GALAT = re.compile(r"Exception|Error|error|failed|gagal|status ?code[:= ]*[45]\d\d|statusCode[:= ]*[45]\d\d|"
                            r"Null check|RangeError|NoSuchMethod|setState\(\) called after dispose|overflowed|timeout", re.I)


def sensor_log(teks):
    """Samarkan token, email, dan kunci panjang di baris log sebelum disimpan/dikirim ke LLM."""
    teks = re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "[token]", teks)
    teks = re.sub(r"(?i)(bearer|token|authorization|api[_-]?key|password|sandi)([\"':= ]+)[^\s\"',}]+", r"\1\2[disensor]", teks)
    teks = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", teks)
    return re.sub(r"\b[A-Za-z0-9+/_-]{32,}\b", "[kunci]", teks)


# ====================================================================== geometri layar: bagian elemen yang terlihat
POLA_LAUNCHER = re.compile(r"launcher|miui\.home|\.home$|trebuchet|lawnchair|quickstep", re.I)
POLA_FOKUS = re.compile(r'\s(focused|a11y-focused)="(true|false)"')


def _norm_xml(xml):
    """Page source tanpa atribut fokus (berubah sendiri) untuk membandingkan keadaan layar."""
    return POLA_FOKUS.sub("", xml or "")


def paket_xml(root):
    """Paket pemilik layar: paket yang paling banyak dipakai node berukuran."""
    hitung = {}
    for el in root.iter():
        p = el.get("package")
        if p and _bounds(el.get("bounds")):
            hitung[p] = hitung.get(p, 0) + 1
    return max(hitung, key=hitung.get) if hitung else None


def pohon_induk(root):
    """(node dalam urutan dokumen, peta anak -> induk)."""
    induk = {c: p for p in root.iter() for c in p}
    return [el for el in root.iter() if el.tag != "hierarchy"], induk


def leluhur(el, induk):
    hasil = []
    while el in induk:
        el = induk[el]
        hasil.append(el)
    return hasil


def _tumpang(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def cari_node(root, target, urut=None, utamakan_klik=True):
    """Node XML yang cocok dengan target rencana, dengan urutan pencarian yang sama seperti kandidat_locator
    (id, label persis pada content-desc lalu text, label_mengandung, hint, kelas saja). Tanpa "urutan", kecocokan
    yang bisa diketuk (atau berada di dalam elemen yang bisa diketuk) didahulukan: judul halaman "Profil" tidak
    dipilih kalau ada tab "Profil"."""
    if urut is None:
        urut = [el for el in root.iter() if el.tag != "hierarchy"]
    urut = [el for el in urut if _bounds(el.get("bounds"))]
    kls = _kelas_penuh(target["kelas"]) if target.get("kelas") else None
    ke = int(target.get("urutan") or 0)
    induk = {c: p for p in root.iter() for c in p} if utamakan_klik and target.get("urutan") is None else None

    def bisa_klik(el):
        return el.get("clickable") == "true" or any(a.get("clickable") == "true" for a in leluhur(el, induk))

    def pilih(uji, pakai_kelas=True):
        h = [el for el in urut if (not (pakai_kelas and kls) or el.get("class") == kls) and uji(el)]
        if induk is not None and h:
            return next((el for el in h if bisa_klik(el)), h[0])
        return h[ke] if len(h) > ke else None

    langkah = []
    if target.get("id"):
        langkah.append((lambda el: el.get("resource-id") == target["id"], False))
    if target.get("label"):
        v = str(target["label"])
        langkah += [(lambda el: el.get("content-desc") == v, True), (lambda el: el.get("text") == v, True)]
    if target.get("label_mengandung"):
        v2 = str(target["label_mengandung"])
        langkah += [(lambda el: v2 in (el.get("content-desc") or ""), True), (lambda el: v2 in (el.get("text") or ""), True)]
    if target.get("hint"):
        langkah.append((lambda el: el.get("hint") == str(target["hint"]), False))
    if not langkah and kls:
        langkah.append((lambda el: True, True))
    for uji, pakai_kelas in langkah:
        n = pilih(uji, pakai_kelas)
        if n is not None:
            return n
    return None


def adalah_penghalang(el, W, H):
    """Lapisan penutup di belakang dialog/lembar bawah (ModalBarrier Flutter, berlabel 'Tutup'/'Dismiss'): bisa
    diklik, selebar layar, tanpa isi. Di urutan dokumen ia bisa muncul SESUDAH isi dialog padahal digambar di
    bawahnya, jadi tidak boleh dianggap menutupi elemen lain."""
    if el.get("clickable") != "true" or W <= 0 or H <= 0:
        return False
    b = _bounds(el.get("bounds"))
    luas = (b[2] - b[0]) * (b[3] - b[1]) if b else 0
    if luas < (0.5 if el.get("dismissable") == "true" else 0.95) * W * H:
        return False
    return not any(c is not el and (c.get("clickable") == "true" or _label(c)) for c in el.iter())


def ada_lapisan_modal(xml):
    """True kalau layar sedang menampilkan dialog/lembar bawah modal (ada lapisan penutup)."""
    try:
        root = _root(xml) if xml else None
    except ET.ParseError:
        return False
    if root is None:
        return False
    W, H = ukuran_layar_xml(root)
    return any(adalah_penghalang(el, W, H) for el in root.iter())


def titik_terlihat(root, node, zona=(), urut=None, induk=None, ambang=0.3, abaikan_dalam=None):
    """Titik ketuk pada bagian elemen yang benar-benar terlihat: di luar bilah sistem/papan ketik (zona) dan tidak
    tertutup elemen yang digambar di atasnya (elemen sesudahnya dalam urutan dokumen yang bisa diklik atau berlabel,
    misalnya bilah navigasi mengambang atau lembar bawah; lapisan penutup dialog tidak dihitung). Untuk lapisan
    penutup itu sendiri, semua isi lain dianggap di atasnya. abaikan_dalam: penutup di dalam node ini tidak dihitung.
    Kembalikan ((x, y), porsi_terlihat); titik None kalau yang terlihat < ambang."""
    b = _bounds(node.get("bounds"))
    if not b or b[2] - b[0] < 2 or b[3] - b[1] < 2:
        return None, 0.0
    if urut is None or induk is None:
        urut, induk = pohon_induk(root)
    lewati = {id(x) for x in node.iter()} | {id(x) for x in leluhur(node, induk)}
    if abaikan_dalam is not None:
        lewati |= {id(x) for x in abaikan_dalam.iter()}
    W, H = ukuran_layar_xml(root)
    penutup = []
    if adalah_penghalang(node, W, H):
        for el in urut:  # digambar paling bawah: bagian yang tertutup isi dialog/lembar bawah tidak bisa diketuk
            bb = _bounds(el.get("bounds"))
            if (id(el) not in lewati and bb and _tumpang(bb, b)
                    and (bb[2] - bb[0]) * (bb[3] - bb[1]) < 0.95 * W * H):
                penutup.append(bb)
        ambang = min(ambang, 0.04)
    else:
        try:
            mulai = urut.index(node)
        except ValueError:
            mulai = len(urut)
        for el in urut[mulai + 1:]:
            if id(el) in lewati or not (el.get("clickable") == "true" or _label(el)):
                continue  # kontainer gulir sendiri tidak dihitung; isinya (anak berlabel/bisa diklik) yang menutupi
            bb = _bounds(el.get("bounds"))
            if bb and _tumpang(bb, b) and not adalah_penghalang(el, W, H):
                penutup.append(bb)
    w, h = b[2] - b[0], b[3] - b[1]
    sampel = [(b[0] + w * fx, b[1] + h * fy) for fy in (0.5, 0.3, 0.7, 0.12, 0.88) for fx in (0.5, 0.3, 0.7, 0.12, 0.88)]
    batas = [[z[0] - 8, z[1] - 8, z[2] + 8, z[3] + 8] for z in zona]
    terlihat = [(x, y) for x, y in sampel
                if not any(z[0] <= x < z[2] and z[1] <= y < z[3] for z in batas)
                and not any(p[0] <= x < p[2] and p[1] <= y < p[3] for p in penutup)]
    porsi = len(terlihat) / len(sampel)
    if not terlihat or porsi < ambang:
        return None, porsi
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    x, y = min(terlihat, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
    return (int(x), int(y)), porsi


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
    pulih = r.get("pemulihan") or []
    if not isinstance(pulih, list):
        salah.append('"pemulihan" harus berupa daftar langkah')
        pulih = []
    akun = apps.get(app, {}).get("akun", {})
    sandi = {cfg.get("password_default")} | {a.get("password") for a in akun.values()}
    sandi.discard(None)
    keadaan = {"login": bool(r.get("peran")), "ubah": False}

    def cek(nomor, s):
        if not isinstance(s, dict):
            salah.append(f"{nomor}: harus berupa objek")
            return
        aksi = s.get("aksi")
        if aksi not in AKSI:
            salah.append(f"{nomor}: aksi '{aksi}' tidak dikenal (pilihan: {', '.join(AKSI)})")
            return
        try:
            if aksi == "tap":
                t = s.get("target")
                if isinstance(t, dict) and "koordinat" in t:
                    k = t["koordinat"]
                    if not (isinstance(k, list) and len(k) == 2 and all(isinstance(x, (int, float)) for x in k)):
                        raise ValueError("koordinat harus [x, y]")
                else:
                    kandidat_locator(t)
            if aksi in ("ketik", "pastikan_ada", "pastikan_tidak_ada"):
                kandidat_locator(s.get("target"))
            for k in ("tunggu_muncul", "tunggu_hilang"):
                if k in s:
                    kandidat_locator(s[k])
        except ValueError as e:
            salah.append(f"{nomor}: {e}")
        ubah = s.get("ubah_data")
        if ubah is not None and ubah not in JENIS_UBAH_DATA:
            salah.append(f"{nomor}: 'ubah_data' harus salah satu dari: {', '.join(JENIS_UBAH_DATA)}")
        if ubah in ("buat", "ubah"):
            keadaan["ubah"] = True
        if ubah and ubah != "biarkan" and not izinkan_berisiko:
            salah.append(f"{nomor}: perubahan data tidak diizinkan di sesi ini (boleh_ubah_data=false)")
        if aksi == "tap" and isinstance(s.get("target"), dict):
            lb = " ".join(str(s["target"].get(k) or "") for k in ("label", "label_mengandung")).strip()
            jenis = jenis_risiko(lb)
            if jenis == "selalu":
                salah.append(f"{nomor}: tap '{lb}' selalu diblokir (pembayaran, pesan/telepon ke orang lain, atau hapus "
                             "akun tidak bisa dipulihkan)")
            elif jenis and not izinkan_berisiko:
                salah.append(f"{nomor}: tap '{lb}' diblokir karena bisa mengubah data/keluar dari aplikasi; uji tanpa "
                             "menekan tombol itu")
            elif jenis and not ubah and not KATA_KELUAR_AKUN.search(lb):
                salah.append(f"{nomor}: tap '{lb}' mengubah data; tambahkan \"ubah_data\": \"buat\"|\"ubah\"|\"hapus\"|"
                             "\"status\" (dan langkah \"pemulihan\" untuk mengembalikannya)")
        if aksi == "login":
            if s.get("peran") not in akun:
                salah.append(f"{nomor}: peran '{s.get('peran')}' tidak ada untuk aplikasi {app} "
                             f"(pilihan: {', '.join(akun)})")
            keadaan["login"] = True
        if aksi == "ketik":
            if "teks" not in s and s.get("dari_akun") not in ("email", "hp", "kode", "password"):
                salah.append(f"{nomor}: 'ketik' butuh 'teks' atau 'dari_akun' (email/hp/kode/password)")
            if "dari_akun" in s and not keadaan["login"]:
                salah.append(f"{nomor}: 'dari_akun' butuh langkah login sebelumnya atau 'peran' di rencana")
            if s.get("teks") in sandi:
                salah.append(f"{nomor}: jangan tulis kata sandi di rencana; pakai \"dari_akun\": \"password\"")
        if aksi == "gulir" and s.get("arah", "bawah") not in ARAH:
            salah.append(f"{nomor}: arah gulir harus salah satu dari: {', '.join(ARAH)}")
        if aksi == "atur_skala_font":
            try:
                if not 0.85 <= float(s.get("skala", 1.0)) <= 2.0:
                    raise ValueError
            except (TypeError, ValueError):
                salah.append(f"{nomor}: 'skala' harus angka 0.85-2.0")
        if aksi == "atur_tema" and s.get("tema", "gelap") not in ("gelap", "terang"):
            salah.append(f"{nomor}: 'tema' harus gelap atau terang")
        if aksi == "atur_jaringan" and s.get("status", "mati") not in ("mati", "hidup"):
            salah.append(f"{nomor}: 'status' jaringan harus mati atau hidup")
        if aksi == "tunggu":
            try:
                if not 0 < float(s.get("detik", 1)) <= 30:
                    raise ValueError
            except (TypeError, ValueError):
                salah.append(f"{nomor}: 'detik' harus angka 0-30")

    for i, s in enumerate(langkah, 1):
        cek(f"langkah {i}", s)
    for i, s in enumerate(pulih, 1):
        cek(f"pemulihan {i}", s)
    if keadaan["ubah"] and not pulih:
        salah.append('rencana membuat/mengubah data ("ubah_data": "buat"/"ubah") tetapi tidak punya "pemulihan": '
                     "tambahkan langkah untuk menghapus data buatan uji ({penanda}) atau mengembalikan isian lama "
                     "({nilai_awal:nama_langkah}), diakhiri pastikan_tidak_ada")
    if len(pulih) > 25:
        salah.append(f"maksimal 25 langkah pemulihan (ada {len(pulih)})")
    return salah


# ====================================================================== pelaksana
class Pelaksana:
    def __init__(self, driver, cfg, kode_app, folder, info, izinkan_berisiko=False):
        self.d = driver
        self.cfg = cfg
        self.kode_app = kode_app
        self.app = cfg["aplikasi"][kode_app]
        self.pkg = self.app["package"]
        self.folder = folder
        self.density = info["dpi"] / 160 if info.get("dpi") else None
        self.info = info
        self.izinkan_berisiko = izinkan_berisiko
        self.langkah = []
        self.peran_aktif = None
        self.xml_terakhir = None
        self.t_acuan = time.perf_counter()  # titik nol waktu (= awal rekaman layar, kalau direkam)
        self.aksi_sekarang = None
        self.rencana_ke = None
        self.skala_font_awal = None
        self.sso = self.app.get("login_metode") == "google_sso"
        self.di_gerbang = False
        self.berikutnya = None  # langkah rencana sesudah langkah yang sedang berjalan
        self.beri_izin = self.app.get("beri_izin_otomatis", True)
        self.izin_otomatis = []  # catatan pemberian izin lewat adb selama uji
        self._zona = None    # bilah sistem (cache)
        self._ukuran = None  # ukuran layar (cache)
        self.fase = "utama"  # "pemulihan" saat menjalankan langkah pemulihan data
        self.penanda = None  # penanda data uji run ini, mis. UJI-1112-153012
        self.penanda_aktif = set()  # penanda data uji sesi ini (boleh dihapus/diubah statusnya)
        self.nilai_awal = {}  # isi kolom sebelum diketik ulang (untuk {nilai_awal:nama_langkah})
        self.ubah_data = []   # catatan perubahan data yang dilakukan uji
        self.tema_awal = None
        self.pesawat_awal = None
        self.jaringan_svc = False
        self.penanda_terakhir = None  # penanda data uji di baris/halaman tombol hapus/ubah status terakhir
        self.log_sejak = None  # epoch HP terakhir yang sudah dibaca dari logcat
        self.pid_app = None
        self.saring_screenshot = None  # fungsi(xml) -> bool; None = selalu ambil screenshot

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

    def tunggu_stabil(self, t0=None, sebelum=None, batas=8.0, jeda=0.3):
        """Tunggu struktur layar berhenti berubah. Kembalikan (mulai_berubah, stabil) dalam detik sejak t0:
        mulai_berubah = polling pertama yang berbeda dari 'sebelum' (None kalau tidak pernah berubah);
        stabil = saat keadaan AKHIR pertama kali terlihat (bukan saat stabilnya dikonfirmasi), None kalau tidak stabil."""
        t0 = time.perf_counter() if t0 is None else t0
        mulai, lalu, t_lalu, berubah = time.perf_counter(), None, None, None
        while time.perf_counter() - mulai < batas:
            t_panggil = time.perf_counter()
            kini = _norm_xml(self.d.page_source)
            if sebelum is not None and berubah is None and kini != sebelum:
                berubah = t_panggil - t0
            if kini == lalu:
                return berubah, t_lalu - t0
            lalu, t_lalu = kini, t_panggil
            time.sleep(jeda)
        return berubah, None

    # ---------- geometri: ketuk hanya bagian elemen yang terlihat
    def ukuran_layar(self):
        if self._ukuran is None:
            try:
                u = self.d.get_window_size()
                self._ukuran = (int(u["width"]), int(u["height"]))
            except Exception:
                self._ukuran = (1080, 2400)
        return self._ukuran

    def zona_sistem(self):
        """Persegi bilah status, bilah navigasi HP, dan (kalau tampil) perkiraan papan ketik: jangan diketuk."""
        W, H = self.ukuran_layar()
        if self._zona is None:
            zona = []
            try:
                bars = self.d.execute_script("mobile: getSystemBars", {}) or {}
            except Exception:
                bars = {}
            for v in (bars.values() if isinstance(bars, dict) else []):
                if isinstance(v, dict) and v.get("visible", True) and v.get("width") and v.get("height"):
                    x, y = int(v.get("x") or 0), int(v.get("y") or 0)
                    zona.append([x, y, x + int(v["width"]), y + int(v["height"])])
            if not zona:  # perkiraan: bilah status 24 dp, navigasi 3 tombol 48 dp (gestur: 16 dp)
                d = self.density or 3.0
                mode = adb_baca("shell", "settings", "get", "secure", "navigation_mode").strip()
                zona = [[0, 0, W, int(24 * d)], [0, H - int((16 if mode == "2" else 48) * d), W, H]]
            self._zona = zona
        zona = list(self._zona)
        try:
            if self.d.is_keyboard_shown():
                zona.append([0, int(H * 0.58), W, H])
        except Exception:
            pass
        return zona

    def gulir_ke(self, node, root, induk, arah=None, porsi=0.45):
        """Gulir sedikit supaya node masuk ke area yang terlihat (pakai kontainer gulir terdekat kalau ada)."""
        W, H = self.ukuran_layar()
        zona = self.zona_sistem()
        bawah = min([z[1] for z in zona if z[1] > H / 2] or [H])
        atas = max([z[3] for z in zona if z[3] < H / 2] or [0])
        b = _bounds(node.get("bounds")) if node is not None else None
        if arah is None:
            arah = "bawah" if b and (b[1] + b[3]) / 2 > (atas + bawah) / 2 else "atas"
        area = None
        for el in ([] if node is None else leluhur(node, induk)):
            bb = _bounds(el.get("bounds"))
            if el.get("scrollable") == "true" and bb:
                area = [bb[0], max(bb[1], atas), bb[2], min(bb[3], bawah)]
                break
        if not area or area[3] - area[1] < 100:
            area = [int(W * 0.1), atas + int((bawah - atas) * 0.2), int(W * 0.9), atas + int((bawah - atas) * 0.8)]
        try:
            self.d.execute_script("mobile: scrollGesture", {"left": area[0], "top": area[1], "width": area[2] - area[0],
                                                            "height": area[3] - area[1], "direction": ARAH[arah],
                                                            "percent": porsi})
        except Exception as e:  # noqa: BLE001
            log("  gagal menggulir:", str(e)[:120])
        time.sleep(0.5)
        return arah

    def siapkan_ketuk(self, target, batas_cari=10.0, maks_gulir=3):
        """Cari elemen target di page source, gulir kalau tertutup/terpotong, lalu kembalikan titik ketuk yang aman.
        Hanya menggulir kontainer gulir milik elemen itu, dan hanya kalau penutupnya bilah sistem atau elemen di luar
        kontainer itu (menggulir tidak membantu kalau penutupnya ikut tergulir). Kalau tetap tertutup, posisi gulir
        dikembalikan supaya layar tidak berubah.
        Hasil: {"titik", "node", "xml", "digulir", "porsi"} atau {"alasan": "tidak_ditemukan"|"tertutup", ...}."""
        akhir = time.perf_counter() + batas_cari
        digulir, xml, node, keyboard_ditutup, arah_gulir = 0, None, None, False, []
        while True:
            try:
                xml = self.d.page_source
                root = _root(xml)
            except Exception:
                xml, root = None, None
            if root is not None:
                urut, induk = pohon_induk(root)
                node = cari_node(root, target, urut)
                if node is not None:
                    zona = self.zona_sistem()
                    titik, porsi = titik_terlihat(root, node, zona, urut, induk)
                    if titik:
                        return {"titik": titik, "node": node, "xml": xml, "digulir": digulir, "porsi": porsi}
                    if not keyboard_ditutup and self.keyboard_tampil():
                        keyboard_ditutup = True  # papan ketik menutupi target: tutup dulu sebelum menggulir
                        self.tutup_keyboard()
                        time.sleep(0.4)
                        continue
                    sc = _scroll_terdekat(node, induk)
                    membantu = sc is not None and titik_terlihat(root, node, zona, urut, induk,
                                                                 abaikan_dalam=sc)[0] is None
                    if digulir < maks_gulir and membantu:
                        arah_gulir.append(self.gulir_ke(node, root, induk))
                        digulir += 1
                        continue
                    for a in reversed(arah_gulir):  # tetap tertutup: kembalikan posisi gulir semula
                        self.gulir_ke(node, root, induk, arah="atas" if a == "bawah" else "bawah")
                    return {"alasan": "tertutup", "node": node, "xml": xml, "digulir": digulir, "porsi": porsi}
            if time.perf_counter() >= akhir:
                return {"alasan": "tidak_ditemukan", "xml": xml, "digulir": digulir}
            time.sleep(0.3)

    def pernah_terlihat(self, target, maks=10):
        """True kalau target ada di salah satu tangkapan layar terakhir run ini (kemungkinan sekarang tertutup)."""
        for lk in reversed(self.langkah[-maks:]):
            f = self.folder / lk["xml"] if lk.get("xml") else None
            try:
                if f and f.exists() and cari_node(_root(f.read_text(encoding="utf-8")), target) is not None:
                    return True
            except ET.ParseError:
                continue
        return False

    def tunggu_app_siap(self, batas=30):
        return self.tunggu(lambda: self.paket_aktif() == self.pkg
                           and self.jumlah(f'packageName("{self.pkg}").clickable(true)') > 0, batas, jeda=0.4)

    def kembali_ke_app(self):
        """Aplikasi tidak di depan (layar utama HP / aplikasi lain): Back sekali-dua kali, kalau perlu buka lagi."""
        for _ in range(2):
            paket = self.paket_aktif()
            if paket == self.pkg or (paket and POLA_LAUNCHER.search(paket)):
                break
            self.d.back()
            time.sleep(0.8)
        if self.paket_aktif() != self.pkg:
            adb("shell", "am", "start", "-n", f'{self.pkg}/{self.app["activity"]}')
            self.tunggu_app_siap()
            self.tutup_dialog_izin()
            self.lewati_gerbang()
        return self.paket_aktif() == self.pkg

    def coba_pulihkan_target(self, target, info, batas_cari):
        """Target tidak bisa diketuk: coba kembalikan aplikasi ke depan, tunggu penutup sementara (snackbar/animasi),
        tutup lapisan (Back) kalau ada dialog/lembar bawah atau target tadi terlihat, atau cari dengan menggulir.
        Kembalikan (info_baru, catatan_pemulihan)."""
        catatan = []
        paket = self.paket_aktif()
        if paket and paket != self.pkg and not POLA_PENGATURAN.search(paket):
            catatan.append(f"aplikasi tidak di depan ({paket}); dibawa kembali")
            self.kembali_ke_app()
            info = self.siapkan_ketuk(target, min(batas_cari, 5))
            if info.get("titik"):
                return info, catatan
        if info.get("alasan") == "tertutup":
            catatan.append("target tertutup elemen lain; tunggu sebentar (snackbar/animasi) lalu coba lagi")
            time.sleep(2.5)
            info2 = self.siapkan_ketuk(target, 2)
            if info2.get("titik"):
                return info2, catatan
            info = info2 if info2.get("alasan") else info
        lapisan = info.get("alasan") == "tertutup" and ada_lapisan_modal(info.get("xml"))
        if lapisan or (info.get("alasan") == "tidak_ditemukan" and self.pernah_terlihat(target)):
            catatan.append("target tertutup lapisan lain (lembar bawah/dialog); tekan Back sekali")
            self.d.back()
            time.sleep(0.8)
            if self.paket_aktif() != self.pkg:
                catatan.append("Back menutup aplikasi; dibuka lagi")
                self.kembali_ke_app()
            info = self.siapkan_ketuk(target, 3)
            if info.get("titik"):
                return info, catatan
        elif info.get("alasan") == "tidak_ditemukan" and info.get("xml"):
            catatan.append("dicari dengan menggulir")
            try:
                root = _root(info["xml"])
                _, induk = pohon_induk(root)
            except ET.ParseError:
                root, induk = None, {}
            for _ in range(2):
                self.gulir_ke(None, root, induk, arah="bawah", porsi=0.7)
                info2 = self.siapkan_ketuk(target, 0.5)
                if info2.get("titik"):
                    return info2, catatan
            for _ in range(2):
                self.gulir_ke(None, root, induk, arah="atas", porsi=0.7)
        return info, catatan

    def paket_aktif(self):
        try:
            return self.d.current_package
        except Exception:
            return None

    def keyboard_tampil(self):
        try:
            return bool(self.d.is_keyboard_shown())
        except Exception:
            return False

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

    def izin_ulang(self, alasan):
        if self.beri_izin:
            self.izin_otomatis.append({"saat": alasan, **beri_izin_adb(self.pkg)})

    def di_luar_app(self):
        """True kalau yang tampil adalah layar sistem (Pengaturan/dialog izin), bukan aplikasi yang diuji."""
        paket = self.paket_aktif()
        return bool(paket) and paket != self.pkg and POLA_PENGATURAN.search(paket) is not None

    def tangani_pengaturan(self):
        """Di halaman Pengaturan yang dibuka aplikasi: nyalakan sakelar/opsi izin. Kembalikan labelnya atau None."""
        root = _root(self.d.page_source)
        induk = {anak: p for p in root.iter() for anak in p}

        def teks(el):
            return (el.get("text") or el.get("content-desc") or "").strip()

        def teks_baris(el):  # leluhur terkecil yang punya teks dan hanya satu elemen centang
            node = el
            for _ in range(4):
                isi = [t for t in (teks(x) for x in node.iter()) if t]
                n_cek = len(sakelar_terluar(node))
                if isi and n_cek <= 1:
                    return " ".join(isi)[:200]
                if n_cek > 1 and node is not el:
                    break
                node = induk.get(node)
                if node is None:
                    break
            return teks(el)

        def baris_berisi(el, label):  # baris yang punya teks persis sama dengan label (bukan sekadar memuat)
            node = el
            for _ in range(4):
                node = induk.get(node)
                if node is None or len(sakelar_terluar(node)) > 1:
                    return False
                if any(teks(x) == label for x in node.iter()):
                    return True
            return False

        kandidat = [el for el in root.iter() if el.get("checkable") == "true" and el.get("checked") == "false"
                    and el.get("enabled") != "false" and _bounds(el.get("bounds"))]
        radio = [el for el in kandidat if "Radio" in (el.get("class") or "")]
        pilih = None
        for pola in (r"sepanjang waktu|all the time", r"tanpa batasan|no restrictions|unrestricted", r"izinkan|allow"):
            pilih = next((el for el in radio if re.search(pola, teks_baris(el), re.I)
                          and not POLA_TOLAK.search(teks_baris(el))), None)
            if pilih is not None:
                break
        if pilih is None:
            sakelar = [el for el in kandidat if el not in radio]
            label_app = self.app.get("label_hp") or self.app.get("nama")
            milik_app = [el for el in sakelar if label_app and baris_berisi(el, label_app)]  # mis. daftar Autostart
            cocok = [el for el in sakelar if POLA_IZINKAN.search(teks_baris(el)) and not POLA_TOLAK.search(teks_baris(el))]
            pilih = (milik_app + cocok)[0] if (milik_app or cocok) else (sakelar[0] if len(sakelar) == 1 else None)
        if pilih is None:
            return None
        label = teks_baris(pilih)[:120] or (pilih.get("class") or "sakelar")
        b = _bounds(pilih.get("bounds"))
        log(f"  menyalakan izin di Pengaturan: {label}")
        self.d.tap([((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)])
        time.sleep(1.0)
        for lb in LABEL_IZIN + ("OK", "Ya", "Yes"):  # dialog konfirmasi setelah sakelar dinyalakan
            try:
                el = self.d.find_elements(UIA, f'new UiSelector().text("{_esc(lb)}").clickable(true)')
            except Exception:
                el = []
            if el and (el[0].get_attribute("package") or "") != self.pkg:
                el[0].click()
                time.sleep(0.8)
                break
        return label

    def cari_sakelar_baris(self, cocok):
        """Sakelar (elemen checkable) pada baris yang teksnya cocok, di layar saat ini. None kalau tidak ada."""
        root = _root(self.d.page_source)
        induk = {anak: p for p in root.iter() for anak in p}
        for el in root.iter():
            t = (el.get("text") or el.get("content-desc") or "").strip()
            if not t or not cocok(t) or "EditText" in (el.get("class") or ""):
                continue
            node = el
            for _ in range(5):
                cek = sakelar_terluar(node)
                if len(cek) == 1:
                    return tombol_sakelar(cek[0])
                if len(cek) > 1:
                    break
                node = induk.get(node)
                if node is None:
                    break
        return None

    def nyalakan_sakelar_baris(self, cocok, maks_gulir=4):
        """Cari baris (gulir kalau perlu) lalu nyalakan sakelarnya. 'sudah' / 'dinyalakan' / None."""
        for _ in range(maks_gulir + 1):
            sakelar = self.cari_sakelar_baris(cocok)
            if sakelar is not None:
                if sakelar.get("checked") == "true":
                    return "sudah"
                b = _bounds(sakelar.get("bounds"))
                self.d.tap([((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)])
                time.sleep(1.0)
                lagi = self.cari_sakelar_baris(cocok)
                return "dinyalakan" if lagi is not None and lagi.get("checked") == "true" else None
            try:
                self.gulir("bawah", 1)
            except Exception:
                return None
            time.sleep(0.6)
        return None

    def cari_di_daftar(self, label):
        """Di halaman daftar (mis. Autostart): ketuk ikon Cari lalu ketik label, supaya tidak perlu menggulir."""
        pemilih = ('new UiSelector().descriptionMatches("(?i)(cari|search|telusuri).*")',
                   'new UiSelector().resourceIdMatches("(?i).*search.*")',
                   'new UiSelector().textMatches("(?i)(cari|search|telusuri).*")')
        for sel in pemilih:
            try:
                el = self.d.find_elements(UIA, sel)
            except Exception:
                el = []
            if el:
                el[0].click()
                break
        else:
            return False
        if not self.tunggu(lambda: self.jumlah('className("android.widget.EditText")') > 0, 4):
            return False
        kolom = self.d.find_elements(UIA, 'new UiSelector().className("android.widget.EditText")')[0]
        try:
            kolom.click()
            kolom.clear()
        except Exception:
            pass
        kolom.send_keys(label)
        time.sleep(1.0)
        # Keyboard sengaja TIDAK ditutup: di MIUI/HyperOS menutup keyboard (Back) ikut keluar dari mode Cari
        # sehingga hasil pencarian hilang. Baris hasil Cari tetap terlihat di atas keyboard.
        return True

    def aktifkan_autostart(self, paksa=False):
        """HP Xiaomi/Redmi/POCO: nyalakan 'Mulai otomatis' (Autostart) aplikasi lewat tampilan Pengaturan.
        None = tidak berlaku; 'tersimpan'/'sudah'/'dinyalakan' = aktif; 'gagal' = perlu manual.
        Status apa pun (termasuk gagal) disimpan di autostart.json supaya tidak dicoba ulang setiap uji."""
        if not hp_xiaomi(self.info) or not self.app.get("autostart", True):
            return None
        tersimpan = (baca_autostart().get(self.pkg) or {}).get("status")
        if tersimpan and not paksa:
            return "tersimpan" if tersimpan != "gagal" else "gagal"
        label = self.app.get("label_hp") or self.app.get("nama")
        hasil = None
        slug = _slug(self.pkg)
        try:  # hasil Cari di MIUI/HyperOS bisa berada di jendela lain; baca semua jendela selama pemeriksaan ini
            self.d.update_settings({"enableMultiWindows": True})
        except Exception:
            pass
        komponen_hp = (self.cfg.get("hp") or {}).get("komponen_autostart")
        if label:  # jalur 1: daftar Autostart di aplikasi Keamanan + fitur Cari
            for komponen in ([komponen_hp] if komponen_hp else []) + list(KOMPONEN_AUTOSTART):
                out = adb("shell", "am", "start", "-n", komponen)
                if re.search(r"Error|Exception|does not exist|Permission Denial", out):
                    log(f"  daftar Autostart {komponen} tidak bisa dibuka: {out.strip().splitlines()[-1][:160] if out.strip() else '-'}")
                    continue
                log(f"  memeriksa Autostart '{label}' di daftar Autostart ({komponen}) pakai Cari ...")
                if not self.tunggu(lambda: (self.paket_aktif() or "") != self.pkg and self.jumlah(
                        'checkable(true)') > 0, 6):
                    self.ambil(f"autostart_daftar_{slug}")
                    log("  halaman daftar Autostart tidak terbaca (tidak ada sakelar)")
                    continue
                if self.cari_di_daftar(label):
                    cocok = None
                    for c in (lambda t: t == label, lambda t: t.startswith(label)):
                        if self.tunggu(lambda: self.cari_sakelar_baris(c) is not None, 4):
                            cocok = c
                            break
                    if cocok is not None:
                        hasil = self.nyalakan_sakelar_baris(cocok, maks_gulir=0)
                    else:
                        log(f"  baris '{label}' terlihat? sakelarnya tidak terbaca di hasil Cari "
                            "(screenshot + XML disimpan di folder persiapan)")
                else:  # tidak ada fitur Cari: gulir
                    log("  ikon Cari tidak ditemukan; mencari dengan menggulir")
                    hasil = self.nyalakan_sakelar_baris(lambda t: t == label, maks_gulir=15)
                if hasil:
                    break
                self.ambil(f"autostart_daftar_{slug}")
        if hasil is None:  # jalur 2: halaman Info aplikasi
            log(f"  memeriksa Autostart {self.pkg} di halaman Info aplikasi ...")
            adb("shell", "am", "start", "-a", "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{self.pkg}")
            time.sleep(2.0)
            hasil = self.nyalakan_sakelar_baris(lambda t: len(t) <= 40 and POLA_AUTOSTART.search(t) is not None,
                                                maks_gulir=2)
            if hasil is None:
                self.ambil(f"autostart_info_{slug}")  # bukti untuk diperiksa (screenshot + XML)
        try:
            self.d.update_settings({"enableMultiWindows": False})
        except Exception:
            pass
        adb("shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.5)
        catat_autostart(self.pkg, hasil or "gagal")
        if hasil:
            log(f"  Autostart {self.pkg}: {hasil}")
            return hasil
        log(f"  Autostart {self.pkg} belum bisa dinyalakan/diperiksa otomatis (tidak dicoba lagi sampai "
            f"'python agen_uji.py siapkan --paksa'). Nyalakan manual lewat Pengaturan > Aplikasi > Izin > Autostart, "
            f"atau cek 'label_hp' di aplikasi.json. Screenshot halaman terakhir: {self.folder}")
        return "gagal"

    def lewati_gerbang(self):
        """Halaman izin di DALAM aplikasi (misalnya konfirmasi Autostart): jalankan langkah gerbang_izin."""
        g = self.app.get("gerbang_izin")
        if not g or self.di_gerbang or not g.get("tanda"):
            return True
        if not self.tunggu(lambda: self.ada(g["tanda"]), float(g.get("tunggu_detik", 2))):
            return True
        log("  halaman izin aplikasi terdeteksi; menjalankan langkah gerbang_izin dari aplikasi.json")
        self.di_gerbang = True
        try:
            for n, langkah in enumerate(g.get("langkah", []), 1):
                target = langkah.get("target")
                if target and self.tunggu_elemen(target, float(langkah.get("batas_cari_detik", 4))) is None:
                    log(f"  (gerbang) elemen {json.dumps(target, ensure_ascii=False)} tidak ada, dilewati")
                    continue
                s = {**langkah, "nama": langkah.get("nama") or f"gerbang_{n}",
                     "catatan": ((langkah.get("catatan") or "") + " | langkah gerbang izin aplikasi, dijalankan skrip").strip(" |")}
                self.satu_langkah(self.rencana_ke or 0, s)
        finally:
            self.di_gerbang = False
        lolos = not self.ada(g["tanda"])
        log("  gerbang izin aplikasi " + ("terlewati" if lolos else "MASIH tampil (cek aplikasi.json -> gerbang_izin)"))
        return lolos

    def pulihkan_ke_app(self, i):
        """Aplikasi membuka layar sistem (Pengaturan/dialog izin): catat, izinkan, lalu kembali ke aplikasi."""
        paket = self.paket_aktif()
        self.aksi_sekarang, self.rencana_ke = "izin_pengaturan", i
        log(f"  aplikasi membuka layar sistem ({paket}); izin akan dinyalakan lalu kembali ke aplikasi")
        self.catat_langkah(f"layar_sistem_{i}", stabil=False, tambahan={"keluar_ke_pengaturan": paket},
                           catatan=f"aplikasi membawa pengguna ke layar sistem di luar aplikasi ({paket})")
        hasil = {}

        def izinkan_lalu_kembali():
            self.tutup_dialog_izin()
            hasil["label"] = self.tangani_pengaturan() if self.di_luar_app() else "dialog izin sistem"
            for _ in range(4):
                if self.paket_aktif() == self.pkg:
                    break
                self.d.back()
                time.sleep(0.8)
            if self.paket_aktif() != self.pkg:
                adb("shell", "am", "start", "-n", f'{self.pkg}/{self.app["activity"]}')

        ok = self.catat_langkah(f"izinkan_di_pengaturan_{i}", aksi=izinkan_lalu_kembali,
                                sampai=lambda: self.paket_aktif() == self.pkg, batas=15,
                                tambahan={"keluar_ke_pengaturan": paket})
        label = hasil.get("label")
        self.langkah[-1]["catatan"] = (f"izin dinyalakan otomatis: '{label}', lalu kembali ke aplikasi" if label else
                                       "tidak menemukan sakelar izin yang aman untuk dinyalakan; fitur terkait mungkin "
                                       "tetap terkunci") + " (dilakukan skrip, bukan bagian alur aplikasi)"
        return ok

    # ---------- pencatatan langkah
    def ambil(self, dasar, screenshot=True):
        hasil = {"screenshot": None, "xml": None, "metrik": None, "paket": None}
        xml = None
        try:
            xml = self.d.page_source
            (self.folder / f"{dasar}.xml").write_text(xml, encoding="utf-8")
            hasil["xml"] = f"{dasar}.xml"
            hasil["paket"] = paket_xml(_root(xml))
            hasil["metrik"] = analisis_xml(xml, self.density, zona=self.zona_sistem())
            self.xml_terakhir = xml
        except Exception as e:  # noqa: BLE001
            log("  gagal mengambil struktur layar:", e)
        if screenshot and (self.saring_screenshot is None or not xml or self.saring_screenshot(xml)):
            try:
                png = self.folder / f"{dasar}.png"
                self.d.get_screenshot_as_file(str(png))
                hasil["screenshot"] = png.name
            except Exception as e:  # noqa: BLE001
                log("  gagal mengambil screenshot:", e)
        return hasil

    def catat_langkah(self, nama, aksi=None, sampai=None, batas=15.0, stabil=True,
                      catatan="", tambahan=None, xml_sebelum=None, screenshot=True):
        """Jalankan satu aksi, ukur waktunya, lalu simpan screenshot + XML + metrik.
        xml_sebelum (page source sebelum aksi) dipakai untuk mengukur mulai_berubah_ms dan mendeteksi aksi yang tidak
        mengubah layar sama sekali."""
        nama = _slug(nama)
        sebelum = _norm_xml(xml_sebelum) if xml_sebelum else None
        t0 = time.perf_counter()
        t_mulai = round((t0 - self.t_acuan) * 1000)
        berhasil, galat, respon, stabil_dt, berubah_dt = True, None, None, None, None
        keluar = {}
        cek_luar = [t0 + 1.0]

        def kondisi():
            if sampai():
                return True
            if self.aksi_sekarang != "izin_pengaturan" and time.perf_counter() >= cek_luar[0]:
                cek_luar[0] = time.perf_counter() + 1.0
                if self.di_luar_app():
                    keluar["paket"] = self.paket_aktif()
                    return True
            return False

        try:
            if aksi:
                aksi()
            if sampai:
                berhasil = self.tunggu(kondisi, batas)
                if not berhasil:
                    galat = f"kondisi tujuan tidak terpenuhi dalam {batas:g} detik"
            respon = time.perf_counter() - t0
            if keluar:
                catatan = (catatan + " | " if catatan else "") + (
                    f"aplikasi membuka layar sistem ({keluar['paket']}); kondisi tujuan belum diperiksa")
                tambahan = {**(tambahan or {}), "keluar_ke_pengaturan": keluar["paket"]}
            elif stabil and berhasil:
                berubah_dt, stabil_dt = self.tunggu_stabil(t0, sebelum)
        except Exception as e:  # noqa: BLE001 - kegagalan apa pun dicatat sebagai hasil uji
            berhasil = False
            pesan = str(e).splitlines()[0][:300] if str(e) else ""
            galat = pesan if isinstance(e, (HentiTunggu, RuntimeError)) and pesan else f"{type(e).__name__}: {pesan}"
            respon = time.perf_counter() - t0

        no = len(self.langkah) + 1
        tangkapan = self.ambil(f"{no:02d}_{_slug(nama)}", screenshot=screenshot)
        tambah = dict(tambahan or {})
        # Pemeriksaan sesudah aksi: layar tidak berubah, aplikasi keluar ke layar utama HP/aplikasi lain, galat di log.
        if sebelum is not None and self.xml_terakhir is not None and berhasil and not keluar:
            if _norm_xml(self.xml_terakhir) == sebelum:
                tambah["tanpa_perubahan"] = True
            elif berubah_dt is not None:
                tambah["mulai_berubah_ms"] = round(berubah_dt * 1000)
        paket = tangkapan.get("paket")
        if paket and paket != self.pkg and not POLA_PENGATURAN.search(paket) and not tambah.get("privasi") \
                and self.aksi_sekarang not in ("buka_aplikasi", "izin_pengaturan"):
            tambah["keluar_aplikasi"] = paket
            if POLA_LAUNCHER.search(paket):
                if self.aksi_sekarang == "kembali":
                    catatan = (catatan + " | " if catatan else "") + (
                        "Back dari layar akar menutup aplikasi (perilaku standar Android, bukan cacat)")
                elif berhasil:
                    berhasil = False
                    galat = ("aplikasi tertutup ke layar utama HP setelah aksi (crash, atau ketukan mengenai tombol "
                             "navigasi sistem)")
            else:
                catatan = (catatan + " | " if catatan else "") + f"aksi membuka aplikasi lain ({paket})"
        galat_log = self.log_galat_baru()
        if galat_log:
            tambah["log_galat"] = galat_log
            if any(g["jenis"] in ("crash", "anr") for g in galat_log):
                tambah["crash"] = True
                if berhasil:
                    berhasil, galat = False, "aplikasi crash/tidak merespons (lihat log_galat)"
        rekaman = {
            "no": no, "langkah": nama, "aksi": self.aksi_sekarang, "rencana_ke": self.rencana_ke,
            "berhasil": berhasil, "t_mulai_ms": t_mulai,
            "respon_ms": round(respon * 1000) if (sampai and respon is not None) else None,
            "sampai_stabil_ms": round(stabil_dt * 1000) if stabil_dt is not None else None,
            "durasi_aksi_ms": round(respon * 1000) if (not sampai and respon is not None) else None,
            "galat": galat, "catatan": catatan or None, **tambah, **tangkapan,
        }
        self.langkah.append(rekaman)
        m = (tangkapan["metrik"] or {}).get("masalah", {})
        waktu = rekaman["respon_ms"] if rekaman["respon_ms"] is not None else rekaman["sampai_stabil_ms"]
        log(f"[{no:02d}] {nama:<30} {'OK   ' if berhasil else 'GAGAL'} waktu={waktu} ms "
            f"tanpa_label={len(m.get('interaktif_tanpa_label', []))} "
            f"target_kecil={len(m.get('target_sentuh_di_bawah_48dp', []))}"
            + (" tanpa_perubahan" if tambah.get("tanpa_perubahan") else "")
            + (f" log_galat={len(galat_log)}" if galat_log else "")
            + (f"  ({galat})" if galat else ""))
        return berhasil

    # ---------- log aplikasi (logcat): crash, ANR, galat Flutter/Java per langkah
    def mulai_log(self):
        try:
            self.log_sejak = float(adb_baca("shell", "date", "+%s").strip())
        except ValueError:
            self.log_sejak = None
        self.pid_app = (adb_baca("shell", "pidof", self.pkg).split() or [None])[0]

    def log_galat_baru(self):
        """Baris logcat baru sejak pemeriksaan terakhir yang menandakan crash/ANR/galat aplikasi yang diuji."""
        if self.log_sejak is None:
            return []
        out = adb_baca("logcat", "-d", "-v", "epoch", "-T", f"{self.log_sejak:.3f}", timeout=20)
        hasil, lihat, crash_pid = [], set(), None
        for baris in out.splitlines():
            m = POLA_LOGCAT.match(baris)
            if not m:
                continue
            waktu, pid, tingkat, tag, pesan = float(m.group(1)), m.group(2), m.group(4), m.group(5).strip(), m.group(6)
            self.log_sejak = max(self.log_sejak, waktu + 0.001)
            mulai = re.search(r"Start proc (\d+):" + re.escape(self.pkg) + r"\b", pesan)
            if mulai:
                self.pid_app = mulai.group(1)
                continue
            jenis = None
            if tag == "AndroidRuntime" and ("FATAL EXCEPTION" in pesan or pid == crash_pid):
                crash_pid = pid
                jenis = "crash" if pid == self.pid_app or self.pkg in out else None
            elif tag == "ActivityManager" and pesan.startswith(f"ANR in {self.pkg}"):
                jenis = "anr"
            elif tag == "flutter" and (tingkat in "EF" or POLA_LOG_GALAT.search(pesan)):
                jenis = "galat_flutter"
            elif pid == self.pid_app and tag in ("System.err", "AndroidRuntime") and POLA_LOG_GALAT.search(pesan):
                jenis = "galat_java"
            if not jenis:
                continue
            bersih = sensor_log(pesan)[:300]
            if (jenis, bersih) in lihat:
                continue
            lihat.add((jenis, bersih))
            hasil.append({"jenis": jenis, "tingkat": tingkat, "tag": tag, "pesan": bersih})
        return hasil[:8]

    def gagal_langkah(self, nama, pesan, tambahan=None):
        def lempar():
            raise RuntimeError(pesan)
        self.catat_langkah(nama, aksi=lempar, stabil=False, tambahan=tambahan)
        return False

    def page_source_aman(self):
        try:
            return self.d.page_source
        except Exception:
            return None

    # ---------- kebijakan perubahan data (CRUD dengan pemulihan)
    def ada_penanda(self, teks):
        return bool(teks) and any(p and p in teks for p in self.penanda_aktif)

    def tombol_milik_data_uji(self, node, root, induk):
        """Tombol hapus/ubah status hanya boleh untuk data buatan uji: kontainer terbesar yang hanya memuat SATU tombol
        berlabel sama (baris daftar, atau halaman detail) harus memuat penanda data uji."""
        lb = _label(node)
        wadah = node
        for anc in leluhur(node, induk):
            if any(x is not node and x.get("clickable") == "true" and _label(x) == lb for x in anc.iter()):
                break
            wadah = anc
        teks = " ".join(filter(None, (_label(x) or x.get("text") for x in wadah.iter())))
        self.penanda_terakhir = next((p for p in self.penanda_aktif if p and p in teks), None)
        return self.penanda_terakhir is not None

    def alasan_blokir(self, label, xml=None, node=None):
        jenis = jenis_risiko(label)
        if jenis == "selalu":
            return (f"tap diblokir: '{label}' berdampak ke luar aplikasi/akun (pembayaran, pesan, telepon, hapus akun) "
                    "dan tidak bisa dipulihkan")
        try:
            root = _root(xml) if xml else None
        except ET.ParseError:
            root = None
        if root is not None and tap_mengunci_akun(root, label):
            return "tap diblokir: mengubah kata sandi atau verifikasi 2 langkah akun uji bisa mengunci akun"
        if jenis is None:
            return None
        if not self.izinkan_berisiko:
            return f"tap diblokir: elemen '{label}' bisa mengubah data/keluar dari aplikasi"
        if jenis == "data_lama":
            ok = False
            if root is not None and node is not None:
                _, induk = pohon_induk(root)
                cocok = next((x for x in root.iter() if x.get("bounds") == node.get("bounds") and _label(x) == _label(node)), None)
                ok = cocok is not None and self.tombol_milik_data_uji(cocok, root, induk)
            if not ok:
                return (f"tap '{label}' diblokir: menghapus/mengubah status hanya boleh pada data buatan uji (tidak ada "
                        f"penanda {', '.join(sorted(self.penanda_aktif)) or 'data uji'} di baris/halaman ini)")
        return None

    def catat_ubah_data(self, s, ok):
        jenis = s.get("ubah_data")
        if not jenis or jenis == "biarkan":
            return
        teks = json.dumps(s, ensure_ascii=False)
        self.ubah_data.append({
            "no": self.langkah[-1]["no"] if self.langkah else None, "langkah": self.langkah[-1]["langkah"] if self.langkah else None,
            "fase": self.fase, "jenis": jenis, "objek": s.get("objek"), "berhasil": ok,
            "penanda": self.penanda if (self.penanda and (self.penanda in teks or jenis == "buat")) else
            next((p for p in self.penanda_aktif if p in teks), None)
            or (self.penanda_terakhir if jenis in ("hapus", "status") else None)})
        if self.langkah:
            self.langkah[-1]["ubah_data"] = jenis

    def kembalikan_sakelar(self, b_awal, checked_awal, nama):
        """Sakelar yang diketuk dikembalikan ke keadaan awal (dialog sistem seperti sidik jari dibatalkan dulu)."""
        time.sleep(0.4)
        if self.aplikasi_tidak_di_depan():
            self.d.back()
            time.sleep(0.6)
        xml = self.page_source_aman()
        if not xml:
            return
        root = _root(xml)
        urut, induk = pohon_induk(root)
        sakelar = [el for el in urut if el.get("checkable") == "true" and el.get("bounds") == b_awal]
        if not sakelar:
            log("  sakelar tidak ditemukan lagi; keadaannya tidak bisa diperiksa")
            return
        if sakelar[0].get("checked") == checked_awal:
            return
        titik, _ = titik_terlihat(root, sakelar[0], self.zona_sistem(), urut, induk)
        if not titik:
            return
        aksi_lama, self.aksi_sekarang = self.aksi_sekarang, "pemulihan_otomatis"
        self.catat_langkah(f"{nama}_kembalikan", aksi=lambda: self.d.tap([titik]), xml_sebelum=xml, screenshot=False,
                           tambahan={"fase": "pemulihan"},
                           catatan=f"sakelar dikembalikan ke keadaan awal ({'aktif' if checked_awal == 'true' else 'nonaktif'}); "
                                   "dilakukan skrip, bukan bagian alur aplikasi")
        self.aksi_sekarang = aksi_lama

    def ganti_nilai_awal(self, teks):
        def ganti(m):
            return self.nilai_awal.get(_slug(m.group(1)), "")
        return re.sub(r"\{nilai_awal:([^}]+)\}", ganti, str(teks))

    # ---------- pemulihan otomatis dan eksekusi rencana
    def aplikasi_tidak_di_depan(self):
        paket = self.paket_aktif()
        return bool(paket) and paket != self.pkg and not POLA_PENGATURAN.search(paket)

    def catat_kembali_ke_app(self, i):
        paket = self.paket_aktif()
        self.aksi_sekarang, self.rencana_ke = "pemulihan_otomatis", i
        log(f"  aplikasi tidak di depan ({paket}); dibawa kembali sebelum langkah {i}")
        return self.catat_langkah(f"kembali_ke_aplikasi_{i}", aksi=self.kembali_ke_app, stabil=False,
                                  sampai=lambda: self.paket_aktif() == self.pkg, batas=40,
                                  catatan=f"aplikasi tidak di depan ({paket}); skrip membawanya kembali (bukan alur aplikasi)")

    def mulai_ulang_app(self, i, alasan):
        self.aksi_sekarang, self.rencana_ke = "pemulihan_otomatis", i
        log(f"  {alasan}: aplikasi dimulai ulang")

        def ulang():
            adb("shell", "am", "force-stop", self.pkg)
            time.sleep(0.8)
            adb("shell", "am", "start", "-W", "-n", f'{self.pkg}/{self.app["activity"]}')
            self.tunggu_app_siap()
            self.tutup_dialog_izin()
            self.lewati_gerbang()
        return self.catat_langkah(f"mulai_ulang_aplikasi_{i}", aksi=ulang, stabil=False,
                                  catatan=f"{alasan}; aplikasi dimulai ulang oleh skrip (bukan alur aplikasi)")

    def jalankan(self, rencana):
        """Langkah gagal tidak menghentikan rencana: skrip memulihkan keadaan lalu lanjut. Berhenti hanya kalau
        buka_aplikasi/login gagal, langkah bertanda berhenti_jika_gagal gagal, atau 6 langkah gagal berturut-turut."""
        langkah, beruntun, berhenti = rencana["langkah"], 0, False
        for i, s in enumerate(langkah, 1):
            self.berikutnya = langkah[i] if i < len(langkah) else None
            if s["aksi"] not in ("buka_aplikasi", "reset_aplikasi"):
                if self.di_luar_app():
                    self.pulihkan_ke_app(i)
                elif s["aksi"] != "login" and self.aplikasi_tidak_di_depan():
                    self.catat_kembali_ke_app(i)
            if self.satu_langkah(i, s):
                beruntun = 0
                continue
            if s["aksi"] in ("buka_aplikasi", "login") or s.get("berhenti_jika_gagal"):
                berhenti = True
                break
            beruntun += 1
            if beruntun >= 6:
                log("  6 langkah gagal berturut-turut; sisa rencana dihentikan")
                berhenti = True
                break
            if beruntun % 3 == 0:
                self.mulai_ulang_app(i, "3 langkah gagal berturut-turut")
        self.jalankan_pemulihan(rencana)
        return not berhenti and all(l["berhasil"] for l in self.langkah)

    def jalankan_pemulihan(self, rencana):
        """Langkah "pemulihan" rencana SELALU dijalankan (juga kalau langkah utama gagal) untuk mengembalikan data."""
        pulih = rencana.get("pemulihan") or []
        if not pulih:
            return
        log(f"--- Pemulihan data: {len(pulih)} langkah")
        self.fase = "pemulihan"
        n0 = len(self.langkah)
        try:
            for j, s in enumerate(pulih, 1):
                s = {**s, "nama": f"pulihkan_{s.get('nama') or s['aksi']}"}
                if s["aksi"] not in ("buka_aplikasi", "reset_aplikasi") and self.aplikasi_tidak_di_depan():
                    self.catat_kembali_ke_app(1000 + j)
                self.satu_langkah(1000 + j, s)
        finally:
            self.fase = "utama"
        for lk in self.langkah[n0:]:
            lk["fase"] = "pemulihan"

    def kondisi_dari(self, s):
        muncul, hilang = s.get("tunggu_muncul"), s.get("tunggu_hilang")
        if not muncul and not hilang:
            return None
        return lambda: (not muncul or self.ada(muncul)) and (not hilang or not self.ada(hilang))

    def ada_di_layar(self, target, gulir=0):
        """Cari target di layar, kalau perlu sambil menggulir ke bawah lalu kembali ke atas."""
        xml = self.page_source_aman()
        if xml and cari_node(_root(xml), target) is not None:
            return True
        ketemu, n = False, 0
        for n in range(1, gulir + 1):
            self.gulir_ke(None, None, {}, arah="bawah", porsi=0.7)
            xml = self.page_source_aman()
            if xml and cari_node(_root(xml), target) is not None:
                ketemu = True
                break
        for _ in range(n):
            self.gulir_ke(None, None, {}, arah="atas", porsi=0.7)
        return ketemu

    def satu_langkah(self, i, s):
        aksi = s["aksi"]
        self.aksi_sekarang, self.rencana_ke = aksi, i
        nama = _slug(s.get("nama") or f"{aksi}_{i}")
        batas = float(s.get("batas_detik", 15))
        sampai = self.kondisi_dari(s)
        catatan = s.get("catatan", "")
        tambah_fase = {"fase": "pemulihan"} if self.fase == "pemulihan" else None

        if aksi == "buka_aplikasi":
            return self.buka_aplikasi(nama, s.get("cold_start", True), sampai, batas)
        if aksi == "login":
            return self.login(nama, s["peran"], s.get("bersihkan_jika_perlu", True),
                              s.get("paksa_login_ulang", False))
        if aksi == "reset_aplikasi":
            def reset():
                adb("shell", "pm", "clear", self.pkg)
                tulis_status_login(self.pkg, None)
                self.izin_ulang("setelah reset_aplikasi")
            return self.catat_langkah(nama, aksi=reset, stabil=False, catatan="data aplikasi dihapus (pm clear)")
        if aksi == "kembali":
            return self.catat_langkah(nama, aksi=self.d.back, sampai=sampai, batas=batas, catatan=catatan,
                                      xml_sebelum=self.page_source_aman(), tambahan=tambah_fase)
        if aksi == "tunggu":
            return self.catat_langkah(nama, aksi=lambda: time.sleep(float(s.get("detik", 1))),
                                      stabil=False, catatan=catatan)
        if aksi == "tangkap":
            return self.catat_langkah(nama, stabil=False, catatan=catatan, tambahan=tambah_fase)
        if aksi == "atur_skala_font":
            skala = float(s.get("skala", 1.0))

            def atur():
                if self.skala_font_awal is None:
                    self.skala_font_awal = baca_skala_font()
                adb("shell", "settings", "put", "system", "font_scale", f"{skala:g}")
            return self.catat_langkah(nama, aksi=atur, sampai=sampai, batas=batas,
                                      catatan=(catatan + f" | skala font sistem {skala:g}").strip(" |"))
        if aksi == "atur_tema":
            gelap = s.get("tema", "gelap") != "terang"

            def tema():
                if self.tema_awal is None:
                    m = re.search(r"Night mode:\s*(\w+)", adb_baca("shell", "cmd", "uimode", "night"))
                    self.tema_awal = m.group(1) if m else "no"
                adb("shell", "cmd", "uimode", "night", "yes" if gelap else "no")
            return self.catat_langkah(nama, aksi=tema, sampai=sampai, batas=batas, xml_sebelum=self.page_source_aman(),
                                      catatan=(catatan + f" | tema sistem {'gelap' if gelap else 'terang'} "
                                                         "(dikembalikan otomatis di akhir uji)").strip(" |"))
        if aksi == "atur_jaringan":
            hidup = s.get("status", "mati") != "mati"

            def jaringan():
                self.atur_jaringan(hidup)
            return self.catat_langkah(nama, aksi=jaringan, sampai=sampai, batas=batas, stabil=False,
                                      catatan=(catatan + (" | internet dinyalakan lagi" if hidup else
                                                          " | mode pesawat: aplikasi tanpa internet (dikembalikan "
                                                          "otomatis di akhir uji)")).strip(" |"))
        if aksi == "gulir":
            return self.catat_langkah(nama, aksi=lambda: self.gulir(s.get("arah", "bawah"),
                                                                     int(s.get("jumlah", 1))),
                                      sampai=sampai, batas=batas, catatan=catatan, xml_sebelum=self.page_source_aman())
        if aksi in ("pastikan_ada", "pastikan_tidak_ada"):
            harap = aksi == "pastikan_ada"
            target = s["target"]

            def periksa():
                ada = self.tunggu(lambda: self.ada_di_layar(target), float(s.get("batas_cari_detik", 3))) if harap \
                    else self.ada_di_layar(target)
                if not ada and s.get("gulir_cari", True):
                    ada = self.ada_di_layar(target, gulir=4)
                if ada != harap:
                    raise RuntimeError(f"pemeriksaan gagal: {json.dumps(target, ensure_ascii=False)} "
                                       + ("tidak ditemukan" if harap else "masih ada"))
            return self.catat_langkah(nama, aksi=periksa, stabil=False, tambahan={**(tambah_fase or {}), "target": target},
                                      catatan=(catatan + f" | pemeriksaan: elemen harus {'ada' if harap else 'tidak ada'}").strip(" |"))

        target = s["target"]
        if aksi == "tap" and "koordinat" in target:
            x, y = (int(v) for v in target["koordinat"])
            if any(z[0] <= x < z[2] and z[1] <= y < z[3] for z in self.zona_sistem()):
                return self.gagal_langkah(nama, "koordinat berada di bilah status/navigasi HP; pakai label elemen")
            xml = self.page_source_aman()
            lb = elemen_di_titik(xml, x, y) if xml else ""
            alasan = self.alasan_blokir(lb, xml)
            if alasan:
                return self.gagal_langkah(nama, alasan)
            return self.catat_langkah(nama, aksi=lambda: self.d.tap([(x, y)]), sampai=sampai, xml_sebelum=xml,
                                      batas=batas, catatan=(catatan + " | tap koordinat").strip(" |"))

        # Cari elemen dulu (di luar pengukuran waktu), pastikan bagian yang diketuk terlihat, baru lakukan aksi.
        batas_cari = float(s.get("batas_cari_detik", 10))
        info = self.siapkan_ketuk(target, batas_cari)
        pemulihan = []
        if not info.get("titik") and not s.get("tanpa_pemulihan"):
            info, pemulihan = self.coba_pulihkan_target(target, info, batas_cari)
        teks_target = json.dumps(target, ensure_ascii=False)
        if not info.get("titik"):
            if info.get("alasan") == "tertutup":
                n = info["node"]
                return self.gagal_langkah(
                    nama, f"elemen {teks_target} tertutup bilah navigasi/elemen lain dan tidak bisa digulir ke area "
                          "yang terlihat", tambahan={"tak_terjangkau": {"label": _label(n) or None, "kelas": (n.get("class") or "").split(".")[-1],
                                                                         "bounds": n.get("bounds")}, "pemulihan": pemulihan or None})
            return self.gagal_langkah(nama, f"elemen tidak ditemukan: {teks_target}",
                                      tambahan={"pemulihan": pemulihan or None})
        node, titik = info["node"], info["titik"]
        lb = _label(node) or node.get("text") or ""
        extra = {"titik_ketuk": list(titik)}
        if info.get("digulir"):
            extra["digulir"] = info["digulir"]
        if pemulihan:
            extra["pemulihan"] = pemulihan
        if tambah_fase:
            extra.update(tambah_fase)

        if aksi == "tap":
            alasan = self.alasan_blokir(lb, info["xml"], node)
            if alasan:
                return self.gagal_langkah(nama, alasan, tambahan={"diblokir": True})
            # Sakelar pengaturan (Switch/Toggle) dikembalikan otomatis; kotak centang di form & gerbang izin tidak.
            sakelar = (node.get("checkable") == "true" and not self.di_gerbang
                       and node.get("class") in ("android.widget.Switch", "android.widget.ToggleButton"))
            b_awal, checked_awal = node.get("bounds"), node.get("checked")
            ok = self.catat_langkah(nama, aksi=lambda: self.d.tap([titik]), sampai=sampai, batas=batas,
                                    catatan=catatan, xml_sebelum=info["xml"], tambahan=extra)
            self.catat_ubah_data(s, ok)
            if ok and KATA_KELUAR_AKUN.search(lb) and jenis_risiko(lb):
                tulis_status_login(self.pkg, None)
            if sakelar and s.get("ubah_data") != "biarkan":
                self.kembalikan_sakelar(b_awal, checked_awal, nama)
            return ok

        # aksi == "ketik"
        rahasia = s.get("dari_akun") == "password"
        nilai = self.ganti_nilai_awal(s["teks"]) if "teks" in s else self.nilai_akun(s["dari_akun"])
        if nilai is None:
            return self.gagal_langkah(nama, f"nilai '{s.get('dari_akun')}' tidak ada di akun peran aktif")
        hint = node.get("hint") or ""

        def isi():
            self.d.tap([titik])
            time.sleep(0.2)
            el = self.cari(target)
            if el is None:
                raise RuntimeError("kolom input tidak bisa difokuskan")
            if self.izinkan_berisiko and not rahasia and node.get("password") != "true":
                lama = el.get_attribute("text") or ""
                self.nilai_awal[nama] = "" if lama == hint else lama
            if s.get("hapus_dulu", True):
                try:
                    el.clear()
                except Exception:
                    pass
            el.send_keys(nilai)
            if s.get("tutup_keyboard"):
                self.tutup_keyboard()

        ok = self.catat_langkah(nama, aksi=isi, sampai=sampai, batas=batas, stabil=False, tambahan=extra,
                                catatan=(catatan + " | input; durasi termasuk waktu mengetik Appium"
                                         + (" | nilai rahasia" if rahasia else "")).strip(" |"))
        self.catat_ubah_data(s, ok)
        return ok

    def atur_jaringan(self, hidup):
        """Mode pesawat lewat adb (USB tetap tersambung); keadaan awal disimpan untuk dikembalikan."""
        if self.pesawat_awal is None:
            self.pesawat_awal = adb_baca("shell", "settings", "get", "global", "airplane_mode_on").strip() or "0"
        adb("shell", "cmd", "connectivity", "airplane-mode", "disable" if hidup else "enable")
        time.sleep(1.5)
        kini = adb_baca("shell", "settings", "get", "global", "airplane_mode_on").strip()
        if kini != ("0" if hidup else "1"):  # HP lama: matikan/nyalakan Wi-Fi dan data seluler
            for svc in ("wifi", "data"):
                adb("shell", "svc", svc, "enable" if hidup else "disable")
            self.jaringan_svc = not hidup

    def kembalikan_pengaturan_hp(self):
        """Skala font, tema, dan jaringan dikembalikan ke keadaan sebelum uji."""
        if self.skala_font_awal is not None:
            adb("shell", "settings", "put", "system", "font_scale", f"{self.skala_font_awal:g}")
            log(f"skala font sistem dikembalikan ke {self.skala_font_awal:g}")
        if self.tema_awal is not None:
            adb("shell", "cmd", "uimode", "night", self.tema_awal)
            log(f"tema sistem dikembalikan ({self.tema_awal})")
        if self.pesawat_awal is not None:
            adb("shell", "cmd", "connectivity", "airplane-mode", "enable" if self.pesawat_awal == "1" else "disable")
            if self.jaringan_svc:
                for svc in ("wifi", "data"):
                    adb("shell", "svc", svc, "enable")
            log("jaringan dikembalikan ke keadaan awal")

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
            return (self.jumlah(f'packageName("{self.pkg}").clickable(true)') > 0 or self.ada_dialog_izin()
                    or self.di_luar_app())

        ok = self.catat_langkah(
            nama, aksi=buka, sampai=sampai or siap, batas=max(batas, 30), tambahan={"startup": startup},
            catatan=("cold start; " if cold else "") + "respon_ms = sampai layar pertama punya elemen yang bisa diklik",
        )
        self.tutup_dialog_izin()
        if ok and (self.berikutnya or {}).get("aksi") != "login":  # kalau langsung login, gerbang ditangani sesudahnya
            self.lewati_gerbang()
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
            self.lewati_gerbang()
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
                ok = self.catat_langkah(f"{nama}_sudah_login", stabil=False,
                                        catatan=f"sesi login {peran} sebelumnya masih aktif; login tidak diulang")
                self.lewati_gerbang()
                return ok
            if not bersihkan:
                return self.gagal_langkah(nama, "form login tidak ditemukan (kemungkinan masih login)")
            log("  form login tidak ada (kemungkinan masih login akun lain); menghapus data aplikasi lalu membuka ulang")
            adb("shell", "pm", "clear", self.pkg)
            tulis_status_login(self.pkg, None)
            self.izin_ulang("setelah data aplikasi dihapus untuk login")
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
            self.lewati_gerbang()
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
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Gagal(f"akun.json tidak valid: {e}") from e
    if FILE_APLIKASI.exists():  # perilaku per aplikasi (gerbang izin, label di HP); akun.json tetap menang
        try:
            tambahan = json.loads(FILE_APLIKASI.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise Gagal(f"aplikasi.json tidak valid: {e}") from e
        cfg.setdefault("hp", {})
        for k, v in (tambahan.get("hp") or {}).items():
            if not k.startswith("_"):
                cfg["hp"].setdefault(k, v)
        for kode, isi in tambahan.get("aplikasi", {}).items():
            if kode in cfg.get("aplikasi", {}):
                for k, v in isi.items():
                    if not k.startswith("_"):
                        cfg["aplikasi"][kode].setdefault(k, v)
    return cfg


def sambung_appium():
    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
    except ImportError as e:
        raise Gagal("library Appium belum terpasang; jalankan: pip install Appium-Python-Client") from e
    opsi = UiAutomator2Options()
    opsi.no_reset = True
    opsi.new_command_timeout = 600
    if SERIAL_AKTIF:
        opsi.udid = SERIAL_AKTIF  # HP yang sama dengan yang dipakai perintah adb
    try:
        return webdriver.Remote(APPIUM_URL, options=opsi)
    except Exception as e:  # noqa: BLE001
        raise Gagal(f"gagal menyambung ke Appium di {APPIUM_URL}: {str(e).splitlines()[0][:200]} "
                    "(pastikan 'appium' sedang jalan dan sesi Inspector sudah ditutup)") from e


def buat_penanda():
    """Penanda unik data buatan uji, ditulis di kolom teks supaya data itu bisa dicari dan dihapus lagi."""
    return f"UJI-{datetime.now():%m%d-%H%M%S}"


def status_pemulihan(langkah, ubah_data):
    """Status pemulihan tiap perubahan data yang berhasil di fase utama:
    dipulihkan (terbukti hilang lewat pastikan_tidak_ada / langkah pemulihan lengkap), dihapus (dihapus tapi belum
    diperiksa), tidak_perlu (yang dihapus/diubah adalah data buatan uji), atau belum."""
    pulih_ok = [l for l in langkah if l.get("fase") == "pemulihan"]
    semua_pulih_ok = bool(pulih_ok) and all(l["berhasil"] for l in pulih_ok)
    hasil = []
    for e in ubah_data:
        if e["fase"] != "utama" or not e["berhasil"]:
            continue
        m = e.get("penanda")
        sesudah = [l for l in langkah if l["no"] > (e["no"] or 0)]
        hilang = m and any(l.get("aksi") == "pastikan_tidak_ada" and l["berhasil"] and m in json.dumps(l.get("target") or {})
                           for l in sesudah)
        dihapus = m and any(x["jenis"] == "hapus" and x["berhasil"] and x.get("penanda") == m and x["no"] > e["no"]
                            for x in ubah_data)
        if e["jenis"] in ("hapus", "status"):
            st = "tidak_perlu"  # hanya bisa lolos pengaman pada data berpenanda (data buatan uji)
        elif hilang:
            st = "dipulihkan"
        elif e["jenis"] == "ubah" and semua_pulih_ok:
            st = "dipulihkan"
        elif dihapus:
            st = "dihapus"
        else:
            st = "belum"
        hasil.append({**e, "status_pulih": st})
    return hasil


def buka_pelaksana(kode_app, cfg, folder, izinkan_berisiko=False, judul="uji"):
    """Pemeriksaan HP + aplikasi, folder hasil, sambungan Appium, dan objek Pelaksana."""
    app = cfg["aplikasi"][kode_app]
    pastikan_hp(cfg)
    if not aplikasi_terpasang(app["package"]):
        raise Gagal(f"aplikasi {app['package']} tidak terpasang di HP ({SERIAL_AKTIF})")
    info = info_perangkat(app["package"])
    if not info["model"]:
        raise Gagal(f"HP {SERIAL_AKTIF} tidak menjawab perintah adb. Cek kabel USB dan buka kunci layar HP, "
                    "lalu jalankan lagi")
    folder = Path(folder) if folder else (FOLDER_DASAR / f"{datetime.now():%Y%m%d_%H%M%S}_{kode_app}_{_slug(judul)}")
    folder.mkdir(parents=True, exist_ok=True)
    log(f"Menyambung ke Appium di {APPIUM_URL} ...")
    driver = sambung_appium()
    return Pelaksana(driver, cfg, kode_app, folder, info, izinkan_berisiko), driver, info, folder


def tulis_ringkasan(p, rencana, info, mulai, t0, berhasil, video, izinkan_berisiko, tambahan=None):
    app = p.app
    perubahan = status_pemulihan(p.langkah, p.ubah_data)
    ringkasan = {
        "aplikasi": {"kode": p.kode_app, "nama": app.get("nama"), "package": app["package"],
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
        "beri_izin_otomatis": p.beri_izin,
        "izin_otomatis": p.izin_otomatis,
        "catatan_metode": CATATAN_METODE,
        "penanda": p.penanda,
        "perubahan_data": perubahan,
        "pemulihan_lengkap": all(x["status_pulih"] in ("dipulihkan", "tidak_perlu") for x in perubahan),
        "zona_sistem": p._zona,
        "langkah": p.langkah,
        "layar_akhir": daftar_elemen(p.xml_terakhir, p.density) if p.xml_terakhir else [],
        **(tambahan or {}),
    }
    (p.folder / "ringkasan.json").write_text(json.dumps(ringkasan, ensure_ascii=False, indent=2), encoding="utf-8")
    return ringkasan


def hasil_run(p, berhasil, video, folder):
    gagal = [l for l in p.langkah if not l["berhasil"]]
    log(f"\n{'BERHASIL' if berhasil else 'TIDAK BERHASIL'}. Hasil: {folder}")
    g = gagal[0] if gagal else None
    return {
        "status": "selesai" if berhasil else "gagal_langkah",
        "berhasil": berhasil,
        "folder": str(folder),
        "ringkasan": str(folder / "ringkasan.json"),
        "jumlah_langkah": len(p.langkah),
        "jumlah_gagal": len(gagal),
        "langkah_gagal": ({"no": g["no"], "langkah": g["langkah"], "galat": g["galat"], "screenshot": g["screenshot"]}
                          if g else None),
        "rekaman": video,
    }


def jalankan_rencana(rencana, cfg, rekam=False, folder=None, izinkan_berisiko=False):
    penanda = buat_penanda()
    rencana = json.loads(json.dumps(rencana, ensure_ascii=False).replace("{penanda}", penanda))
    salah = validasi_rencana(rencana, cfg, izinkan_berisiko)
    if salah:
        return {"status": "rencana_tidak_valid", "berhasil": False, "kesalahan": salah}

    kode_app = rencana["aplikasi"]
    p, driver, info, folder = buka_pelaksana(kode_app, cfg, folder, izinkan_berisiko, rencana.get("judul", "uji"))
    (folder / "rencana.json").write_text(json.dumps(rencana, ensure_ascii=False, indent=2), encoding="utf-8")
    p.peran_aktif = rencana.get("peran")
    p.beri_izin = rencana.get("beri_izin_otomatis", p.beri_izin)
    p.penanda = penanda
    p.penanda_aktif = {penanda, *[str(x) for x in rencana.get("penanda_sesi") or [] if x]}
    p.izin_ulang("awal uji")
    if p.beri_izin and rencana["langkah"][0]["aksi"] == "buka_aplikasi":
        st = p.aktifkan_autostart()
        if st:
            p.izin_otomatis.append({"saat": "awal uji", "runtime": [], "appops": [],
                                    "lain": [] if st == "gagal" else ["autostart_xiaomi"],
                                    "gagal": ["AUTOSTART_XIAOMI"] if st == "gagal" else []})
    mulai = datetime.now().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    berhasil, video = False, None
    p.mulai_log()
    sedang_rekam = rekam and p.mulai_rekam()
    p.t_acuan = time.perf_counter()
    try:
        berhasil = p.jalankan(rencana)
    finally:
        if sedang_rekam:
            video = p.selesai_rekam()
        p.kembalikan_pengaturan_hp()
        tulis_ringkasan(p, rencana, info, mulai, t0, berhasil, video, izinkan_berisiko)
        try:
            driver.quit()
        except Exception:
            pass
    return hasil_run(p, berhasil, video, folder)


# ====================================================================== penjelajah otomatis (tanpa LLM)
_BULAN = ("januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember|january|february|"
          "march|may|june|july|august|october|december|jan|feb|mar|apr|jun|jul|agu|agt|aug|sep|sept|okt|oct|nov|des|dec")
_HARI = ("senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu|ahad|monday|tuesday|wednesday|thursday|friday|saturday|"
         "sunday|sen|sel|rab|kam|jum|sab|min|mon|tue|wed|thu|fri|sat|sun")
POLA_TANGGAL_NORM = re.compile(rf"#[ ,.-]*\b(?:{_BULAN})\b\.?|\b(?:{_BULAN})\b\.?[ ,.-]*#")


def _norm_label(teks):
    """Label untuk mengenali layar/elemen yang sama walau datanya berbeda: penanda data uji dan angka jadi '#',
    nama bulan/hari di dalam tanggal jadi 'bln'/'hr' (sel kalender tetap sama walau bulannya bergeser),
    emoji/simbol dibuang, huruf kecil."""
    t = re.sub(r"UJI-\d{4}-\d{6}", "#", str(teks or "").split("\n")[0])
    t = re.sub(r"[^\w#/ .,:'()-]+", " ", re.sub(r"\d+", "#", t))
    t = re.sub(r"#(\s*#)+", "#", t)
    t = " ".join(t.split()).lower()
    if POLA_TANGGAL_NORM.search(t):
        t = POLA_TANGGAL_NORM.sub(lambda m: "# bln" if m.group(0).startswith("#") else "bln #", t)
        t = re.sub(rf"\b(?:{_HARI})\b", "hr", t)
    return t[:40]


def tanda_layar(root):
    """Sidik layar dari label elemen (angka diganti #, teks panjang diabaikan): layar yang sama tetap dikenali
    walau datanya berubah."""
    return frozenset(((el.get("class") or "").split(".")[-1], _norm_label(_label(el)))
                     for el in root.iter() if _label(el) and len(_label(el)) <= 60 and _bounds(el.get("bounds")))


def mirip(a, b):
    return 1.0 if not a and not b else len(a & b) / max(1, len(a | b))


def judul_layar(root):
    label = [_label(el).replace("\n", " ") for el in root.iter() if _label(el) and el.get("heading") == "true"]
    label += [_label(el).replace("\n", " ") for el in root.iter() if _label(el)]
    return " / ".join(dict.fromkeys(label[:3]))[:70] or "(tanpa teks)"


class Penjelajah:
    """Mengetuk SEMUA elemen yang bisa dijangkau di setiap layar aplikasi, tanpa mengubah data: menggulir untuk
    menemukan elemen di bawah, membuka lembar bawah/dialog, berpindah tab, dan kembali lewat Back atau tab semula.
    Item daftar yang sejenis cukup diwakili beberapa contoh. Tombol yang mengubah data dicatat 'diblokir' (bahan uji
    CRUD oleh LLM Perancang). Kalau tersesat, aplikasi dimulai ulang lalu jalur menuju layar diputar ulang."""

    def __init__(self, p, batas_ketukan=300, batas_menit=40.0, maks_kedalaman=6, maks_per_grup=2, maks_per_layar=None,
                 maks_gagal_beruntun=6):
        self.p = p
        self.batas_ketukan, self.batas_detik = batas_ketukan, batas_menit * 60
        self.maks_kedalaman, self.maks_per_grup = maks_kedalaman, maks_per_grup
        # Satu layar tidak boleh menghabiskan anggaran semua layar lain (mis. kalender atau daftar sangat panjang).
        self.maks_per_layar = maks_per_layar or max(30, batas_ketukan // 8)
        self.maks_gagal_beruntun = maks_gagal_beruntun
        self.layar, self.status, self.posisi_tetap, self.grup = [], {}, {}, {}
        self.ketukan, self.pemulihan, self.t0 = 0, 0, time.perf_counter()
        p.saring_screenshot = self.perlu_screenshot

    # ---------- keadaan layar
    def habis(self):
        return self.ketukan >= self.batas_ketukan or time.perf_counter() - self.t0 > self.batas_detik

    def baca(self):
        xml = self.p.page_source_aman()
        try:
            return _root(xml) if xml else None
        except ET.ParseError:
            return None

    @staticmethod
    def mirip_layar(L, tanda):
        """Kemiripan dengan layar L, termasuk varian hasil digulir (isi daftar panjang berganti saat digulir)."""
        return max(mirip(t, tanda) for t in [L["tanda"]] + L.get("varian", []))

    def cari_layar(self, root):
        tanda, paket = tanda_layar(root), paket_xml(root)
        nilai = [(self.mirip_layar(L, tanda), L) for L in self.layar if L["paket"] == paket]
        n, L = max(nilai, key=lambda x: x[0], default=(0, None))
        return L if n >= 0.6 else None

    def tambah_varian(self, L, root):
        """Layar L sesudah digulir penjelajah sendiri (tanpa pindah layar): simpan sidiknya supaya tetap dikenali."""
        if root is None or paket_xml(root) != L["paket"]:
            return
        tanda = tanda_layar(root)
        if self.mirip_layar(L, tanda) < 0.9 and len(L.setdefault("varian", [])) < 8:
            L["varian"].append(tanda)

    def kenali(self, root, jalur=None, dari=None, ref=None, screenshot=None):
        L = self.cari_layar(root)
        if L is not None:
            return L, False
        L = {"id": f"J{len(self.layar) + 1}", "tanda": tanda_layar(root), "paket": paket_xml(root), "jalur": jalur or [],
             "dari": dari, "judul": judul_layar(root), "ref": ref, "screenshot": screenshot, "tab_terpilih": None,
             "ketukan": 0}
        self.layar.append(L)
        return L, True

    def perlu_screenshot(self, xml):
        try:
            root = _root(xml)
        except ET.ParseError:
            return True
        return paket_xml(root) == self.p.pkg and self.cari_layar(root) is None

    def cocok(self, root, L):
        return root is not None and paket_xml(root) == L["paket"] and self.mirip_layar(L, tanda_layar(root)) >= 0.6

    # ---------- elemen yang bisa dijelajah di satu layar
    def kandidat(self, root, L):
        urut, induk = pohon_induk(root)
        W, H = ukuran_layar_xml(root)
        kotak_berlabel = {el.get("bounds") for el in urut if el.get("clickable") == "true" and _label(el)}
        calon = []
        for el in urut:
            b = _bounds(el.get("bounds"))
            kelas = el.get("class") or ""
            if not b or el.get("clickable") != "true" or el.get("enabled") == "false" or kelas == "android.widget.EditText":
                continue
            if adalah_penghalang(el, W, H):
                continue  # lapisan penutup dialog/lembar bawah: ditutup lewat Back atau tombol Tutup, bukan diketuk
            lb = _label(el)
            if not lb and el.get("bounds") in kotak_berlabel:
                continue  # pembungkus kembar tanpa label: tombol berlabelnya yang diketuk
            turunan = [_label(c) for c in el.iter() if c is not el and _label(c)]
            nama = lb or (turunan[0] if turunan else "")
            if lb:
                atr = "content-desc" if el.get("content-desc") == lb else "text"
                sama = [x for x in urut if x.get(atr) == lb and _bounds(x.get("bounds"))]
                if len(sama) > 1:  # label kembar (mis. judul halaman = nama tab): sempitkan dengan kelas elemennya,
                    sama = [x for x in sama if x.get("class") == kelas]  # supaya tetap ketemu di layar lain
                    k = sama.index(el) if el in sama else 0
                    target = {"label": lb, "kelas": kelas, **({"urutan": k} if len(sama) > 1 else {})}
                else:
                    target = {"label": lb}
            elif turunan:
                target = {"label_mengandung": turunan[0].split("\n")[0][:60]}
            else:
                target = {"koordinat": [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]}
            kunci_label = lb[:60] if lb else (f"[{kelas.split('.')[-1]}] {nama[:50]}" if nama else
                                              f"[{kelas.split('.')[-1]}]@{(b[0] + b[2]) // 120},{(b[1] + b[3]) // 120}")
            tetap = self.posisi_tetap.get((kunci_label, el.get("bounds")))
            kunci = tetap or f"{L['id']}|{kunci_label}" + (f"#{target['urutan']}" if target.get("urutan") else "")
            self.posisi_tetap.setdefault((kunci_label, el.get("bounds")), kunci)
            sc = _scroll_terdekat(el, induk)
            grup = (L["id"], sc.get("bounds") if sc is not None else "layar", kelas, b[0] // 30,
                    (b[2] - b[0]) // 30, (b[3] - b[1]) // 30)
            calon.append({"kunci": kunci, "nama": nama.replace("\n", " ")[:80], "kelas": kelas.split(".")[-1],
                          "target": target, "bounds": el.get("bounds"), "grup": grup,
                          "terpilih": el.get("selected") == "true", "risiko": jenis_risiko(nama)})
        ukuran = {}
        for c in calon:
            if c["grup"]:
                ukuran[c["grup"]] = ukuran.get(c["grup"], 0) + 1
        # Kisi seragam (mis. sel tanggal kalender): kolom-kolom berukuran sama yang labelnya berpola sama = satu daftar.
        # Kisi menu dengan label berbeda-beda tetap per kolom.
        kisi = {}
        for c in calon:
            g = c["grup"]
            if g and ukuran[g] >= 3:
                kisi.setdefault((g[0], g[1], g[2], g[4], g[5]), []).append(c)
        for kunci_kisi, anggota in kisi.items():
            if len({c["grup"][3] for c in anggota}) >= 3 and len({_norm_label(c["nama"]) for c in anggota}) <= 3:
                for c in anggota:
                    c["grup"] = ("kisi",) + kunci_kisi
        ukuran = {}
        for c in calon:
            if c["grup"]:
                ukuran[c["grup"]] = ukuran.get(c["grup"], 0) + 1
        baris = {}
        for c in calon:
            if c["grup"] and ukuran[c["grup"]] < 3:
                c["grup"] = None  # bukan daftar
            b = _bounds(c["bounds"])
            baris.setdefault((c["kelas"], b[1] // 20, (b[3] - b[1]) // 20), []).append(c)
        for anggota in baris.values():  # baris tab: >= 2 elemen sejajar, tepat satu yang terpilih
            if len(anggota) >= 2 and sum(1 for c in anggota if c["terpilih"]) == 1:
                for c in anggota:
                    c["tab"] = True
                    if c["terpilih"] and L.get("tab_terpilih") is None:
                        L["tab_terpilih"] = c["target"]
        for c in calon:
            self.status.setdefault(c["kunci"], {"layar": L["id"], "label": c["nama"] or None, "kelas": c["kelas"],
                                                "status": "belum"})
        return sorted(calon, key=lambda c: bool(c.get("tab")))  # isi layar dulu, tab terakhir

    # ---------- aksi
    def langkah_nav(self, s, aksi_catat="navigasi_otomatis"):
        n0 = len(self.p.langkah)
        ok = self.p.satu_langkah(0, {**s, "tanpa_pemulihan": True})
        for lk in self.p.langkah[n0:]:
            lk["aksi"] = aksi_catat
        return ok

    def tekan_kembali(self):
        p = self.p
        p.aksi_sekarang, p.rencana_ke = "kembali", None
        p.catat_langkah(f"jelajah_kembali_{self.ketukan:03d}", aksi=p.d.back, xml_sebelum=p.page_source_aman(),
                        catatan="penjelajah otomatis: kembali ke layar sebelumnya")
        p.langkah[-1]["aksi"] = "navigasi_otomatis"

    def pergi_ke(self, L):
        """Kembali ke layar L: mulai dari layar sekarang kalau ada di jalur L, selain itu mulai ulang aplikasi lalu
        putar ulang jalur ketukan dari layar awal."""
        self.pemulihan += 1
        if self.pemulihan > 80:
            return False
        root = self.baca()
        kini = self.cari_layar(root) if root is not None and paket_xml(root) == self.p.pkg else None
        ids = [lid for lid, _ in L["jalur"]]
        if kini is not None and kini["id"] in ids:
            sisa = L["jalur"][ids.index(kini["id"]):]
        else:
            self.p.mulai_ulang_app(0, f"penjelajah tersesat; kembali ke {L['id']}")
            self.p.langkah[-1]["aksi"] = "navigasi_otomatis"
            sisa = L["jalur"]
        for _, target in sisa:
            if self.habis():
                return False
            self.langkah_nav({"aksi": "tap", "nama": f"jelajah_menuju_{L['id']}", "target": target, "batas_cari_detik": 4,
                              "catatan": f"penjelajah otomatis: menuju {L['id']}"})
        return self.cocok(self.baca(), L)

    def pastikan_di(self, L, lewat=None):
        root = self.baca()
        if self.cocok(root, L):
            return True
        kini = self.cari_layar(root) if root is not None and paket_xml(root) == self.p.pkg else None
        if kini is not None and kini["id"] in [lid for lid, _ in L["jalur"]]:
            return self.pergi_ke(L)  # lembar bawah/dialog tertutup sendiri: buka lagi dari layar induknya
        if lewat and lewat.get("tab") and L.get("tab_terpilih") and not self.p.aplikasi_tidak_di_depan():
            self.langkah_nav({"aksi": "tap", "nama": "jelajah_kembali_tab", "target": L["tab_terpilih"], "batas_cari_detik": 3,
                              "catatan": "penjelajah otomatis: kembali lewat tab semula"})
            if self.cocok(self.baca(), L):
                return True
        if not self.p.aplikasi_tidak_di_depan():
            self.tekan_kembali()
            if self.cocok(self.baca(), L):
                return True
        if self.p.aplikasi_tidak_di_depan():
            self.p.catat_kembali_ke_app(0)
            self.p.langkah[-1]["aksi"] = "navigasi_otomatis"
            if self.cocok(self.baca(), L):
                return True
        return self.pergi_ke(L)

    def ketuk(self, c, L):
        p = self.p
        self.ketukan += 1
        L["ketukan"] = L.get("ketukan", 0) + 1
        if self.ketukan % 25 == 0:
            sisa = sum(1 for s in self.status.values() if s["status"] == "belum")
            log(f"  penjelajah: {self.ketukan} ketukan, {len(self.layar)} layar, {sisa} elemen menunggu, "
                f"{(time.perf_counter() - self.t0) / 60:.0f} menit")
        n0 = len(p.langkah)
        p.satu_langkah(0, {"aksi": "tap", "nama": f"jelajah_{self.ketukan:03d}_{_slug(c['nama'] or c['kelas'], 40)}",
                           "target": c["target"], "batas_cari_detik": 2, "tanpa_pemulihan": True,
                           "catatan": f"penjelajah otomatis: {L['id']} -> '{c['nama'] or c['kelas']}'"})
        lk = p.langkah[n0] if len(p.langkah) > n0 else None
        if lk is None:
            return "gagal", None, None
        lk["jelajah"] = {"layar": L["id"], "kunci": c["kunci"]}
        if not lk["berhasil"]:
            if lk.get("tak_terjangkau"):
                return "tak_terjangkau", lk, None
            if lk.get("diblokir"):
                return "diblokir", lk, None
            return ("crash" if lk.get("crash") else "keluar_aplikasi" if lk.get("keluar_aplikasi") else "gagal"), lk, None
        if lk.get("keluar_aplikasi"):
            return "aplikasi_lain", lk, None
        if lk.get("tanpa_perubahan"):
            return "tanpa_perubahan", lk, None
        root = _root(p.xml_terakhir) if p.xml_terakhir else None
        if root is None or self.cocok(root, L):
            return "berubah", lk, None
        L2, baru = self.kenali(root, jalur=L["jalur"] + [(L["id"], c["target"])], dari=L["id"], ref=lk["no"],
                               screenshot=lk.get("screenshot"))
        return ("layar_baru" if baru else "layar_lama"), lk, L2

    def gulir(self, arah):
        p = self.p
        p.aksi_sekarang, p.rencana_ke = "gulir", None
        p.catat_langkah(f"jelajah_gulir_{arah}", aksi=lambda: p.gulir_ke(None, None, {}, arah=arah, porsi=0.6), stabil=False,
                        catatan="penjelajah otomatis: menggulir untuk menemukan elemen lain")
        p.langkah[-1]["aksi"] = "navigasi_otomatis"

    # ---------- penjelajahan
    def lewati_sisa(self, L, alasan):
        n = 0
        for s in self.status.values():
            if s["layar"] == L["id"] and s["status"] == "belum":
                s.update(status="dilewati", alasan=alasan)
                n += 1
        log(f"  penjelajah: {L['id']} ({L['judul']}): {n} elemen sisanya dilewati ({alasan})")

    def jelajahi(self, L, kedalaman=0):
        digulir, buntu, gagal_beruntun = 0, 0, 0
        while not self.habis():
            root = self.baca()
            if not self.cocok(root, L):
                if not self.pastikan_di(L):
                    log(f"  penjelajah: {L['id']} tidak bisa dicapai lagi; sisa elemennya dilewati")
                    return
                continue
            calon = self.kandidat(root, L)
            belum = [c for c in calon if self.status[c["kunci"]]["status"] == "belum"]
            if not belum:
                if any(el.get("scrollable") == "true" for el in root.iter()) and digulir < 6 and buntu < 1:
                    sebelum = {c["kunci"] for c in calon}
                    self.gulir("bawah")
                    digulir += 1
                    root2 = self.baca()
                    self.tambah_varian(L, root2)  # digulir sendiri: tetap layar yang sama walau isinya berganti
                    if root2 is None or {c["kunci"] for c in self.kandidat(root2, L)} <= sebelum:
                        buntu += 1
                    continue
                break
            c = belum[0]
            st = self.status[c["kunci"]]
            if c["risiko"]:
                st.update(status="diblokir", alasan=f"mengubah data/berisiko ({c['risiko']}); diuji lewat CRUD Perancang")
                continue
            g = self.grup.setdefault(c["grup"], {"n": 0, "tujuan": set()}) if c["grup"] else None
            if g is not None and g["n"] >= self.maks_per_grup and len(g["tujuan"]) <= 1:
                st.update(status="diwakili", alasan=f"item sejenis; {g['n']} contoh sudah dijelajah dan menuju layar yang sama")
                continue
            if L.get("ketukan", 0) >= self.maks_per_layar:
                self.lewati_sisa(L, f"batas {self.maks_per_layar} ketukan per layar tercapai; dilanjutkan Perancang")
                break
            hasil, lk, L2 = self.ketuk(c, L)
            if g is not None and hasil != "tak_terjangkau":
                g["n"] += 1  # tetap di layar yang sama (memilih, menandai) = satu jenis hasil, berubah atau tidak
                g["tujuan"].add(L2["id"] if L2 else "layar_sama" if hasil in ("berubah", "tanpa_perubahan") else hasil)
            st.update(status="dijelajah" if hasil not in ("tak_terjangkau", "diblokir", "gagal") else hasil,
                      hasil=hasil + (f" {L2['id']}" if L2 else ""), ref=lk["no"] if lk else None)
            if lk is not None and lk.get("galat") and hasil in ("tak_terjangkau", "diblokir", "gagal"):
                st["alasan"] = lk["galat"]
            gagal_beruntun = gagal_beruntun + 1 if hasil == "tak_terjangkau" else 0
            if gagal_beruntun >= self.maks_gagal_beruntun:
                self.lewati_sisa(L, f"{gagal_beruntun} elemen berturut-turut tidak bisa diketuk; kemungkinan tertutup "
                                    "lapisan yang tidak dikenali")
                break
            if hasil in ("tanpa_perubahan", "berubah", "tak_terjangkau", "diblokir"):
                continue  # tidak berpindah layar: tidak perlu Back
            if hasil == "layar_baru" and L2 is not None and kedalaman < self.maks_kedalaman:
                log(f"  penjelajah: layar baru {L2['id']} ({L2['judul']}) dari {L['id']}")
                self.jelajahi(L2, kedalaman + 1)
            if not self.pastikan_di(L, c):
                log(f"  penjelajah: gagal kembali ke {L['id']}")
                return
        for _ in range(digulir):
            if self.cocok(self.baca(), L):
                self.gulir("atas")

    def jalan(self):
        p = self.p
        p.aksi_sekarang, p.rencana_ke = "tangkap", None
        p.catat_langkah("jelajah_layar_awal", stabil=False, catatan="penjelajah otomatis: layar awal")
        root = _root(p.xml_terakhir) if p.xml_terakhir else None
        if root is None or paket_xml(root) != p.pkg:
            raise Gagal("penjelajah: aplikasi tidak tampil di layar awal")
        akar, _ = self.kenali(root, ref=p.langkah[-1]["no"], screenshot=p.langkah[-1].get("screenshot"))
        self.jelajahi(akar)

    def hasil(self):
        app = [L for L in self.layar if L["paket"] == self.p.pkg]
        id_app = {L["id"] for L in app}
        elemen = [e for e in self.status.values() if e["layar"] in id_app]
        hitung = {}
        for e in elemen:
            hitung[e["status"]] = hitung.get(e["status"], 0) + 1
        return {
            "layar": [{"id": L["id"], "judul": L["judul"], "dari": L["dari"], "ref": L["ref"], "screenshot": L["screenshot"],
                       "jalur": [t.get("label") or t.get("label_mengandung") or str(t.get("koordinat")) for _, t in L["jalur"]]}
                      for L in app],
            "elemen": elemen,
            "statistik": {"layar": len(app), "elemen": len(elemen), "ketukan": self.ketukan,
                          "pemulihan_jalur": self.pemulihan, "durasi_detik": round(time.perf_counter() - self.t0),
                          "habis_anggaran": self.habis(), **hitung},
        }


def jalankan_jelajah(rencana, cfg, rekam=False, folder=None):
    """Run penjelajah otomatis: buka aplikasi (+ login), lalu jelajahi semua elemen. Hasilnya berformat sama dengan
    run rencana (ringkasan.json) ditambah jelajah.json (layar, status tiap elemen, statistik)."""
    kode_app = rencana["aplikasi"]
    if kode_app not in cfg.get("aplikasi", {}):
        raise Gagal(f"aplikasi '{kode_app}' tidak ada di akun.json")
    peran = rencana.get("peran")
    p, driver, info, folder = buka_pelaksana(kode_app, cfg, folder, False, rencana.get("judul", "jelajah_otomatis"))
    (folder / "rencana.json").write_text(json.dumps(rencana, ensure_ascii=False, indent=2), encoding="utf-8")
    p.peran_aktif = peran
    p.beri_izin = rencana.get("beri_izin_otomatis", p.beri_izin)
    p.izin_ulang("awal uji")
    if p.beri_izin:
        p.aktifkan_autostart()
    mulai, t0 = datetime.now().isoformat(timespec="seconds"), time.perf_counter()
    j = Penjelajah(p, int(rencana.get("batas_ketukan", 300)), float(rencana.get("batas_menit", 40)),
                   maks_per_grup=int(rencana.get("maks_per_grup", 2)))
    p.saring_screenshot = None  # langkah pembuka selalu dengan screenshot
    berhasil, video, data = False, None, None
    p.mulai_log()
    sedang_rekam = rekam and p.mulai_rekam()
    p.t_acuan = time.perf_counter()
    try:
        pembuka = [{"aksi": "buka_aplikasi", "nama": "buka_aplikasi"}]
        if peran:
            pembuka.append({"aksi": "login", "nama": f"login_{peran}", "peran": peran})
        if all(p.satu_langkah(i, s) for i, s in enumerate(pembuka, 1)):
            p.saring_screenshot = j.perlu_screenshot
            j.jalan()
            berhasil = True
    finally:
        if sedang_rekam:
            video = p.selesai_rekam()
        p.kembalikan_pengaturan_hp()
        data = j.hasil()
        (folder / "jelajah.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tulis_ringkasan(p, rencana, info, mulai, t0, berhasil, video, False, tambahan={"jelajah": data["statistik"]})
        try:
            driver.quit()
        except Exception:
            pass
    h = hasil_run(p, berhasil, video, folder)
    h["status"] = "selesai" if berhasil else "gagal_langkah"
    h["berhasil"] = berhasil
    h["jelajah"] = data["statistik"]
    log(f"Penjelajah: {data['statistik']}")
    return h


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


def perintah_siapkan(args, cfg):
    """Persiapan HP sekali jalan: beri semua izin lewat adb + nyalakan Autostart (Xiaomi) untuk tiap aplikasi."""
    kode_app = [args.app] if args.app else list(cfg.get("aplikasi", {}))
    for kode in kode_app:
        if kode not in cfg.get("aplikasi", {}):
            raise Gagal(f"aplikasi '{kode}' tidak ada di akun.json")
    pastikan_hp(cfg)
    log(f"HP: {adb_baca('shell', 'getprop', 'ro.product.manufacturer').strip()} "
        f"{adb_baca('shell', 'getprop', 'ro.product.model').strip()} ({SERIAL_AKTIF})")
    hasil, driver = {}, None
    try:
        for kode in kode_app:
            app = cfg["aplikasi"][kode]
            if not aplikasi_terpasang(app["package"]):
                log(f"- {kode}: {app['package']} tidak terpasang di HP, dilewati")
                hasil[kode] = {"status": "tidak_terpasang"}
                continue
            info = info_perangkat(app["package"])
            log(f"- {kode} ({app['package']})")
            izin = beri_izin_adb(app["package"])
            if driver is None:
                log(f"Menyambung ke Appium di {APPIUM_URL} ...")
                driver = sambung_appium()
            (FOLDER_DASAR / "persiapan").mkdir(parents=True, exist_ok=True)
            p = Pelaksana(driver, cfg, kode, FOLDER_DASAR / "persiapan", info)
            gagal_dulu = (baca_autostart().get(app["package"]) or {}).get("status") == "gagal"
            hasil[kode] = {"status": "ok", "izin": izin, "autostart": p.aktifkan_autostart(paksa=args.paksa or gagal_dulu)}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return {"status": "selesai", "aplikasi": hasil}


SKEMA = """FORMAT RENCANA UJI (JSON) UNTUK agen_uji.py

{
  "aplikasi": "<kode aplikasi>",
  "judul": "nama_singkat_tanpa_spasi",
  "tujuan": "apa yang ingin dinilai dari uji ini",
  "peran": "(opsional) peran aktif kalau rencana tidak diawali login",
  "tugas": ["(opsional) id tugas dari DAFTAR UJI MENDALAM yang dikerjakan rencana ini"],
  "langkah": [ ... maksimal 40 langkah ... ],
  "pemulihan": [ ... (wajib kalau ada "ubah_data": "buat"/"ubah") langkah pengembali data, maks. 25 ... ]
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
- {"aksi":"pastikan_ada","nama":"...","target":{...}}        pemeriksaan: elemen harus ada (dicari sambil menggulir)
- {"aksi":"pastikan_tidak_ada","nama":"...","target":{...}}  pemeriksaan: elemen tidak boleh ada (bukti data terhapus)
- {"aksi":"atur_skala_font","nama":"...","skala":1.3}        ukuran huruf sistem 0.85-2.0 (uji teks besar)
- {"aksi":"atur_tema","nama":"...","tema":"gelap|terang"}    tema sistem (uji mode gelap)
- {"aksi":"atur_jaringan","nama":"...","status":"mati|hidup"} mode pesawat (uji tanpa internet)
  Skala font, tema, dan jaringan dikembalikan otomatis di akhir uji.

TARGET ELEMEN (utamakan "label" persis dari daftar elemen layar)
  {"label":"Teks persis"}            {"label_mengandung":"potongan teks"}
  {"hint":"placeholder kolom"}       {"id":"resource-id"}
  {"kelas":"EditText","urutan":0}    "kelas"/"urutan" boleh digabung dengan "label"
  {"koordinat":[x,y]}                hanya untuk tap, pilihan terakhir (pakai "pusat" dari daftar elemen)
Pelaksana hanya mengetuk bagian elemen yang terlihat dan menggulir sendiri kalau elemen tertutup bilah navigasi.

OPSI PER LANGKAH
  "tunggu_muncul": {target}    elemen STATIS yang pasti muncul setelah aksi (judul layar/label menu), untuk mengukur
                               waktu respons. JANGAN pakai teks yang berisi angka/data yang bisa berubah.
  "tunggu_hilang": {target}    elemen yang harus hilang setelah aksi
  "batas_detik": 15            batas waktu tunggu
  "berhenti_jika_gagal": true  hentikan rencana kalau langkah ini gagal (bawaan: pelaksana memulihkan keadaan
                               lalu lanjut ke langkah berikutnya)
  "ubah_data": "buat|ubah|hapus|status"  WAJIB untuk tap yang menyimpan/mengirim/menghapus/menyetujui (hanya kalau
                               sesi mengizinkan CRUD); "biarkan" = sakelar tidak dikembalikan otomatis
  "objek": "..."               nama data yang diubah, mis. "pengajuan kas kecil"
  "paksa_login_ulang": true    (khusus login) login ulang walau sesi peran yang sama masih aktif
  "catatan": "..."             alasan langkah ini, untuk evaluator
Tingkat rencana: "beri_izin_otomatis": false  izin TIDAK diberikan lewat adb di awal (untuk menilai alur izin).

NILAI KHUSUS DI "teks" DAN TARGET
  {penanda}                    diganti kode unik run ini (mis. UJI-1112-153012). Tulis di kolom teks utama setiap data
                               yang dibuat, lalu cari/hapus data itu lewat {"label_mengandung": "{penanda}"}.
  {nilai_awal:nama_langkah}    isi kolom SEBELUM langkah ketik bernama itu mengubahnya (untuk mengembalikan isian).

ATURAN
1. Pakai label persis seperti di daftar elemen layar terbaru; jangan menebak elemen yang belum terlihat.
2. Kalau label berisi baris baru, pakai "label_mengandung" dengan potongan satu baris.
3. Jangan pernah menulis kata sandi atau data akun; untuk login pakai aksi "login".
4. Selalu diblokir: pembayaran, WhatsApp/telepon/SMS/email, hapus/nonaktifkan akun, ganti kata sandi dan verifikasi
   2 langkah akun uji. Tanpa izin CRUD, semua tombol simpan/kirim/hapus/setujui/keluar juga diblokir.
5. Hapus/ubah status (hapus, setujui, tolak, akui, selesaikan, abaikan, batalkan, ...) hanya pada data buatan uji:
   baris/halaman yang diketuk harus memuat penanda data uji.
6. Setelah membuka lembar bawah/dialog, tutup dulu (kembali, atau tombol tutup/batal) sebelum mengetuk elemen di
   layar di bawahnya.
7. Beri "nama" yang deskriptif: huruf kecil dan garis bawah.
8. Keluarkan HANYA objek JSON rencana, tanpa teks lain.

CONTOH CRUD DENGAN PEMULIHAN
{
  "aplikasi": "santamaria",
  "judul": "crud_pengajuan_kas_kecil",
  "tujuan": "Validasi form pengajuan kas kecil lalu buat, periksa, dan hapus data uji",
  "tugas": ["form:L8"],
  "langkah": [
    {"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
    {"aksi": "login", "nama": "login_owner", "peran": "owner"},
    {"aksi": "tap", "nama": "buka_kas_kecil", "target": {"label_mengandung": "Pengajuan Kas Kecil"}},
    {"aksi": "tap", "nama": "kirim_kosong", "target": {"label": "Ajukan"}, "ubah_data": "buat", "objek": "pengajuan kas kecil",
     "catatan": "form kosong harus ditolak dengan pesan yang jelas"},
    {"aksi": "ketik", "nama": "isi_keperluan", "target": {"hint": "Keperluan"}, "teks": "Uji otomatis {penanda}"},
    {"aksi": "ketik", "nama": "isi_nominal", "target": {"hint": "Nominal"}, "teks": "-5000"},
    {"aksi": "tap", "nama": "ajukan_nominal_negatif", "target": {"label": "Ajukan"}, "ubah_data": "buat",
     "objek": "pengajuan kas kecil"},
    {"aksi": "ketik", "nama": "isi_nominal_benar", "target": {"hint": "Nominal"}, "teks": "15000"},
    {"aksi": "tap", "nama": "ajukan", "target": {"label": "Ajukan"}, "ubah_data": "buat", "objek": "pengajuan kas kecil"},
    {"aksi": "pastikan_ada", "nama": "data_uji_tampil", "target": {"label_mengandung": "{penanda}"}}
  ],
  "pemulihan": [
    {"aksi": "tap", "nama": "buka_data_uji", "target": {"label_mengandung": "{penanda}"}},
    {"aksi": "tap", "nama": "hapus_data_uji", "target": {"label": "Hapus"}, "ubah_data": "hapus", "objek": "pengajuan kas kecil"},
    {"aksi": "pastikan_tidak_ada", "nama": "data_uji_hilang", "target": {"label_mengandung": "{penanda}"}}
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

    pp = sub.add_parser("siapkan", help="beri semua izin + nyalakan Autostart (Xiaomi) untuk aplikasi uji")
    pp.add_argument("--app", help="hanya aplikasi ini (bawaan: semua di akun.json)")
    pp.add_argument("--paksa", action="store_true", help="periksa ulang Autostart walau tercatat sudah aktif")

    pz = sub.add_parser("jelajahi", help="penjelajah otomatis: ketuk semua elemen yang bisa dijangkau (tanpa LLM, "
                                         "tanpa mengubah data)")
    pz.add_argument("--app", required=True)
    pz.add_argument("--peran", help="login sebagai peran ini dulu")
    pz.add_argument("--batas-ketukan", type=int, default=300)
    pz.add_argument("--batas-menit", type=float, default=40)
    pz.add_argument("--rekam", action="store_true", help="rekam layar (3 menit pertama)")

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
        if args.perintah == "siapkan":
            print(json.dumps(perintah_siapkan(args, cfg), ensure_ascii=False))
            return 0
        if args.perintah == "jelajahi":
            app = cfg.get("aplikasi", {}).get(args.app)
            if app is None:
                raise Gagal(f"aplikasi '{args.app}' tidak ada di akun.json")
            if args.peran and args.peran not in app.get("akun", {}):
                raise Gagal(f"peran '{args.peran}' tidak ada untuk {args.app}")
            hasil = jalankan_jelajah({"jenis": "jelajah_otomatis", "aplikasi": args.app, "peran": args.peran,
                                      "judul": f"jelajah_{args.peran or 'tanpa_login'}", "batas_ketukan": args.batas_ketukan,
                                      "batas_menit": args.batas_menit}, cfg, rekam=args.rekam)
            print(json.dumps(hasil, ensure_ascii=False))
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
