#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orkestrator.py - Dua agen LLM + loop uji ulang untuk evaluasi UI/UX aplikasi Android.

  LLM Perancang : melihat layar (screenshot bernomor + daftar elemen) lalu menyusun rencana uji (JSON).
  Pelaksana     : agen_uji.py menjalankan rencana di HP lewat Appium (tanpa LLM, memegang akun uji).
  LLM Evaluator : menilai waktu, metrik aksesibilitas, screenshot, dan frame rekaman. Kalau keyakinannya
                  sudah >= ambang, laporan dibuat. Kalau belum, ia menulis permintaan uji ulang yang spesifik
                  dan LLM Perancang menyusun rencana baru (maksimal N putaran).

Perintah (stdout = satu baris JSON untuk n8n, progres ke stderr):
  python orkestrator.py cek-llm                         tes koneksi 9router/LLM, JSON, dan gambar
  python orkestrator.py daftar-skenario                 isi skenario.json
  python orkestrator.py siklus --app santamaria --peran owner [--tujuan "..."]   satu sesi penuh
  python orkestrator.py siklus --skenario 0             satu sesi dari skenario.json
  python orkestrator.py semua                           semua skenario + ringkasan gabungan
Langkah terpisah (dipakai workflow n8n):
  mulai --skenario N | rancang --sesi F | jalankan --sesi F | evaluasi --sesi F | laporan --sesi F
  ringkasan --sesi F1 F2 ...

Hasil: ~/.n8n-files/uji-ui/sesi_<waktu>_<aplikasi>_<peran>/ (laporan.html, laporan.md, laporan.json).
Data akun (email, HP, kode, kata sandi) tidak pernah dikirim ke LLM; teks yang memuatnya disensor.
"""

import argparse
import base64
import html
import io
import json
import os
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

FOLDER_SKRIP = Path(__file__).resolve().parent
sys.path.insert(0, str(FOLDER_SKRIP))
import agen_uji as eks  # noqa: E402

FILE_LLM = Path(os.environ.get("UJI_UI_LLM") or FOLDER_SKRIP / "llm.json")
FILE_SKENARIO = Path(os.environ.get("UJI_UI_SKENARIO") or FOLDER_SKRIP / "skenario.json")
FILE_PROMPT_PERANCANG = FOLDER_SKRIP / "prompt_perancang.md"
FILE_PROMPT_EVALUATOR = FOLDER_SKRIP / "prompt_evaluator.md"

LLM_BAWAAN = {
    "base_url": "http://localhost:20128/v1",
    "api_key": "",
    "model_perancang": "gemini/gemini-3.6-flash",
    "model_evaluator": "gemini/gemini-3.6-flash",
    "model_cadangan": "gemini/gemini-3.5-flash-lite",
    "reasoning_effort_perancang": "low",
    "reasoning_effort_evaluator": "medium",
    "temperature": 0.2,
    "max_tokens": None,
    "response_format": None,      # "json_object" untuk vLLM/OpenAI; 9router->Gemini mengabaikannya
    "timeout_detik": 240,
    "maks_coba": 4,
    "jeda_coba_detik": 5,
    "kirim_gambar": True,
    "lebar_gambar_perancang": 540,
    "lebar_gambar_evaluator": 720,
    "maks_gambar_perancang": 6,
    "maks_gambar_evaluator": 20,
}
PENGATURAN_BAWAAN = {"ambang_yakin": 0.75, "maks_putaran": 3, "rekam": True, "boleh_ubah_data": False,
                     "beri_izin_otomatis": True, "jelajah_menyeluruh": False,
                     # mode "overtesting": putaran 1 = penjelajah otomatis (semua elemen), lalu LLM menguji mendalam
                     # (CRUD + pemulihan, form, pencarian, teks besar, tema gelap, tanpa internet) sampai semua tercakup
                     "mode": "evaluasi", "jelajah_otomatis": False, "batas_ketukan_jelajah": 400,
                     "batas_menit_jelajah": 45, "batas_menit_sesi": 0,
                     "perintah_cadangan": "", "perintah_pulihkan": ""}
TUJUAN_BAWAAN = (
    "Evaluasi UI/UX alur utama peran ini: (1) aksesibilitas - label elemen untuk pembaca layar, ukuran area "
    "sentuh, kontras, dukungan teks besar; (2) waktu respons - startup, login, perpindahan layar, ada tidaknya "
    "indikator loading; (3) kerumitan UI - kepadatan elemen, kedalaman navigasi, kejelasan hierarki; "
    "(4) konsistensi dan umpan balik - pola navigasi, istilah, pesan. Jelajahi menu utama tanpa mengubah data."
)
TINGKAT = ("kritis", "tinggi", "sedang", "rendah")
KATEGORI = ("crash", "bug_fungsional", "data_janggal", "penanganan_error", "cacat_visual", "estetika", "keterbacaan",
            "aksesibilitas", "waktu_respons", "kerumitan_ui", "konsistensi", "umpan_balik", "navigasi", "lainnya")
KELOMPOK_KATEGORI = (("Bug, crash, dan data janggal", ("crash", "bug_fungsional", "data_janggal", "penanganan_error")),
                     ("Cacat visual dan estetika", ("cacat_visual", "estetika", "keterbacaan")),
                     ("Aksesibilitas", ("aksesibilitas",)),
                     ("Performa", ("waktu_respons",)),
                     ("Navigasi, konsistensi, dan umpan balik", ("navigasi", "konsistensi", "kerumitan_ui", "umpan_balik")),
                     ("Lainnya", ("lainnya",)))
BATAS_JEDA_MS = 1000         # Nielsen: lebih dari 1 detik, alur pikir pengguna terputus
BATAS_PERHATIAN_MS = 10000   # Nielsen: batas perhatian 10 detik
VITALS_STARTUP_MS = {"COLD": 5000, "WARM": 2000, "HOT": 1500}   # Android vitals: dianggap berlebihan
AKSI_BERWAKTU = ("tap", "kembali", "gulir", "buka_aplikasi", "login", "atur_skala_font", "atur_tema")
AKSI_DINILAI_WAKTU = ("tap", "kembali", "buka_aplikasi", "login", "atur_skala_font", "atur_tema")  # gulir = durasi gestur
KODE_MASALAH = {"interaktif_tanpa_label": "A", "target_sentuh_di_bawah_48dp": "T",
                "input_label_hanya_dari_hint": "H", "tombol_tidak_bisa_diklik": "B", "duplikat_klik": "D",
                "teks_janggal": "E", "elemen_bertumpuk": "O"}
WARNA_MASALAH = {"A": (220, 38, 38), "T": (234, 88, 12), "H": (202, 138, 4), "B": (147, 51, 234),
                 "K": (37, 99, 235), "D": (100, 116, 139), "E": (190, 24, 93), "O": (8, 145, 178), "U": (22, 101, 52)}
WARNA_NOMOR = [(220, 38, 38), (37, 99, 235), (22, 163, 74), (147, 51, 234), (234, 88, 12), (8, 145, 178),
               (190, 24, 93), (101, 163, 13)]
CATATAN_FRAME = ("Frame rekaman diambil pada 30% dan 70% waktu tunggu langkah lambat. Awal rekaman di HP bisa "
                 "terlambat sekitar 0,3-1 detik dari awal pengukuran, jadi posisi frame adalah perkiraan.")


class GagalOrkestrasi(Exception):
    """Kesalahan yang menghentikan perintah (state sesi, LLM, atau rencana)."""


class LLMSementara(Exception):
    def __init__(self, pesan, tunggu=None):
        super().__init__(pesan)
        self.tunggu = tunggu


def log(*bagian):
    print(*bagian, file=sys.stderr, flush=True)


def sekarang():
    return datetime.now().isoformat(timespec="seconds")


def baca_json(path, bawaan=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return bawaan
    except json.JSONDecodeError as e:
        raise GagalOrkestrasi(f"{path} bukan JSON yang valid: {e}") from e


def tulis_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def baca_teks(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise GagalOrkestrasi(f"file tidak ditemukan: {path} (simpan di folder yang sama dengan orkestrator.py)") from e


# ====================================================================== konfigurasi
def muat_llm():
    conf = dict(LLM_BAWAAN)
    if FILE_LLM.exists():
        conf.update({k: v for k, v in (baca_json(FILE_LLM) or {}).items() if not k.startswith("_")})
    elif not os.environ.get("UJI_LLM_KEY"):
        log(f"catatan: {FILE_LLM.name} tidak ada; salin llm.contoh.json menjadi llm.json dan isi api_key")
    for env, kunci in (("UJI_LLM_URL", "base_url"), ("UJI_LLM_KEY", "api_key")):
        if os.environ.get(env):
            conf[kunci] = os.environ[env]
    if os.environ.get("UJI_LLM_MODEL"):
        conf["model_perancang"] = conf["model_evaluator"] = os.environ["UJI_LLM_MODEL"]
    conf["base_url"] = str(conf["base_url"]).rstrip("/")
    return conf


def muat_skenario():
    data = baca_json(FILE_SKENARIO, {}) or {}
    return {**PENGATURAN_BAWAAN, **data.get("pengaturan", {})}, data.get("skenario", [])


class Sensor:
    """Ganti nilai akun (email, HP, kode, kata sandi) dengan [DATA_AKUN] sebelum teks dikirim ke LLM."""

    def __init__(self, cfg):
        self.nilai = sorted(eks.nilai_rahasia(cfg), key=len, reverse=True)

    def __call__(self, teks):
        for v in self.nilai:
            if v in teks:
                teks = teks.replace(v, "[DATA_AKUN]")
        return teks


# ====================================================================== klien LLM (OpenAI-compatible)
class KlienLLM:
    def __init__(self, conf, sensor):
        self.c = conf
        self.sensor = sensor
        host = urllib.parse.urlparse(conf["base_url"]).hostname or ""
        handler = [urllib.request.ProxyHandler({})] if host in ("localhost", "127.0.0.1", "::1") else []
        self.opener = urllib.request.build_opener(*handler)

    def _minta(self, path, body=None, timeout=None):
        req = urllib.request.Request(
            self.c["base_url"] + path, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.c.get('api_key') or 'kosong'}"})
        try:
            with self.opener.open(req, timeout=timeout or float(self.c["timeout_detik"])) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            if e.code in (408, 409, 425, 429, 500, 502, 503, 504, 529):
                raise LLMSementara(f"HTTP {e.code}: {detail}", e.headers.get("Retry-After")) from e
            petunjuk = {401: " (API key salah/kosong? cek api_key di llm.json)", 403: " (API key tidak punya akses?)",
                        404: " (base_url atau nama model salah?)"}.get(e.code, "")
            raise GagalOrkestrasi(f"LLM menolak permintaan: HTTP {e.code}{petunjuk}: {detail}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                raise GagalOrkestrasi(f"tidak bisa menyambung ke {self.c['base_url']} - apakah 9router sedang jalan?") from e
            raise LLMSementara(f"koneksi gagal: {e.reason}") from e
        except (socket.timeout, TimeoutError, ConnectionError) as e:
            raise LLMSementara(f"koneksi/timeout: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMSementara(f"jawaban server bukan JSON: {e}") from e

    def daftar_model(self):
        data = self._minta("/models", timeout=20)
        return [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]

    def _sensor_pesan(self, pesan):
        hasil = []
        for p in pesan:
            isi = p["content"]
            if isinstance(isi, str):
                isi = self.sensor(isi)
            else:
                isi = [{**b, "text": self.sensor(b["text"])} if b.get("type") == "text" else b for b in isi]
            hasil.append({**p, "content": isi})
        return hasil

    def chat(self, pesan, agen, label, folder_log=None):
        """Kirim percakapan; kembalikan (teks jawaban, model). Coba ulang otomatis + model cadangan."""
        pesan = self._sensor_pesan(pesan)
        utama = self.c[f"model_{agen}"]
        daftar = [utama] + ([self.c["model_cadangan"]] if self.c.get("model_cadangan") not in (None, "", utama) else [])
        galat = None
        for model in daftar:
            for coba in range(1, int(self.c["maks_coba"]) + 1):
                body = {"model": model, "messages": pesan, "stream": False}
                if self.c.get("temperature") is not None:
                    body["temperature"] = self.c["temperature"]
                if self.c.get(f"reasoning_effort_{agen}"):
                    body["reasoning_effort"] = self.c[f"reasoning_effort_{agen}"]
                if self.c.get("max_tokens"):
                    body["max_tokens"] = int(self.c["max_tokens"])
                if self.c.get("response_format"):
                    body["response_format"] = {"type": self.c["response_format"]}
                t0 = time.perf_counter()
                try:
                    data = self._minta("/chat/completions", body)
                    if data.get("error"):
                        raise LLMSementara(f"error dari router: {str(data['error'])[:300]}")
                    pilihan = (data.get("choices") or [{}])[0]
                    teks = (pilihan.get("message") or {}).get("content")
                    if isinstance(teks, list):
                        teks = "".join(b.get("text", "") for b in teks if isinstance(b, dict))
                    if not teks or not teks.strip():
                        raise LLMSementara(f"jawaban kosong (finish_reason={pilihan.get('finish_reason')})")
                    durasi = round((time.perf_counter() - t0) * 1000)
                    log(f"  LLM {agen} ({model}) menjawab dalam {durasi / 1000:.1f} s")
                    if folder_log:
                        tulis_json(Path(folder_log) / f"llm_{label}.json", {
                            "model": model, "waktu": sekarang(), "durasi_ms": durasi, "usage": data.get("usage"),
                            "pesan": _pesan_untuk_log(pesan), "jawaban": teks})
                    return teks, model
                except LLMSementara as e:
                    galat = e
                    tunggu = float(self.c["jeda_coba_detik"]) * 2 ** (coba - 1)
                    try:
                        tunggu = max(tunggu, float(e.tunggu)) if e.tunggu else tunggu
                    except ValueError:
                        pass
                    tunggu = min(tunggu, 90)
                    log(f"  LLM {agen} ({model}) gagal (coba {coba}): {str(e)[:200]}; ulang dalam {tunggu:.0f} s")
                    if coba < int(self.c["maks_coba"]):
                        time.sleep(tunggu)
            log(f"  pindah ke model berikutnya setelah {self.c['maks_coba']} kali gagal")
        raise GagalOrkestrasi(f"LLM {agen} tidak memberi jawaban: {galat}")


def _pesan_untuk_log(pesan):
    hasil = []
    for p in pesan:
        isi = p["content"]
        if not isinstance(isi, str):
            isi = [b if b.get("type") == "text" else
                   {"type": b.get("type"), "keterangan": f"[gambar {len(json.dumps(b)) // 1024} KB]"} for b in isi]
        hasil.append({"role": p["role"], "content": isi})
    return hasil


def ambil_json(teks):
    """Ambil objek JSON pertama dari jawaban LLM (toleran terhadap ```json ... ``` dan teks tambahan)."""
    kandidat = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", teks, re.S)] + [teks]
    dec = json.JSONDecoder()
    for k in kandidat:
        for m in re.finditer(r"\{", k):
            try:
                obj, _ = dec.raw_decode(k[m.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("jawaban bukan objek JSON yang valid")


# ====================================================================== gambar
def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except ImportError:
        return None


def _font(ImageFont, ukuran):
    try:
        return ImageFont.load_default(size=ukuran)
    except TypeError:
        return ImageFont.load_default()


def gambar_url(path, lebar, kotak=None, kualitas=72, tutup=None, lebar_asal=None):
    """Screenshot -> data URL JPEG yang diperkecil.
    kotak = [(teks, [x1,y1,x2,y2], warna_rgb), ...] untuk anotasi; tutup = [[x1,y1,x2,y2], ...] area yang
    disensor (kolom berisi data akun). Koordinat dalam piksel HP; lebar_asal = lebar layar HP kalau gambar
    (misalnya frame video) beresolusi lain."""
    pil = _pil()
    if pil is None:
        return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()
    Image, ImageDraw, ImageFont = pil
    with Image.open(path) as asli:
        im = asli.convert("RGB")
    if tutup:
        f = im.width / lebar_asal if lebar_asal else 1.0
        g = ImageDraw.Draw(im)
        font = _font(ImageFont, max(11, round(30 * f)))
        for b in tutup:
            x1, y1, x2, y2 = (round(v * f) for v in b)
            g.rectangle([x1, y1, x2, y2], fill=(226, 226, 226), outline=(160, 160, 160))
            g.text((x1 + 8, y1 + 6), "[data akun disensor]", fill=(90, 90, 90), font=font)
    skala = min(1.0, lebar / im.width)
    if skala < 1.0:
        im = im.resize((round(im.width * skala), round(im.height * skala)), Image.LANCZOS)
    if kotak:
        g = ImageDraw.Draw(im)
        font = _font(ImageFont, max(11, round(28 * skala)))
        for teks, b, warna in kotak:
            if not b:
                continue
            x1, y1, x2, y2 = (round(v * skala) for v in b)
            g.rectangle([x1, y1, x2, y2], outline=warna, width=2)
            kiri, atas, kanan, bawah = g.textbbox((0, 0), teks, font=font)
            w, h = kanan - kiri + 6, bawah - atas + 4
            ly = y1 - h if y1 - h >= 0 else y1
            g.rectangle([x1, ly, x1 + w, ly + h], fill=warna)
            g.text((x1 + 3 - kiri, ly + 2 - atas), teks, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=kualitas, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _luminansi(c):
    def kanal(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * kanal(c[0]) + 0.7152 * kanal(c[1]) + 0.0722 * kanal(c[2])


def rasio_kontras(a, b):
    la, lb = _luminansi(a), _luminansi(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


BATAS_KONTRAS_LAPOR = 4.3  # WCAG 4,5:1; perkiraan dari piksel punya galat sekitar +/-0,2, jadi yang mepet tidak dilaporkan


def kontras_perkiraan(png, elemen, density):
    """Perkiraan kontras teks per elemen berlabel: warna latar dominan vs warna 'tinta' paling kontras."""
    pil = _pil()
    if pil is None or not png or not Path(png).exists():
        return []
    Image = pil[0]
    hasil = []
    with Image.open(png) as asli:
        im = asli.convert("RGB")
    W, H = im.size
    for e in elemen:
        if e.get("kelas") == "EditText" or not e.get("label") or not e.get("kotak") or e.get("terpotong") \
                or e.get("tertutup") or len(e["label"].strip()) < 2:
            continue  # terpotong/tertutup bilah sistem atau elemen lain: warnanya tidak bisa diukur jujur
        x1, y1, x2, y2 = e["kotak"]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        w, h = x2 - x1, y2 - y1
        if w < 16 or h < 12 or w * h > 0.4 * W * H or (density and h / density > 160):
            continue
        dx, dy = max(1, int(w * 0.06)), max(1, int(h * 0.1))
        crop = im.crop((x1 + dx, y1 + dy, x2 - dx, y2 - dy))
        if max(crop.size) > 240:  # NEAREST: perkecil tanpa mencampur warna teks dengan latar
            f = 240 / max(crop.size)
            crop = crop.resize((max(1, round(crop.width * f)), max(1, round(crop.height * f))), Image.NEAREST)
        data = crop.tobytes()
        px = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
        if len(px) < 30:
            continue
        kasar = {}
        for p in px:
            kasar.setdefault((p[0] // 24, p[1] // 24, p[2] // 24), []).append(p)
        latar_px = max(kasar.values(), key=len)
        if len(latar_px) / len(px) < 0.35:
            continue  # tidak ada latar yang jelas (gambar/gradasi)
        latar = tuple(sum(c[i] for c in latar_px) / len(latar_px) for i in range(3))
        l_latar = _luminansi(latar)
        jauh = [p for p in px if sum(abs(p[i] - latar[i]) for i in range(3)) > 60]
        if len(jauh) / len(px) < 0.02:
            continue  # tidak ada tinta/teks yang terlihat
        jauh.sort(key=lambda p: abs(_luminansi(p) - l_latar), reverse=True)
        inti = jauh[:max(1, len(jauh) * 15 // 100)]  # inti goresan huruf, bukan tepi anti-aliasing
        tinta = tuple(sum(c[i] for c in inti) / len(inti) for i in range(3))
        skala = crop.width / max(1, (x2 - dx) - (x1 + dx))
        baris_tinta = {i // (3 * crop.width) for i in range(0, len(data), 3)
                       if sum(abs(data[i + k] - latar[k]) for k in range(3)) > 60}
        if density and baris_tinta and (max(baris_tinta) - min(baris_tinta) + 1) / skala / density < 7:
            continue  # tinta setipis garis (pegangan lembar, garis pemisah): bukan teks
        rasio = rasio_kontras(latar, tinta)
        if rasio < BATAS_KONTRAS_LAPOR:
            hasil.append({"label": e["label"][:60], "rasio": round(rasio, 2), "kotak": e["kotak"],
                          "warna_latar": "#%02x%02x%02x" % tuple(round(v) for v in latar),
                          "warna_teks": "#%02x%02x%02x" % tuple(round(v) for v in tinta)})
    return hasil[:25]


def kotak_rahasia(elemen, sensor):
    """Kotak elemen yang isi/labelnya memuat data akun (email, HP, akun Google), untuk ditutup di gambar."""
    hasil = []
    for e in elemen:
        teks = " ".join(filter(None, [e.get("isi"), e.get("label")]))
        if e.get("kotak") and teks and sensor(teks) != teks:
            hasil.append(e["kotak"])
    return hasil


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def ambil_frame(video, detik, keluar, lebar=405):
    exe = ffmpeg()
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-loglevel", "error", "-y", "-ss", f"{max(0.0, detik):.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", f"scale={lebar}:-2", "-q:v", "5", str(keluar)],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and Path(keluar).exists() and Path(keluar).stat().st_size > 0


# ====================================================================== sesi
class Sesi:
    def __init__(self, folder):
        f = Path(folder)
        if not (f / "sesi.json").exists() and (eks.FOLDER_DASAR / folder / "sesi.json").exists():
            f = eks.FOLDER_DASAR / folder
        if not (f / "sesi.json").exists():
            raise GagalOrkestrasi(f"sesi tidak ditemukan: {folder}")
        self.folder = f
        self.data = baca_json(f / "sesi.json")

    @classmethod
    def baru(cls, app, peran, tujuan, pengaturan):
        folder = eks.FOLDER_DASAR / f"sesi_{datetime.now():%Y%m%d_%H%M%S}_{app}_{peran or 'tanpa_login'}"
        folder.mkdir(parents=True, exist_ok=False)
        tulis_json(folder / "sesi.json", {
            "versi": 1, "aplikasi": app, "peran": peran, "tujuan": tujuan, "dibuat": sekarang(),
            "pengaturan": pengaturan, "status": "baru", "observasi": None, "putaran": []})
        return cls(folder)

    def simpan(self):
        self.data["diperbarui"] = sekarang()
        tulis_json(self.folder / "sesi.json", self.data)

    @property
    def app(self):
        return self.data["aplikasi"]

    @property
    def peran(self):
        return self.data.get("peran")

    @property
    def atur(self):
        return {**PENGATURAN_BAWAAN, **self.data.get("pengaturan", {})}

    @property
    def putaran(self):
        return self.data["putaran"]

    def terakhir(self):
        return self.putaran[-1] if self.putaran else None

    def daftar_run(self):
        """[(id, folder)] semua run yang sudah selesai: P0 = observasi, P1.. = putaran."""
        runs = []
        if (self.folder / "observasi" / "ringkasan.json").exists():
            runs.append(("P0", self.folder / "observasi"))
        for p in self.putaran:
            f = self.folder / p.get("folder_uji", f"putaran_{p['ke']}/uji")
            if p.get("status") in ("diuji", "dievaluasi") and (f / "ringkasan.json").exists():
                runs.append((f"P{p['ke']}", f))
        return runs

    def evaluasi_terakhir(self):
        for p in reversed(self.putaran):
            if p.get("evaluasi"):
                return baca_json(self.folder / p["evaluasi"])
        return None


def keluaran(sesi, **isi):
    return {"status": "ok", "sesi": str(sesi.folder), **isi}


def perubahan_sesi(sesi):
    """Semua perubahan data buatan uji di sesi ini beserta status pemulihannya. Data yang baru diperiksa hilang
    (pastikan_tidak_ada) di run sesudahnya juga dihitung dipulihkan."""
    runs = sesi.daftar_run()
    urutan = {rid: i for i, (rid, _) in enumerate(runs)}
    entri, hilang = [], []
    for rid, folder in runs:
        r = baca_json(folder / "ringkasan.json") or {}
        entri += [{"run": rid, "ref": f"{rid}.{e.get('no')}", **e} for e in r.get("perubahan_data") or []]
        hilang += [(urutan[rid], json.dumps(lk.get("target") or {}, ensure_ascii=False)) for lk in r.get("langkah", [])
                   if lk.get("aksi") == "pastikan_tidak_ada" and lk.get("berhasil")]
    for e in entri:
        m = e.get("penanda")
        if e.get("status_pulih") not in ("dipulihkan", "tidak_perlu") and m and any(
                m in teks and i >= urutan[e["run"]] for i, teks in hilang):
            e["status_pulih"] = "dipulihkan"
    return entri


def belum_pulih(sesi):
    return [e for e in perubahan_sesi(sesi) if e.get("status_pulih") in ("belum", "dihapus")]


def penanda_sesi(sesi):
    return sorted({str(m) for m in sesi.data.get("penanda", []) if m}
                  | {e["penanda"] for e in perubahan_sesi(sesi) if e.get("penanda")})


def jalankan_hook(nama, perintah):
    """Perintah shell opsional dari skenario.json (mis. cadangkan/pulihkan database uji di server)."""
    log(f"--- Hook {nama}: {perintah}")
    try:
        r = subprocess.run(perintah, shell=True, capture_output=True, text=True, timeout=900)
        hasil = {"perintah": perintah, "kode_keluar": r.returncode, "waktu": sekarang(),
                 "keluaran": ((r.stdout or "") + (r.stderr or ""))[-800:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        hasil = {"perintah": perintah, "kode_keluar": None, "waktu": sekarang(), "keluaran": str(e)[:300]}
    log(f"  hook {nama}: kode keluar {hasil['kode_keluar']}")
    return hasil


def susun_daftar_uji(sesi, peta, cak):
    """Daftar uji mendalam (overtesting) yang diturunkan dari layar yang ditemukan; status dipertahankan antar-putaran."""
    atur = sesi.atur
    lama = {x["id"]: x for x in sesi.data.get("daftar_uji", [])}
    tugas = []

    def tambah(id_, judul, layar=None):
        if any(x["id"] == id_ for x in tugas):
            return
        tugas.append({**lama.get(id_, {"status": "belum"}), "id": id_, "judul": judul, "layar": layar})

    nama_layar = {s["id"]: eks._norm_label(s["judul"]) or s["id"] for s in peta["layar"]}  # layar sejenis = satu tugas

    for s in peta["layar"]:
        if not layar_bisa_dijelajah(s):
            continue
        kolom = [e for e in s["elemen"] if e.get("kelas") == "EditText" and not e.get("kata_sandi")]
        cari = [e for e in kolom if re.search(r"cari|search|telusuri", f"{e.get('hint') or ''} {e.get('label') or ''}", re.I)]
        if cari:
            tambah(f"pencarian:{nama_layar[s['id']]}", f"Pencarian di {s['id']} ({s['judul']}): kata kunci tanpa hasil, karakter "
                                           "khusus/emoji, teks sangat panjang, lalu kosongkan lagi", s["id"])
        kirim = [e for e in s["elemen"] if e.get("bisa_diklik") and eks.jenis_risiko(e.get("label")) == "ubah_data"
                 and not eks.KATA_KELUAR_AKUN.search(e.get("label") or "")]
        if atur["boleh_ubah_data"] and kirim and len(kolom) > len(cari):
            tambah(f"form:{nama_layar[s['id']]}", f"Form {s['id']} ({s['judul']}): kirim kosong, format salah, teks panjang/emoji, "
                                      "lalu buat data berpenanda {penanda} dan hapus lagi", s["id"])
    if atur["boleh_ubah_data"]:
        for x in cak.get("aksi_data", []):
            if not eks.KATA_KELUAR_AKUN.search(x["label"] or ""):
                tambah(f"crud:{nama_layar.get(x['layar'], x['layar'])}:{x['label']}", f"Aksi data '{x['label']}' di {x['layar']}: jalankan pada data "
                                                          "buatan uji (berpenanda) lalu pulihkan", x["layar"])
    if atur.get("mode") == "overtesting":
        for id_, judul in (("teks_besar", "Teks besar: skala font 1.3 lalu 2.0, tangkap 3-4 layar utama; cari teks "
                                          "terpotong, bertumpuk, atau tombol yang terdorong keluar layar"),
                           ("tema_gelap", "Tema gelap: atur_tema gelap, buka ulang aplikasi, tangkap layar-layar utama; "
                                          "cari teks yang hilang/kontras rusak/warna tidak berubah"),
                           ("tanpa_internet", "Tanpa internet: atur_jaringan mati, buka/muat ulang 2-3 layar data, nilai pesan "
                                              "error dan apakah aplikasi pulih setelah internet hidup lagi"),
                           ("ketuk_cepat", "Ketuk cepat: tombol utama/navigasi diketuk 2-3 kali berturut-turut tanpa jeda; "
                                           "cari layar ganda, aksi ganda, atau crash"),
                           ("navigasi_kembali", "Konsistensi Back: masuk 3-4 tingkat menu lalu kembali satu per satu; layar "
                                                "yang muncul harus sesuai urutan")):
            tambah(id_, judul)
    sesi.data["daftar_uji"] = tugas
    return tugas


# ====================================================================== peta layar & pengukuran
def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def bangun_peta(sesi, kontras=False):
    """Layar unik dari semua run sesi (L1, L2, ...) + pemetaan ref langkah (P1.3) -> layar."""
    layar, ref_layar, runs, luar = [], {}, [], {}
    for rid, folder in sesi.daftar_run():
        r = baca_json(folder / "ringkasan.json")
        dpi = (r.get("perangkat") or {}).get("dpi")
        density = dpi / 160 if dpi else None
        pkg = (r.get("aplikasi") or {}).get("package")
        runs.append({"id": rid, "folder": folder, "ringkasan": r, "density": density,
                     "jelajah": baca_json(folder / "jelajah.json") if (folder / "jelajah.json").exists() else None})
        jalur = []
        for lk in r.get("langkah", []):
            ref = f"{rid}.{lk['no']}"
            if lk.get("berhasil") and lk.get("aksi") not in ("navigasi_otomatis", "pemulihan_otomatis"):
                jalur.append(lk["langkah"])
            if lk.get("privasi") or not lk.get("xml") or not (folder / lk["xml"]).exists():
                continue  # langkah privasi (misalnya layar login Google) tidak pernah dipakai sebagai bukti/gambar
            try:
                xml = (folder / lk["xml"]).read_text(encoding="utf-8")
                paket = lk.get("paket") or eks.paket_xml(eks._root(xml))
                if pkg and paket and paket != pkg and not lk.get("keluar_ke_pengaturan"):
                    luar[ref] = paket  # layar utama HP / aplikasi lain: bukan layar aplikasi yang diuji
                    continue
                elemen = eks.daftar_elemen(xml, density, maks=120, kotak=True, zona=r.get("zona_sistem"))
            except ET.ParseError:
                continue
            tanda = frozenset((e["kelas"], eks._norm_label(e.get("label") or e.get("hint") or ""))
                              for e in elemen if e.get("label") or e.get("hint"))
            sama = next((s for s in layar if _jaccard(s["_tanda"], tanda) >= 0.8), None)
            if sama:
                sama["refs"].append(ref)
                ref_layar[ref] = sama["id"]
                if lk.get("tak_terjangkau"):
                    sama["tak_terjangkau"].append({"ref": ref, **lk["tak_terjangkau"]})
                if not sama["screenshot"] and lk.get("screenshot"):
                    sama["screenshot"] = folder / lk["screenshot"]
                continue
            label = [e["label"].replace("\n", " ") for e in elemen if e.get("label")]
            s = {"id": f"L{len(layar) + 1}", "_tanda": tanda, "refs": [ref], "run": rid, "folder": folder,
                 "asal": {"aksi": lk.get("aksi"), "langkah": lk["langkah"], "sistem": bool(lk.get("keluar_ke_pengaturan"))},
                 "screenshot": folder / lk["screenshot"] if lk.get("screenshot") else None,
                 "elemen": elemen, "metrik": lk.get("metrik") or {}, "density": density,
                 "jalur": jalur[-6:], "judul": " / ".join(label[:2])[:70] or "(tanpa teks)",
                 "tak_terjangkau": [{"ref": ref, **lk["tak_terjangkau"]}] if lk.get("tak_terjangkau") else []}
            layar.append(s)
            ref_layar[ref] = s["id"]
    if kontras:
        cache_f = sesi.folder / "cache_kontras.json"
        cache = baca_json(cache_f, {}) or {}
        for s in layar:
            kunci = str(s["screenshot"])
            if kunci not in cache:
                cache[kunci] = kontras_perkiraan(s["screenshot"], s["elemen"], s["density"])
            s["kontras"] = cache[kunci]
        tulis_json(cache_f, cache)
    return {"layar": layar, "ref_layar": ref_layar, "runs": runs, "luar": luar}


def layar_bisa_dijelajah(s):
    """Layar aplikasi biasa (bukan form login, gerbang izin, atau layar sistem) yang menunya perlu dijelajah."""
    a = s.get("asal") or {}
    if a.get("sistem") or a.get("aksi") in ("login", "izin_pengaturan"):
        return False
    if str(a.get("langkah", "")).startswith(("gerbang_", "layar_sistem", "izinkan_di_pengaturan")):
        return False
    return not any(e.get("kata_sandi") for e in s["elemen"])


def cakupan_elemen(peta):
    """Cakupan elemen yang bisa diklik di layar aplikasi: hasil penjelajah otomatis (dijelajah/diwakili/diblokir/
    tak terjangkau) digabung dengan tap rencana LLM. Kunci = (layar L, label)."""
    tap = []
    for run in peta["runs"]:
        for st in (baca_json(run["folder"] / "rencana.json") or {}).get("langkah", []) or []:
            if st.get("aksi") == "tap" and isinstance(st.get("target"), dict):
                tap.append(st["target"])

    def diketuk(e, label):
        for t_ in tap:
            if t_.get("label") and t_["label"].strip() == label:
                return True
            if t_.get("label_mengandung") and t_["label_mengandung"] in label:
                return True
            if t_.get("koordinat") and e.get("kotak"):
                x, y = t_["koordinat"][:2]
                b = e["kotak"]
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    return True
        return False

    peringkat = {"dijelajah": 4, "diwakili": 3, "diblokir": 2, "tak_terjangkau": 1}
    elemen = {}

    def catat(kunci, info):
        lama = elemen.get(kunci)
        if lama is None or peringkat.get(info["status"], 0) > peringkat.get(lama["status"], 0):
            elemen[kunci] = {**(lama or {}), **info}

    for run in peta["runs"]:
        j = run.get("jelajah")
        if not j:
            continue
        ke_l = {L["id"]: peta["ref_layar"].get(f"{run['id']}.{L['ref']}") for L in j["layar"]}
        jalur = {L["id"]: L.get("jalur") or [] for L in j["layar"]}
        for e in j["elemen"]:
            lid = ke_l.get(e["layar"])
            if not lid:
                continue  # layar di luar aplikasi / tidak tertangkap
            label = (e.get("label") or f"[{e.get('kelas')}]").split("\n")[0][:60]
            catat((lid, label), {"layar": lid, "label": label, "kelas": e.get("kelas"), "status": e["status"],
                                 "alasan": e.get("alasan"), "jalur": jalur.get(e["layar"])})
    for s in peta["layar"]:
        if not layar_bisa_dijelajah(s):
            continue
        for e in s["elemen"]:
            label = (e.get("label") or "").strip()
            if not e.get("bisa_diklik") or e.get("kelas") == "EditText" or not label or len(label) > 80:
                continue
            kunci = (s["id"], label.split("\n")[0][:60])
            if eks.label_berisiko(label):
                catat(kunci, {"layar": s["id"], "label": kunci[1], "kelas": e.get("kelas"), "status": "diblokir",
                              "alasan": "mengubah data/berisiko"})
            elif diketuk(e, label):
                catat(kunci, {"layar": s["id"], "label": kunci[1], "kelas": e.get("kelas"), "status": "dijelajah"})
            elif kunci not in elemen:
                elemen[kunci] = {"layar": s["id"], "label": kunci[1], "kelas": e.get("kelas"), "status": "belum"}
    sandi_layar = {s["id"] for s in peta["layar"] if sum(1 for e in s["elemen"] if e.get("kata_sandi")) >= 2}
    # Label yang sama di varian layar yang sama (mis. beranda sebelum/sesudah digulir) dihitung satu elemen.
    unik = {}
    for x in sorted(elemen.values(), key=lambda x: -peringkat.get(x["status"], 0)):
        unik.setdefault(x["label"].lower(), x)
    sudah = [x for x in unik.values() if x["status"] in ("dijelajah", "diwakili")]
    belum = [x for x in unik.values() if x["status"] in ("belum", "gagal")]
    return {"total": len(sudah) + len(belum), "sudah": len(sudah), "belum": belum,
            "diwakili": sum(1 for x in sudah if x["status"] == "diwakili"),
            "aksi_data": [x for x in elemen.values() if x["status"] == "diblokir" and x["layar"] not in sandi_layar],
            "tak_terjangkau": [x for x in unik.values() if x["status"] == "tak_terjangkau"],
            "jumlah_layar": sum(1 for s in peta["layar"] if layar_bisa_dijelajah(s))}


def masalah_berkode(s):
    """Daftar masalah otomatis satu layar dengan kode (A1, T1, H1, B1, K1) untuk anotasi & bukti."""
    hasil = []
    kotak = [e["kotak"] for e in s["elemen"] if e.get("kotak")]
    luas_layar = (max(b[2] for b in kotak) * max(b[3] for b in kotak)) if kotak else 0
    for kunci, huruf in KODE_MASALAH.items():
        for i, m in enumerate((s["metrik"].get("masalah") or {}).get(kunci, []), 1):
            b = eks._bounds(m.get("bounds"))
            item = {"kode": f"{huruf}{i}", "jenis": kunci, "kelas": m.get("kelas"), "label": m.get("label"),
                    "hint": m.get("hint"), "ukuran_dp": m.get("ukuran_dp"), "bounds": b}
            if huruf == "A" and b and luas_layar and (b[2] - b[0]) * (b[3] - b[1]) >= 0.5 * luas_layar:
                item["catatan"] = ("kontainer hampir selayar, kemungkinan GestureDetector (misalnya untuk menutup "
                                   "keyboard); TalkBack tetap bisa berhenti di sini tanpa label")
            hasil.append(item)
    for i, k in enumerate(s.get("kontras") or [], 1):
        hasil.append({"kode": f"K{i}", "jenis": "kontras_rendah", "label": k["label"], "rasio": k["rasio"],
                      "warna_teks": k["warna_teks"], "warna_latar": k["warna_latar"], "bounds": k["kotak"]})
    for i, u in enumerate(s.get("tak_terjangkau") or [], 1):
        hasil.append({"kode": f"U{i}", "jenis": "tidak_terjangkau", "label": u.get("label"), "kelas": u.get("kelas"),
                      "ref": u.get("ref"), "bounds": eks._bounds(u.get("bounds")),
                      "catatan": "tertutup bilah navigasi/elemen lain dan tidak bisa digulir ke area yang terlihat"})
    return hasil


def ringkas_layar(s, untuk="evaluator"):
    m = s["metrik"]
    hasil = {"layar": s["id"], "judul": s["judul"], "ref": s["refs"][:8], "jalur": s["jalur"]}
    if untuk == "perancang":
        hasil["elemen"] = [{k: v for k, v in e.items() if k != "kotak"} for e in s["elemen"][:60]]
        if len(s["elemen"]) > 60:
            hasil["catatan"] = f"{len(s['elemen'])} elemen, hanya 60 pertama yang ditampilkan"
        return hasil
    hasil.update({"jumlah_node": m.get("jumlah_node"), "kedalaman_maks": m.get("kedalaman_maks"),
                  "elemen_interaktif": m.get("elemen_interaktif"), "elemen_teks": m.get("elemen_teks"),
                  "rasio_interaktif_berlabel": m.get("rasio_interaktif_berlabel"),
                  "jumlah_heading": m.get("jumlah_heading")})
    masalah = masalah_berkode(s)
    if masalah:
        hasil["masalah"] = [{k: v for k, v in x.items() if k != "bounds" and v is not None} for x in masalah[:30]]
    hasil["teks_di_layar"] = [t.replace("\n", " ")[:60] for t in (m.get("teks_di_layar") or [])[:15]]
    return hasil


def waktu_langkah(lk):
    return lk.get("respon_ms") if lk.get("respon_ms") is not None else lk.get("sampai_stabil_ms")


def pengukuran(peta):
    """Hasil terukur (tanpa LLM): startup, langkah lambat, langkah gagal, statistik pengukuran berulang."""
    startup, lambat, gagal, grup = [], [], [], {}
    for run in peta["runs"]:
        for lk in run["ringkasan"].get("langkah", []):
            ref = f"{run['id']}.{lk['no']}"
            st = lk.get("startup") or {}
            if st.get("total_time_ms") is not None:
                jenis = (st.get("launch_state") or "COLD").upper()
                batas = VITALS_STARTUP_MS.get(jenis, 5000)
                startup.append({"ref": ref, "total_time_ms": st["total_time_ms"], "jenis": jenis,
                                "batas_vitals_ms": batas, "berlebihan": st["total_time_ms"] >= batas})
            if not lk.get("berhasil"):
                gagal.append({"ref": ref, "langkah": lk["langkah"], "galat": lk.get("galat")})
                continue
            w = waktu_langkah(lk)
            if lk.get("aksi") in AKSI_BERWAKTU and w is not None:
                grup.setdefault((lk["aksi"], re.sub(r"_\d+$", "", lk["langkah"])), []).append((ref, w))
                if w >= BATAS_JEDA_MS and lk.get("aksi") in AKSI_DINILAI_WAKTU:
                    lambat.append({"ref": ref, "langkah": lk["langkah"], "aksi": lk["aksi"], "waktu_ms": w,
                                   "jenis_waktu": "respon" if lk.get("respon_ms") is not None else "sampai_stabil",
                                   "lebih_dari_10_detik": w >= BATAS_PERHATIAN_MS})
    statistik = [{"langkah": nama, "aksi": aksi, "n": len(v), "median_ms": round(statistics.median(x for _, x in v)),
                  "min_ms": min(x for _, x in v), "maks_ms": max(x for _, x in v), "ref": [r for r, _ in v]}
                 for (aksi, nama), v in grup.items() if len(v) >= 2]
    return {"startup": startup, "langkah_lambat": lambat, "langkah_gagal": gagal, "statistik_berulang": statistik}


def tabel_langkah(run, ref_layar):
    baris = []
    for lk in run["ringkasan"].get("langkah", []):
        ref = f"{run['id']}.{lk['no']}"
        item = {"ref": ref, "langkah": lk["langkah"], "aksi": lk.get("aksi"), "berhasil": lk.get("berhasil"),
                "respon_ms": lk.get("respon_ms"), "mulai_berubah_ms": lk.get("mulai_berubah_ms"),
                "stabil_ms": lk.get("sampai_stabil_ms"), "durasi_ms": lk.get("durasi_aksi_ms"),
                "tanpa_perubahan": lk.get("tanpa_perubahan"), "layar": ref_layar.get(ref), "galat": lk.get("galat"),
                "catatan": lk.get("catatan"), "startup": lk.get("startup") or None,
                "keluar_ke_pengaturan": lk.get("keluar_ke_pengaturan"), "keluar_aplikasi": lk.get("keluar_aplikasi"),
                "crash": lk.get("crash"), "log_galat": [g["pesan"][:160] for g in lk.get("log_galat") or []][:3] or None,
                "ubah_data": lk.get("ubah_data"), "fase": lk.get("fase"), "pemulihan": lk.get("pemulihan"),
                "tak_terjangkau": (lk.get("tak_terjangkau") or {}).get("label"), "digulir": lk.get("digulir")}
        baris.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
    return baris


def frame_lambat(run, folder_keluar, sensor, maks=4):
    r = run["ringkasan"]
    video = run["folder"] / r["rekaman"] if r.get("rekaman") else None
    if not video or not video.exists():
        return []
    lambat = [lk for lk in r.get("langkah", []) if lk.get("berhasil") and not lk.get("privasi")
              and lk.get("aksi") in AKSI_BERWAKTU
              and (waktu_langkah(lk) or 0) >= BATAS_JEDA_MS and lk.get("t_mulai_ms") is not None]
    hasil = []
    for lk in sorted(lambat, key=lambda x: -waktu_langkah(x))[:max(1, maks // 2)]:
        w = waktu_langkah(lk)
        for frac in (0.3, 0.7):
            keluar = Path(folder_keluar) / f"frame_{run['id']}_{lk['no']:02d}_{int(frac * 100)}.jpg"
            keluar.parent.mkdir(parents=True, exist_ok=True)
            if ambil_frame(video, (lk["t_mulai_ms"] + frac * w) / 1000, keluar):
                hasil.append({"ref": f"{run['id']}.{lk['no']}", "langkah": lk["langkah"], "waktu_ms": w,
                              "detik_setelah_aksi": round(frac * w / 1000, 2), "file": keluar,
                              **_tutup_untuk_frame(run, lk["no"], sensor)})
    if lambat and not hasil:
        log("  frame rekaman tidak bisa diambil (pasang: pip install imageio-ffmpeg)")
    return hasil[:maks]


def _tutup_untuk_frame(run, no, sensor):
    """Saat menunggu (misalnya login), layar masih sama dengan tangkapan langkah sebelumnya: pakai XML-nya."""
    for lk in reversed([x for x in run["ringkasan"].get("langkah", []) if x["no"] < no and x.get("xml")]):
        try:
            elemen = eks.daftar_elemen((run["folder"] / lk["xml"]).read_text(encoding="utf-8"), None, maks=200, kotak=True)
        except (OSError, ET.ParseError):
            continue
        tutup = kotak_rahasia(elemen, sensor)
        if not tutup:
            return {}
        lebar = max((e["kotak"][2] for e in elemen if e.get("kotak")), default=None)
        pil = _pil()
        if pil and lk.get("screenshot") and (run["folder"] / lk["screenshot"]).exists():
            with pil[0].open(run["folder"] / lk["screenshot"]) as im:
                lebar = im.width  # lebar layar HP dalam piksel = ruang koordinat bounds
        return {"tutup": tutup, "lebar_asal": lebar}
    return {}


def ringkas_izin(ringkasan):
    """Teks singkat izin yang diberikan otomatis lewat adb pada satu run."""
    if not ringkasan.get("beri_izin_otomatis", True):
        return "izin TIDAK diberikan otomatis (alur permintaan izin aplikasi teramati apa adanya)"
    nama = sorted({n for x in ringkasan.get("izin_otomatis") or [] for k in ("runtime", "appops", "lain") for n in x.get(k, [])})
    return ("izin diberikan otomatis lewat adb sebelum uji: " + (", ".join(nama) if nama else "tidak ada yang perlu")
            + " (dialog izin aplikasi tidak teramati)")


def info_aplikasi(sesi, cfg, peta):
    app = cfg["aplikasi"][sesi.app]
    r = peta["runs"][-1]["ringkasan"] if peta["runs"] else {}
    p = r.get("perangkat") or {}
    return {"nama": app.get("nama", sesi.app), "kode": sesi.app, "package": app.get("package"),
            "versi": (r.get("aplikasi") or {}).get("versi"),
            "perangkat": " ".join(filter(None, [p.get("merek"), p.get("model")])) or None,
            "android": p.get("android"), "dpi": p.get("dpi")}


# ====================================================================== agen 1: LLM Perancang
def rapikan_rencana(rencana, sesi):
    rencana["aplikasi"] = sesi.app
    langkah = rencana.get("langkah")
    if not isinstance(langkah, list):
        return rencana
    langkah = [s for s in langkah if isinstance(s, dict)]
    if not langkah or langkah[0].get("aksi") != "buka_aplikasi":
        langkah.insert(0, {"aksi": "buka_aplikasi", "nama": "buka_aplikasi", "catatan": "titik awal yang sama"})
    if sesi.peran and not any(s.get("aksi") == "login" for s in langkah):
        langkah.insert(1, {"aksi": "login", "nama": f"login_{sesi.peran}", "peran": sesi.peran})
    if sesi.peran:
        rencana.setdefault("peran", sesi.peran)
    rencana["langkah"] = langkah
    return rencana


def jalur_jelajah(peta):
    """Layar L -> daftar label yang diketuk penjelajah otomatis dari layar awal untuk mencapainya."""
    hasil = {}
    for run in peta["runs"]:
        for J in (run.get("jelajah") or {}).get("layar", []):
            lid = peta["ref_layar"].get(f"{run['id']}.{J.get('ref')}")
            if lid and lid not in hasil:
                hasil[lid] = J.get("jalur") or []
    return hasil


def ringkas_layar_perancang(s, jalur=None):
    elemen = []
    for e in s["elemen"][:70]:
        x = {"no": e["no"], "kelas": e["kelas"]}
        if e.get("label"):
            x["label"] = e["label"][:80]
        for k in ("hint", "isi", "bisa_diklik", "sakelar", "tercentang", "terpilih", "kata_sandi", "nonaktif",
                  "bisa_digulir", "tertutup"):
            if e.get(k):
                x[k] = e[k]
        if not e.get("label") and e.get("bisa_diklik"):
            x["pusat"] = e.get("pusat")
        elemen.append(x)
    hasil = {"layar": s["id"], "judul": s["judul"], "ref": s["refs"][:3], "elemen": elemen}
    if jalur is not None:
        hasil["jalur_dari_layar_awal"] = jalur
    else:
        hasil["jalur_langkah"] = s["jalur"]
    if len(s["elemen"]) > 70:
        hasil["catatan"] = f"{len(s['elemen'])} elemen, hanya 70 pertama yang ditampilkan"
    return hasil


def ringkas_jelajah(run, peta):
    """Ringkasan run penjelajah otomatis untuk LLM: statistik, layar, dan peristiwa penting (bukan semua langkah)."""
    j = run.get("jelajah") or {}
    ke_l = {L["id"]: peta["ref_layar"].get(f"{run['id']}.{L.get('ref')}") for L in j.get("layar", [])}
    peristiwa = []
    for lk in run["ringkasan"].get("langkah", []):
        ref = f"{run['id']}.{lk['no']}"
        info = {"ref": ref, "langkah": lk["langkah"], "layar": peta["ref_layar"].get(ref)}
        w = waktu_langkah(lk)
        if lk.get("crash"):
            peristiwa.append({**info, "jenis": "crash", "galat": lk.get("galat")})
        elif lk.get("log_galat"):
            peristiwa.append({**info, "jenis": "log_galat", "log": [g["pesan"][:160] for g in lk["log_galat"][:2]]})
        if lk.get("tak_terjangkau"):
            peristiwa.append({**info, "jenis": "tak_terjangkau", "elemen": lk["tak_terjangkau"].get("label")})
        if lk.get("tanpa_perubahan") and lk.get("aksi") == "tap":
            peristiwa.append({**info, "jenis": "tap_tanpa_perubahan"})
        if lk.get("keluar_aplikasi") and not lk.get("crash"):
            peristiwa.append({**info, "jenis": "keluar_aplikasi", "paket": lk["keluar_aplikasi"]})
        if lk.get("aksi") == "tap" and w and w >= BATAS_JEDA_MS:
            peristiwa.append({**info, "jenis": "lambat", "sampai_stabil_ms": w, "mulai_berubah_ms": lk.get("mulai_berubah_ms")})
    return {"statistik": j.get("statistik"),
            "layar": [{"id": L["id"], "layar": ke_l.get(L["id"]), "judul": L["judul"], "jalur": L.get("jalur")}
                      for L in j.get("layar", [])],
            "peristiwa": peristiwa[:80]}


def log_galat_sesi(peta, maks=40):
    hasil, lihat = [], set()
    for run in peta["runs"]:
        for lk in run["ringkasan"].get("langkah", []):
            for g in lk.get("log_galat") or []:
                kunci = (g["jenis"], g["pesan"][:120])
                if kunci in lihat:
                    continue
                lihat.add(kunci)
                ref = f"{run['id']}.{lk['no']}"
                hasil.append({"ref": ref, "langkah": lk["langkah"], "layar": peta["ref_layar"].get(ref), **g})
    return hasil[:maks]


def agen_perancang(sesi, k, ctx, jenis="rencana"):
    cfg, conf = ctx["cfg"], ctx["llm"]
    peta = bangun_peta(sesi)
    if not peta["layar"]:
        raise GagalOrkestrasi("belum ada layar yang tertangkap; jalankan 'mulai' (observasi) dulu")
    folder = sesi.folder / f"putaran_{k}"
    app = cfg["aplikasi"][sesi.app]
    atur = sesi.atur
    sistem = (baca_teks(FILE_PROMPT_PERANCANG) + "\n\n" + eks.SKEMA
              + f'\nAPLIKASI DAN PERAN YANG TERSEDIA\n- "{sesi.app}" ({app.get("nama")}): '
              + ", ".join(app.get("akun", {})) + "\n")
    info = info_aplikasi(sesi, cfg, peta)
    ev = sesi.evaluasi_terakhir() if k >= 2 else None
    jalur = jalur_jelajah(peta)
    cak = cakupan_elemen(peta)
    crud = bool(atur["boleh_ubah_data"])
    teks = [f"APLIKASI: {json.dumps(info, ensure_ascii=False)}",
            f"PERAN UJI: {sesi.peran or '(tanpa login)'}",
            f"TUJUAN UJI: {sesi.data['tujuan']}",
            f"PUTARAN: {k} dari maksimal {atur['maks_putaran']}; jenis putaran: {jenis}; mode sesi: {atur.get('mode', 'evaluasi')}",
            "BATASAN: " + ("CRUD DIIZINKAN dengan pemulihan wajib (lihat aturan CRUD): data yang dibuat harus memuat "
                           "{penanda} dan dihapus lagi; isian lama dikembalikan dengan {nilai_awal:nama_langkah}; hapus/ubah "
                           "status hanya pada data buatan uji." if crud else
                           "tombol yang mengubah data/keluar dari aplikasi diblokir pelaksana.")
            + " Selalu diblokir: pembayaran, WhatsApp/telepon/SMS/email, hapus/nonaktifkan akun, ganti kata sandi dan "
              "verifikasi 2 langkah akun uji."
            + (" Izin aplikasi sudah diberikan otomatis." if atur["beri_izin_otomatis"] else "")
            + " Kalau aplikasi membuka Pengaturan sistem, pelaksana menyalakan izinnya lalu kembali ke aplikasi."]
    if crud:
        teks.append("PENANDA DATA UJI SESI INI (data berpenanda ini boleh dihapus/diubah statusnya): "
                    + json.dumps(penanda_sesi(sesi), ensure_ascii=False))
    teks += ["", "PETA LAYAR (JSON):",
             json.dumps([ringkas_layar_perancang(s, jalur.get(s["id"])) for s in peta["layar"] if layar_bisa_dijelajah(s)],
                        ensure_ascii=False)]
    sebelumnya = sesi.putaran[k - 2] if k >= 2 else None
    if sebelumnya:
        run = next((r for r in peta["runs"] if r["id"] == f"P{k - 1}"), None)
        if run and run.get("jelajah"):
            teks += ["", f"HASIL PUTARAN {k - 1} (penjelajah otomatis):", json.dumps(ringkas_jelajah(run, peta), ensure_ascii=False)]
        elif run:
            teks += ["", f"HASIL PUTARAN {k - 1} (rencana '{(baca_json(sesi.folder / sebelumnya['rencana']) or {}).get('judul')}'):",
                     json.dumps([{x: y for x, y in b.items() if x in ("ref", "langkah", "aksi", "berhasil", "galat", "layar",
                                                                      "pemulihan", "tanpa_perubahan", "keluar_aplikasi")}
                                 for b in tabel_langkah(run, peta["ref_layar"])], ensure_ascii=False)]
    if ev:
        teks += ["", "TEMUAN SEMENTARA EVALUATOR: " + "; ".join(f"{t.get('id')} {t.get('judul')}" for t in ev.get("temuan", [])[:25]),
                 "", "PERMINTAAN EVALUATOR UNTUK PUTARAN INI (prioritas utama):",
                 json.dumps(ev.get("permintaan_uji_ulang", []), ensure_ascii=False)]
    if cak["total"]:
        teks += ["", f"CAKUPAN ELEMEN: {cak['sudah']} dari {cak['total']} elemen yang bisa diklik sudah dijelajah "
                     f"({cak['diwakili']} di antaranya diwakili item sejenis)."]
        if cak["belum"]:
            teks += [("ELEMEN BELUM DIJELAJAH - jelajahi sebanyak mungkin (tap -> tangkap -> kembali); jalur = ketukan dari "
                      "layar awal untuk mencapai layarnya:" if atur.get("jelajah_menyeluruh") or atur.get("mode") == "overtesting"
                      else "ELEMEN BELUM DIJELAJAH (opsional, kerjakan kalau relevan dengan tujuan):"),
                     json.dumps(cak["belum"][:40], ensure_ascii=False)]
        if cak["tak_terjangkau"]:
            teks += ["ELEMEN TAK TERJANGKAU (tertutup dan tidak bisa digulir; sudah dicatat sebagai temuan, jangan diulang):",
                     json.dumps([{x: e.get(x) for x in ("layar", "label")} for e in cak["tak_terjangkau"][:15]], ensure_ascii=False)]
    tugas = susun_daftar_uji(sesi, peta, cak) if atur.get("mode") == "overtesting" or crud else []
    if tugas:
        teks += ["", "DAFTAR UJI MENDALAM (overtesting). Kerjakan sebanyak mungkin yang 'belum' di putaran ini dan tulis id-nya "
                     "di field \"tugas\" rencana:",
                 json.dumps([{x: t_.get(x) for x in ("id", "judul", "status")} for t_ in tugas], ensure_ascii=False)]
    pulih = belum_pulih(sesi)
    if pulih:
        teks += ["", "PERUBAHAN DATA YANG BELUM DIPULIHKAN (WAJIB dipulihkan di putaran ini: cari penandanya, hapus, lalu "
                     "pastikan_tidak_ada):",
                 json.dumps([{x: e.get(x) for x in ("ref", "jenis", "objek", "penanda", "status_pulih")} for e in pulih],
                            ensure_ascii=False)]
    if jenis == "pemulihan":
        teks += ["", "PUTARAN PEMULIHAN: tugasmu HANYA mengembalikan data di atas ke keadaan semula. Navigasi ke daftar "
                     "datanya, temukan item berpenanda, hapus (\"ubah_data\": \"hapus\"), lalu pastikan_tidak_ada "
                     "{\"label_mengandung\": \"<penanda>\"}. Jangan menguji hal lain dan jangan membuat data baru."]
    teks += ["", "Susun rencana uji untuk putaran ini. Keluarkan HANYA objek JSON rencana."]
    isi = [{"type": "text", "text": "\n".join(teks)}]

    if conf.get("kirim_gambar", True):
        terakhir = peta["runs"][-1]["id"]
        penting = {t_["layar"] for t_ in tugas if t_.get("layar") and t_["status"] == "belum"} | {e["layar"] for e in cak["belum"]}
        pilih = [peta["layar"][0]] + [s for s in peta["layar"][1:] if s["id"] in penting]
        pilih += [s for s in reversed(peta["layar"][1:]) if s["run"] == terakhir and s not in pilih]
        pilih += [s for s in reversed(peta["layar"][1:]) if s not in pilih]
        n = 0
        for s in pilih:
            if n >= int(conf["maks_gambar_perancang"]):
                break
            if not s["screenshot"] or not s["screenshot"].exists() or not layar_bisa_dijelajah(s):
                continue
            kotak = [(str(e["no"]), e.get("kotak"), WARNA_NOMOR[i % len(WARNA_NOMOR)]) for i, e in enumerate(s["elemen"][:70])]
            url = gambar_url(s["screenshot"], int(conf["lebar_gambar_perancang"]), kotak,
                             tutup=kotak_rahasia(s["elemen"], ctx["sensor"]))
            isi += [{"type": "text", "text": f"Screenshot layar {s['id']} ({s['judul']}); nomor kotak = 'no' di daftar elemen {s['id']}:"},
                    {"type": "image_url", "image_url": {"url": url}}]
            n += 1

    pesan = [{"role": "system", "content": sistem}, {"role": "user", "content": isi}]
    salah = []
    for coba in range(1, 4):
        jawaban, model = ctx["klien"].chat(pesan, "perancang", f"perancang_{coba}", folder)
        try:
            rencana = rapikan_rencana(ambil_json(jawaban), sesi)
            salah = eks.validasi_rencana(rencana, cfg, crud)
        except ValueError as e:
            salah = [str(e)]
        if not salah:
            rencana.setdefault("judul", f"putaran_{k}")
            return rencana, model
        log(f"  rencana ditolak validator (coba {coba}): {salah[:3]}")
        pesan += [{"role": "assistant", "content": jawaban},
                  {"role": "user", "content": "Rencana ditolak validator:\n- " + "\n- ".join(salah)
                   + "\nPerbaiki dan kirim ulang HANYA objek JSON rencana."}]
    raise GagalOrkestrasi("LLM Perancang gagal membuat rencana yang valid: " + "; ".join(salah[:5]))


# ====================================================================== agen 2: LLM Evaluator
def rapikan_evaluasi(ev, peta):
    def angka(v):
        try:
            v = float(str(v).replace(",", ".").rstrip("%"))
        except ValueError:
            return 0.0
        return max(0.0, min(1.0, v / 100 if v > 1 else v))

    hasil = {"ringkasan_eksekutif": str(ev.get("ringkasan_eksekutif") or "").strip(),
             "keyakinan": angka(ev.get("keyakinan", 0)),
             "alasan_keyakinan": str(ev.get("alasan_keyakinan") or "").strip(),
             "keputusan": "laporan" if str(ev.get("keputusan", "")).lower().startswith("lapor") else "uji_ulang",
             "temuan": [], "hal_positif": [str(x) for x in (ev.get("hal_positif") or []) if x],
             "cakupan": {"sudah_diuji": [], "belum_diuji": []}, "permintaan_uji_ulang": []}
    cak = ev.get("cakupan") if isinstance(ev.get("cakupan"), dict) else {}
    for k in ("sudah_diuji", "belum_diuji"):
        hasil["cakupan"][k] = [str(x) for x in (cak.get(k) or []) if x]
    refs, layar_ids = set(peta["ref_layar"]) | {f"{r['id']}.{lk['no']}" for r in peta["runs"]
                                                for lk in r["ringkasan"].get("langkah", [])}, {s["id"] for s in peta["layar"]}
    for i, t in enumerate(x for x in (ev.get("temuan") or []) if isinstance(x, dict)):
        tingkat = str(t.get("tingkat", "sedang")).lower()
        kategori = str(t.get("kategori", "lainnya")).lower().replace(" ", "_")
        bukti = []
        for b in t.get("bukti") or []:
            if isinstance(b, str):
                b = {"keterangan": b}
            if not isinstance(b, dict):
                continue
            b = {k: str(v) for k, v in b.items() if v not in (None, "")}
            if b.get("ref") and b["ref"] not in refs:
                b["catatan_validasi"] = "ref tidak dikenal"
            if b.get("layar") and b["layar"] not in layar_ids:
                b["catatan_validasi"] = "layar tidak dikenal"
            bukti.append(b)
        hasil["temuan"].append({
            "id": str(t.get("id") or f"T{i + 1}"), "judul": str(t.get("judul") or "(tanpa judul)"),
            "kategori": kategori if kategori in KATEGORI else "lainnya",
            "tingkat": tingkat if tingkat in TINGKAT else "sedang",
            "sumber": str(t.get("sumber") or ""), "deskripsi": str(t.get("deskripsi") or ""),
            "bukti": bukti, "dasar": str(t.get("dasar") or ""), "rekomendasi": str(t.get("rekomendasi") or ""),
            "langkah_reproduksi": [str(x) for x in (t.get("langkah_reproduksi") or []) if x][:10]})
    urutan = {t: i for i, t in enumerate(TINGKAT)}
    hasil["temuan"].sort(key=lambda t: urutan[t["tingkat"]])
    for p in (ev.get("permintaan_uji_ulang") or [])[:5]:
        if isinstance(p, str):
            p = {"tugas": p}
        if isinstance(p, dict) and p.get("tugas"):
            hasil["permintaan_uji_ulang"].append({"jenis": str(p.get("jenis") or "eksplorasi"),
                                                  "tugas": str(p["tugas"]), "alasan": str(p.get("alasan") or "")})
    return hasil


def agen_evaluator(sesi, k, ctx):
    cfg, conf = ctx["cfg"], ctx["llm"]
    peta = bangun_peta(sesi, kontras=True)
    folder = sesi.folder / f"putaran_{k}"
    atur = sesi.atur
    ukur = pengukuran(peta)
    ev_lama = sesi.evaluasi_terakhir()
    run_baru = {"P0", f"P{k}"} if not ev_lama else {f"P{k}"}
    teks = [f"APLIKASI: {json.dumps(info_aplikasi(sesi, cfg, peta), ensure_ascii=False)}",
            f"PERAN UJI: {sesi.peran or '(tanpa login)'}",
            f"TUJUAN UJI: {sesi.data['tujuan']}",
            f"PUTARAN: {k} dari maksimal {atur['maks_putaran']}; ambang keyakinan untuk langsung membuat laporan: {atur['ambang_yakin']}",
            "", "METODE PENGUKURAN: " + eks.CATATAN_METODE + " " + CATATAN_FRAME]
    for run in peta["runs"]:
        rencana = run["ringkasan"]
        if run.get("jelajah"):
            teks += ["", f"PENJELAJAH OTOMATIS {run['id']} (tanpa LLM: mengetuk semua elemen yang bisa dijangkau tanpa mengubah "
                         f"data; {ringkas_izin(rencana)}):", json.dumps(ringkas_jelajah(run, peta), ensure_ascii=False)]
            continue
        teks += ["", f"LANGKAH UJI {run['id']} - judul '{rencana.get('judul')}', tujuan: {rencana.get('tujuan') or '-'}; "
                 f"berhasil={rencana.get('berhasil')}, rekaman={'ada' if rencana.get('rekaman') else 'tidak ada'}; "
                 f"{ringkas_izin(rencana)}:",
                 json.dumps(tabel_langkah(run, peta["ref_layar"]), ensure_ascii=False)]
    cak = cakupan_elemen(peta)
    teks += ["", f"CAKUPAN ELEMEN: {cak['sudah']} dari {cak['total']} elemen yang bisa diklik sudah dijelajah "
                 f"({cak['diwakili']} diwakili item sejenis) di {cak['jumlah_layar']} layar"
             + (f"; belum: {json.dumps(cak['belum'][:25], ensure_ascii=False)}" if cak["belum"] else "")
             + (f"; tombol pengubah data (tidak diketuk penjelajah): {json.dumps([x['label'] for x in cak['aksi_data']][:25], ensure_ascii=False)}"
                if cak["aksi_data"] else "")]
    log_app = log_galat_sesi(peta)
    if log_app:
        teks += ["", "LOG ERROR APLIKASI (logcat; crash, ANR, galat Flutter/Java yang terjadi saat langkah tertentu):",
                 json.dumps(log_app, ensure_ascii=False)]
    if peta.get("luar"):
        teks += ["", "LANGKAH YANG BERAKHIR DI LUAR APLIKASI (layar utama HP/aplikasi lain; tidak dinilai sebagai layar "
                     "aplikasi): " + json.dumps(peta["luar"], ensure_ascii=False)]
    perubahan = perubahan_sesi(sesi)
    if perubahan:
        teks += ["", "PERUBAHAN DATA OLEH UJI (CRUD) DAN STATUS PEMULIHANNYA:",
                 json.dumps([{x: e.get(x) for x in ("ref", "jenis", "objek", "penanda", "status_pulih")} for e in perubahan],
                            ensure_ascii=False)]
    tugas_ot = sesi.data.get("daftar_uji") or []
    if tugas_ot:
        teks += ["", "DAFTAR UJI MENDALAM (overtesting) DAN STATUSNYA:",
                 json.dumps([{x: t_.get(x) for x in ("id", "judul", "status")} for t_ in tugas_ot], ensure_ascii=False)]
    teks += ["", "HASIL TERUKUR OTOMATIS (startup, langkah >= 1 detik, langkah gagal, statistik pengukuran berulang):",
             json.dumps(ukur, ensure_ascii=False),
             "", "LAYAR UNIK DAN MASALAH OTOMATIS:",
             json.dumps([ringkas_layar(s) for s in peta["layar"]], ensure_ascii=False)]
    if ev_lama:
        teks += ["", "EVALUASI SEBELUMNYA (perbarui):",
                 json.dumps({x: ev_lama.get(x) for x in ("keyakinan", "temuan", "hal_positif", "cakupan", "permintaan_uji_ulang")},
                            ensure_ascii=False)]
    teks += ["", "Nilai semua data di atas dan gambar berikut. Keluarkan HANYA objek JSON sesuai format."]
    isi = [{"type": "text", "text": "\n".join(teks)}]

    dikirim = []
    if conf.get("kirim_gambar", True):
        maks, lebar, n = int(conf["maks_gambar_evaluator"]), int(conf["lebar_gambar_evaluator"]), 0
        dinilai = set(sesi.data.get("layar_dinilai", []))
        ada = [s for s in peta["layar"] if s["screenshot"] and s["screenshot"].exists()]
        # layar yang belum pernah dilihat evaluator didahulukan, supaya semua layar dinilai visual (cacat visual/estetika)
        baru = [s for s in ada if s["id"] not in dinilai and (s["run"] in run_baru or atur.get("mode") == "overtesting")]
        baru += [s for s in ada if s["run"] in run_baru and s not in baru]
        lama = [s for s in ada if s not in baru]
        antre = [("asli", s) for s in baru[:max(8, maks - 8)]]
        antre += [("anotasi", s) for s in baru if masalah_berkode(s)][:4]
        frame = []
        for run in peta["runs"]:
            if run["id"] in run_baru:
                frame += frame_lambat(run, folder / "frame", ctx["sensor"])
        antre += [("frame", f) for f in frame[:4]]
        antre += [("asli", s) for s in lama]
        for jenis, x in antre:
            if n >= maks:
                break
            if jenis == "frame":
                isi += [{"type": "text", "text": f"Frame rekaman saat langkah {x['ref']} '{x['langkah']}' (total tunggu "
                                                 f"{x['waktu_ms']} ms), {x['detik_setelah_aksi']} detik setelah aksi:"},
                        {"type": "image_url", "image_url": {"url": gambar_url(x["file"], lebar, tutup=x.get("tutup"),
                                                                              lebar_asal=x.get("lebar_asal"))}}]
            elif jenis == "anotasi":
                kotak = [(m["kode"], m["bounds"], WARNA_MASALAH[m["kode"][0]]) for m in masalah_berkode(x)]
                isi += [{"type": "text", "text": f"Layar {x['id']} ({x['judul']}) BERANOTASI otomatis: kotak berkode = "
                                                 "daftar masalah layar ini (A merah, T oranye, H kuning, B ungu, K biru, "
                                                 "D abu, E merah muda, O sian, U hijau tua):"},
                        {"type": "image_url", "image_url": {"url": gambar_url(x["screenshot"], lebar, kotak,
                                                                              tutup=kotak_rahasia(x["elemen"], ctx["sensor"]))}}]
            else:
                isi += [{"type": "text", "text": f"Screenshot layar {x['id']} ({x['judul']}), langkah {', '.join(x['refs'][:4])}:"},
                        {"type": "image_url", "image_url": {"url": gambar_url(x["screenshot"], lebar,
                                                                              tutup=kotak_rahasia(x["elemen"], ctx["sensor"]))}}]
                dikirim.append(x["id"])
            n += 1

    pesan = [{"role": "system", "content": baca_teks(FILE_PROMPT_EVALUATOR)}, {"role": "user", "content": isi}]
    for coba in range(1, 3):
        jawaban, model = ctx["klien"].chat(pesan, "evaluator", f"evaluator_{coba}", folder)
        try:
            ev = rapikan_evaluasi(ambil_json(jawaban), peta)
            break
        except ValueError as e:
            log(f"  jawaban evaluator tidak bisa dibaca (coba {coba}): {e}")
            pesan += [{"role": "assistant", "content": jawaban},
                      {"role": "user", "content": f"Jawabanmu tidak bisa dibaca sebagai JSON ({e}). Kirim ulang HANYA objek JSON."}]
    else:
        raise GagalOrkestrasi("LLM Evaluator tidak mengembalikan JSON yang valid")

    # Keputusan akhir ditetapkan kode, bukan hanya oleh LLM.
    ambang, maks_putaran = float(atur["ambang_yakin"]), int(atur["maks_putaran"])
    run_ini = next((r for r in peta["runs"] if r["id"] == f"P{k}"), None)
    gagal = next((lk for lk in run_ini["ringkasan"].get("langkah", []) if not lk["berhasil"]), None) if run_ini else None
    if not ev["permintaan_uji_ulang"] and ev["keyakinan"] < ambang and gagal:
        ev["permintaan_uji_ulang"].append({"jenis": "perbaiki_langkah_gagal", "alasan": "ditambahkan otomatis",
                                           "tugas": f"Langkah P{k}.{gagal['no']} '{gagal['langkah']}' gagal ({gagal.get('galat')}); "
                                                    "susun ulang langkah itu memakai elemen yang benar-benar ada di peta layar."})
    jelajah = bool(atur.get("jelajah_menyeluruh"))
    berikut = "rencana"
    if atur.get("mode") == "overtesting":
        lanjut, alasan, berikut = keputusan_overtesting(sesi, k, peta, cak, ev, dikirim)
    elif k >= maks_putaran and (ev["keyakinan"] < ambang or (jelajah and cak["belum"])):
        alasan = (f"putaran maksimal ({maks_putaran}) tercapai; keyakinan {ev['keyakinan']:.2f}"
                  + (f", {len(cak['belum'])} dari {cak['total']} elemen belum dijelajah" if jelajah and cak["belum"] else ""))
        lanjut = False
    elif jelajah and cak["belum"]:
        if not any(x["jenis"] == "eksplorasi" for x in ev["permintaan_uji_ulang"]):
            ev["permintaan_uji_ulang"].append({
                "jenis": "eksplorasi", "alasan": "mode jelajah menyeluruh (ditambahkan otomatis)",
                "tugas": "Jelajahi elemen yang belum pernah diketuk: "
                         + "; ".join(f"{x['layar']} '{x['label']}'" for x in cak["belum"][:12])})
        alasan = (f"mode jelajah menyeluruh: {len(cak['belum'])} dari {cak['total']} elemen belum dijelajah "
                  f"(keyakinan {ev['keyakinan']:.2f})")
        lanjut = True
    elif ev["keyakinan"] >= ambang:
        alasan = f"keyakinan {ev['keyakinan']:.2f} >= ambang {ambang}" + (
            f"; semua {cak['total']} elemen sudah dijelajah" if jelajah and cak["total"] else "")
        lanjut = False
    elif not ev["permintaan_uji_ulang"]:
        alasan = f"keyakinan {ev['keyakinan']:.2f} < ambang {ambang}, tetapi evaluator tidak punya permintaan uji ulang"
        lanjut = False
    else:
        alasan = f"keyakinan {ev['keyakinan']:.2f} < ambang {ambang}; {len(ev['permintaan_uji_ulang'])} permintaan uji ulang"
        lanjut = True
    ev["putaran_berikut"] = berikut if lanjut else None
    ev["_layar_dikirim"] = dikirim
    ev.update({"lanjut": lanjut, "alasan_keputusan": alasan, "putaran": k, "model": model, "waktu": sekarang(),
               "cakupan_elemen": {"total": cak["total"], "sudah": cak["sudah"], "belum": len(cak["belum"]),
                                  "diwakili": cak["diwakili"], "aksi_data": len(cak["aksi_data"]),
                                  "tak_terjangkau": len(cak["tak_terjangkau"]), "layar": cak["jumlah_layar"]}})
    return ev


def menit_berjalan(sesi):
    try:
        return (datetime.now() - datetime.fromisoformat(sesi.data["dibuat"])).total_seconds() / 60
    except (KeyError, ValueError):
        return 0.0


def keputusan_overtesting(sesi, k, peta, cak, ev, dikirim):
    """Mode overtesting: lanjut sampai semua elemen dijelajah, semua tugas uji mendalam dikerjakan, semua layar dilihat
    evaluator, dan semua data uji dipulihkan; berhenti kalau putaran/waktu habis atau 3 putaran tanpa kemajuan."""
    atur = sesi.atur
    tugas = susun_daftar_uji(sesi, peta, cak)
    tugas_belum = [x for x in tugas if x["status"] == "belum"]
    dinilai = set(sesi.data.get("layar_dinilai", [])) | set(dikirim)
    belum_dilihat = [s["id"] for s in peta["layar"] if s["screenshot"] and s["id"] not in dinilai and layar_bisa_dijelajah(s)]
    pulih = belum_pulih(sesi)
    kemajuan = [cak["sudah"], len(tugas) - len(tugas_belum), len(dinilai), len(perubahan_sesi(sesi))]
    riwayat = [p.get("kemajuan") for p in sesi.putaran[:-1] if p.get("kemajuan")]
    mandek = len(riwayat) >= 2 and riwayat[-1] == riwayat[-2] == kemajuan
    batas = float(atur.get("batas_menit_sesi") or 0)
    habis = k >= int(atur["maks_putaran"]) or (batas and menit_berjalan(sesi) >= batas) or mandek
    sisa = []
    if cak["belum"]:
        sisa.append(f"{len(cak['belum'])} elemen belum dijelajah")
        if not any(x["jenis"] == "eksplorasi" for x in ev["permintaan_uji_ulang"]):
            ev["permintaan_uji_ulang"].append({"jenis": "eksplorasi", "alasan": "overtesting (ditambahkan otomatis)",
                                               "tugas": "Jelajahi elemen yang belum diketuk (ikuti jalurnya): " + "; ".join(
                                                   f"{x['layar']} '{x['label']}'" for x in cak["belum"][:12])})
    if tugas_belum:
        sisa.append(f"{len(tugas_belum)} tugas uji mendalam belum dikerjakan")
        ev["permintaan_uji_ulang"].append({"jenis": "overtesting", "alasan": "daftar uji mendalam (ditambahkan otomatis)",
                                           "tugas": "Kerjakan tugas: " + "; ".join(x["id"] for x in tugas_belum[:6])})
    if belum_dilihat:
        sisa.append(f"{len(belum_dilihat)} layar belum dinilai visual")
    ev["kemajuan"] = kemajuan
    if pulih and (habis or not sisa):
        if sesi.data.get("putaran_pemulihan", 0) < 2:
            return True, f"{len(pulih)} perubahan data belum dipulihkan: putaran pemulihan", "pemulihan"
        return False, f"berhenti; {len(pulih)} perubahan data GAGAL dipulihkan otomatis (lihat laporan)", None
    if habis:
        alasan = ("3 putaran tanpa kemajuan" if mandek else "batas waktu sesi tercapai"
                  if batas and menit_berjalan(sesi) >= batas else f"putaran maksimal ({atur['maks_putaran']}) tercapai")
        return False, alasan + (f"; sisa: {', '.join(sisa)}" if sisa else ""), None
    if sisa:
        return True, "overtesting berlanjut: " + ", ".join(sisa), "rencana"
    return False, (f"overtesting selesai: {cak['sudah']}/{cak['total']} elemen, {len(tugas)} tugas uji mendalam, "
                   f"{len(dinilai)} layar dinilai, semua data uji dipulihkan"), None


# ====================================================================== perintah sesi
def tentukan_skenario(args, ctx):
    pengaturan, daftar = ctx["pengaturan"], ctx["skenario"]
    if getattr(args, "skenario", None) is not None:
        if not 0 <= args.skenario < len(daftar):
            raise GagalOrkestrasi(f"skenario {args.skenario} tidak ada (ada {len(daftar)} skenario di {FILE_SKENARIO.name})")
        sk = daftar[args.skenario]
        app, peran, tujuan = sk.get("aplikasi"), sk.get("peran"), sk.get("tujuan")
        atur = {**pengaturan, **{k: sk[k] for k in PENGATURAN_BAWAAN if k in sk}}
    else:
        sk = {}
        app, peran, atur = args.app, args.peran, dict(pengaturan)
        tujuan = args.tujuan or (base64.b64decode(args.tujuan_b64).decode("utf-8") if args.tujuan_b64 else None)
    for k, attr in (("maks_putaran", "maks_putaran"), ("ambang_yakin", "ambang")):
        if getattr(args, attr, None) is not None:
            atur[k] = getattr(args, attr)
    if getattr(args, "tanpa_rekam", False):
        atur["rekam"] = False
    if getattr(args, "jelajah", False):
        atur["jelajah_menyeluruh"] = True
    if getattr(args, "tanpa_jelajah", False):
        atur["jelajah_menyeluruh"] = False
    if getattr(args, "izinkan_berisiko", False):
        atur["boleh_ubah_data"] = True
    if getattr(args, "overtesting", False):
        atur["mode"] = "overtesting"
    if getattr(args, "batas_menit", None) is not None:
        atur["batas_menit_sesi"] = args.batas_menit
    if atur.get("mode") == "overtesting":
        # overtesting = penjelajah otomatis di putaran 1, kecuali pengguna mematikannya sendiri di skenario.json
        if "jelajah_otomatis" not in set(ctx.get("pengaturan_file") or {}) | set(sk):
            atur["jelajah_otomatis"] = True
        atur["jelajah_menyeluruh"] = True
    apps = ctx["cfg"].get("aplikasi", {})
    if app not in apps:
        raise GagalOrkestrasi(f"aplikasi '{app}' tidak ada di akun.json (pilihan: {', '.join(apps)})")
    akun = apps[app].get("akun", {})
    if peran is None and akun:
        peran = apps[app].get("peran_default") or next(iter(akun))
    if peran is not None and peran not in akun:
        raise GagalOrkestrasi(f"peran '{peran}' tidak ada untuk {app} (pilihan: {', '.join(akun)})")
    return app, peran, (tujuan or TUJUAN_BAWAAN), atur


def cmd_mulai(args, ctx):
    app, peran, tujuan, atur = tentukan_skenario(args, ctx)
    sesi = Sesi.baru(app, peran, tujuan, atur)
    log(f"=== Sesi baru: {sesi.folder.name} (mode {atur.get('mode', 'evaluasi')})\n"
        f"--- Observasi awal (buka aplikasi{', login ' + peran if peran else ''}, tangkap layar)")
    if atur.get("perintah_cadangan"):
        sesi.data.setdefault("hook", {})["cadangan"] = jalankan_hook("cadangan", atur["perintah_cadangan"])
        sesi.simpan()
    langkah = [{"aksi": "buka_aplikasi", "nama": "buka_aplikasi"}]
    if peran:
        langkah.append({"aksi": "login", "nama": f"login_{peran}", "peran": peran})
    langkah += [{"aksi": "tangkap", "nama": "layar_awal"},
                {"aksi": "gulir", "nama": "gulir_bawah", "arah": "bawah", "lanjut_jika_gagal": True},
                {"aksi": "tangkap", "nama": "layar_awal_bawah"},
                {"aksi": "gulir", "nama": "gulir_atas", "arah": "atas", "lanjut_jika_gagal": True}]
    rencana = {"aplikasi": app, "peran": peran, "judul": "observasi_awal", "beri_izin_otomatis": atur["beri_izin_otomatis"],
               "tujuan": "observasi awal untuk LLM Perancang (startup, login, layar pertama)", "langkah": langkah}
    try:
        hasil = eks.jalankan_rencana(rencana, ctx["cfg"], rekam=atur["rekam"], folder=sesi.folder / "observasi",
                                     izinkan_berisiko=atur["boleh_ubah_data"])
    except eks.Gagal as ex:
        sesi.data.update({"status": "error", "galat": str(ex)})
        sesi.simpan()
        raise
    sesi.data["observasi"] = {k: hasil.get(k) for k in ("status", "berhasil", "langkah_gagal", "rekaman")}
    if hasil["status"] == "gagal_langkah" and str(hasil["langkah_gagal"]["langkah"]).startswith(("buka", "login")):
        sesi.data["status"] = "error"
        sesi.simpan()
        g = hasil["langkah_gagal"]
        raise GagalOrkestrasi(f"observasi gagal di langkah '{g['langkah']}': {g['galat']} "
                              f"(lihat {sesi.folder / 'observasi' / (g.get('screenshot') or '')})")
    sesi.data["status"] = "observasi_selesai"
    sesi.simpan()
    return keluaran(sesi, aplikasi=app, peran=peran, lanjut=True)


def cmd_rancang(args, ctx):
    sesi = Sesi(args.sesi)
    p = sesi.terakhir()
    if p is None:
        if not sesi.data.get("observasi"):
            raise GagalOrkestrasi("observasi awal belum ada; jalankan perintah 'mulai' dulu")
        k = 1
    elif p["status"] == "rencana_siap":
        k = p["ke"]  # rancang ulang putaran yang belum dijalankan
    elif p["status"] == "dievaluasi":
        if not p.get("lanjut") and not getattr(args, "paksa", False):
            raise GagalOrkestrasi("evaluator tidak meminta uji ulang; lanjutkan ke 'laporan' (atau pakai --paksa)")
        k = p["ke"] + 1
    else:
        raise GagalOrkestrasi(f"putaran {p['ke']} berstatus '{p['status']}'; jalankan 'jalankan'/'evaluasi' dulu")
    ev = sesi.evaluasi_terakhir() if k > 1 else None
    atur = sesi.atur
    if k == 1 and atur.get("jelajah_otomatis"):
        jenis = "jelajah"
    else:
        jenis = (ev or {}).get("putaran_berikut") or "rencana"
    if k > int(atur["maks_putaran"]) and jenis != "pemulihan" and not getattr(args, "paksa", False):
        raise GagalOrkestrasi(f"putaran maksimal ({atur['maks_putaran']}) tercapai")
    if jenis == "jelajah":
        log(f"--- Putaran {k}: penjelajah otomatis (tanpa LLM) akan mengetuk semua elemen yang bisa dijangkau")
        rencana = {"jenis": "jelajah_otomatis", "aplikasi": sesi.app, "peran": sesi.peran, "judul": f"jelajah_otomatis_{k}",
                   "tujuan": "penjelajah otomatis: ketuk semua elemen yang bisa dijangkau tanpa mengubah data",
                   "batas_ketukan": int(atur["batas_ketukan_jelajah"]), "batas_menit": float(atur["batas_menit_jelajah"]),
                   "beri_izin_otomatis": atur["beri_izin_otomatis"]}
        model = "penjelajah_otomatis"
    else:
        log(f"--- Putaran {k}: LLM Perancang menyusun rencana" + (" PEMULIHAN DATA" if jenis == "pemulihan" else ""))
        rencana, model = agen_perancang(sesi, k, ctx, jenis)
    if jenis == "pemulihan":
        sesi.data["putaran_pemulihan"] = sesi.data.get("putaran_pemulihan", 0) + 1
    file_rencana = f"putaran_{k}/rencana.json"
    tulis_json(sesi.folder / file_rencana, rencana)
    data = {"ke": k, "jenis": jenis, "status": "rencana_siap", "rencana": file_rencana, "folder_uji": f"putaran_{k}/uji",
            "model_perancang": model, "dibuat": sekarang(),
            "permintaan": (ev or {}).get("permintaan_uji_ulang", []) if k > 1 else []}
    if p is not None and p["ke"] == k:
        sesi.putaran[-1] = data
    else:
        sesi.putaran.append(data)
    sesi.data["status"] = "berjalan"
    sesi.simpan()
    n = len(rencana.get("langkah") or [])
    log(f"  rencana '{rencana.get('judul')}' ({n} langkah) -> {file_rencana}" if jenis != "jelajah" else
        f"  penjelajah otomatis siap (maks. {rencana['batas_ketukan']} ketukan / {rencana['batas_menit']:g} menit)")
    return keluaran(sesi, putaran=k, jenis=jenis, judul=rencana.get("judul"), jumlah_langkah=n, lanjut=True)


def cmd_jalankan(args, ctx):
    sesi = Sesi(args.sesi)
    p = sesi.terakhir()
    ulang = getattr(args, "ulang", False)
    if p is None or p["status"] not in (("rencana_siap", "diuji", "dievaluasi") if ulang else ("rencana_siap",)):
        raise GagalOrkestrasi("tidak ada rencana yang siap dijalankan; jalankan 'rancang' dulu")
    if ulang and p["status"] != "rencana_siap":
        p["folder_uji"] = f"putaran_{p['ke']}/uji_{datetime.now():%H%M%S}"
    log(f"--- Putaran {p['ke']}: pelaksana menjalankan rencana di HP")
    rencana = baca_json(sesi.folder / p["rencana"])
    atur = sesi.atur
    rencana["beri_izin_otomatis"] = atur["beri_izin_otomatis"]
    if rencana.get("jenis") == "jelajah_otomatis":
        hasil = eks.jalankan_jelajah(rencana, ctx["cfg"], rekam=atur["rekam"], folder=sesi.folder / p["folder_uji"])
    else:
        rencana["penanda_sesi"] = penanda_sesi(sesi)
        hasil = eks.jalankan_rencana(rencana, ctx["cfg"], rekam=atur["rekam"], folder=sesi.folder / p["folder_uji"],
                                     izinkan_berisiko=atur["boleh_ubah_data"])
    if hasil["status"] == "rencana_tidak_valid":
        p["status"] = "rencana_ditolak"
        sesi.simpan()
        raise GagalOrkestrasi("rencana ditolak pelaksana: " + "; ".join(hasil.get("kesalahan", [])))
    r = baca_json(Path(hasil["ringkasan"])) or {}
    if r.get("penanda"):
        sesi.data.setdefault("penanda", []).append(r["penanda"])
    ditandai = set(rencana.get("tugas") or [])
    for t_ in sesi.data.get("daftar_uji", []):
        if t_["id"] in ditandai and t_["status"] == "belum":
            t_.update(status="dikerjakan", putaran=p["ke"])
    p.update({"status": "diuji", "hasil_uji": {k: hasil.get(k) for k in ("status", "berhasil", "jumlah_langkah", "jumlah_gagal",
                                                                          "langkah_gagal", "rekaman", "jelajah")}})
    p.pop("evaluasi", None)
    sesi.simpan()
    return keluaran(sesi, putaran=p["ke"], berhasil=hasil["berhasil"], status_uji=hasil["status"],
                    jumlah_langkah=hasil["jumlah_langkah"], langkah_gagal=hasil.get("langkah_gagal"), lanjut=True)


def cmd_evaluasi(args, ctx):
    sesi = Sesi(args.sesi)
    p = sesi.terakhir()
    if p is None or p["status"] not in ("diuji", "dievaluasi"):
        raise GagalOrkestrasi("belum ada hasil uji untuk dievaluasi; jalankan 'jalankan' dulu")
    if p["status"] == "dievaluasi":
        p.pop("evaluasi", None)  # evaluasi ulang putaran yang sama
    log(f"--- Putaran {p['ke']}: LLM Evaluator menilai hasil")
    ev = agen_evaluator(sesi, p["ke"], ctx)
    sesi.data["layar_dinilai"] = sorted(set(sesi.data.get("layar_dinilai", [])) | set(ev.pop("_layar_dikirim", [])))
    p["kemajuan"] = ev.get("kemajuan")
    file_ev = f"putaran_{p['ke']}/evaluasi.json"
    tulis_json(sesi.folder / file_ev, ev)
    p.update({"status": "dievaluasi", "evaluasi": file_ev, "keyakinan": ev["keyakinan"], "lanjut": ev["lanjut"],
              "alasan_keputusan": ev["alasan_keputusan"]})
    sesi.data["status"] = "perlu_uji_ulang" if ev["lanjut"] else "siap_laporan"
    sesi.simpan()
    log(f"  keyakinan {ev['keyakinan']:.2f}, {len(ev['temuan'])} temuan -> "
        + ("UJI ULANG: " + " | ".join(x["tugas"][:80] for x in ev["permintaan_uji_ulang"]) if ev["lanjut"] else "LAPORAN"))
    c = ev.get("cakupan_elemen") or {}
    return keluaran(sesi, putaran=p["ke"], keyakinan=ev["keyakinan"], lanjut=ev["lanjut"],
                    alasan=ev["alasan_keputusan"], jumlah_temuan=len(ev["temuan"]),
                    cakupan=f"{c.get('sudah', 0)}/{c.get('total', 0)}",
                    permintaan=[x["tugas"] for x in ev["permintaan_uji_ulang"]] if ev["lanjut"] else [])


# ====================================================================== laporan
def _ref_ke_gambar(peta, ref=None, layar=None, kode=None):
    """Layar untuk sebuah bukti: utamakan kode/layar (mis. 'L2-A1'), baru ref langkah (mis. 'P1.3')."""
    if not layar and kode:
        m = re.match(r"(L\d+)", str(kode))
        layar = m.group(1) if m else None
    s = next((x for x in peta["layar"] if x["id"] == layar), None) if layar else None
    if s is None and ref in peta["ref_layar"]:
        s = next((x for x in peta["layar"] if x["id"] == peta["ref_layar"][ref]), None)
    return s


def data_laporan(sesi, ctx):
    peta = bangun_peta(sesi, kontras=True)
    ev = sesi.evaluasi_terakhir()
    if not ev:
        raise GagalOrkestrasi("belum ada evaluasi; jalankan 'evaluasi' dulu")
    ukur = pengukuran(peta)
    jumlah = {t: sum(1 for x in ev["temuan"] if x["tingkat"] == t) for t in TINGKAT}
    kelompok = [{"nama": nama, "jumlah": sum(1 for x in ev["temuan"] if x["kategori"] in kat)}
                for nama, kat in KELOMPOK_KATEGORI]
    layar = []
    for s in peta["layar"]:
        m, masalah = s["metrik"], masalah_berkode(s)
        layar.append({"id": s["id"], "judul": s["judul"], "ref": s["refs"], "jumlah_node": m.get("jumlah_node"),
                      "kedalaman_maks": m.get("kedalaman_maks"), "elemen_interaktif": m.get("elemen_interaktif"),
                      "rasio_interaktif_berlabel": m.get("rasio_interaktif_berlabel"),
                      **{f"jumlah_{j}": sum(1 for x in masalah if x["kode"][0] == h)
                         for j, h in (("tanpa_label", "A"), ("target_kecil", "T"), ("hanya_hint", "H"),
                                      ("tombol_tidak_bisa_diklik", "B"), ("kontras_rendah", "K"), ("duplikat", "D"),
                                      ("teks_janggal", "E"), ("bertumpuk", "O"), ("tak_terjangkau", "U"))},
                      "masalah": [{k: v for k, v in x.items() if k != "bounds" and v is not None} for x in masalah]})
    langkah, waktu_tap, waktu_ubah = [], [], []
    for run in peta["runs"]:
        jelajah = bool(run.get("jelajah"))
        for lk in run["ringkasan"].get("langkah", []):
            w = waktu_langkah(lk)
            if lk.get("aksi") not in AKSI_DINILAI_WAKTU:
                continue
            if lk.get("aksi") == "tap" and lk.get("berhasil") and lk.get("sampai_stabil_ms") is not None:
                waktu_tap.append(lk["sampai_stabil_ms"])
                if lk.get("mulai_berubah_ms") is not None:
                    waktu_ubah.append(lk["mulai_berubah_ms"])
            lambat = (w or 0) >= BATAS_JEDA_MS
            if jelajah and lk.get("berhasil") and not lambat:
                continue  # run penjelajah: hanya langkah lambat/gagal yang ditabelkan
            if w is not None or not lk.get("berhasil"):
                langkah.append({"ref": f"{run['id']}.{lk['no']}", "langkah": lk["langkah"], "aksi": lk.get("aksi"),
                                "berhasil": lk.get("berhasil"), "waktu_ms": w, "mulai_berubah_ms": lk.get("mulai_berubah_ms"),
                                "tanpa_perubahan": lk.get("tanpa_perubahan"),
                                "jenis_waktu": "respon" if lk.get("respon_ms") is not None else "sampai_stabil",
                                "galat": lk.get("galat")})

    def ringkas_waktu(v):
        if not v:
            return None
        v = sorted(v)
        return {"n": len(v), "median_ms": round(statistics.median(v)), "p90_ms": v[min(len(v) - 1, int(len(v) * 0.9))],
                "maks_ms": v[-1]}
    riwayat = []
    for p in sesi.putaran:
        r = baca_json(sesi.folder / p["rencana"]) or {}
        hu = p.get("hasil_uji") or {}
        riwayat.append({"putaran": p["ke"], "jenis": p.get("jenis", "rencana"), "judul": r.get("judul"), "tujuan": r.get("tujuan"),
                        "jumlah_langkah": hu.get("jumlah_langkah") or len(r.get("langkah", []) or []),
                        "berhasil": hu.get("berhasil"), "jumlah_gagal": hu.get("jumlah_gagal"),
                        "keyakinan": p.get("keyakinan"), "keputusan": p.get("alasan_keputusan"),
                        "tugas": r.get("tugas") or [], "permintaan_sebelumnya": [x.get("tugas") for x in p.get("permintaan") or []]})
    cak = cakupan_elemen(peta)
    diuji_crud = set()
    for run in peta["runs"]:
        rr = baca_json(run["folder"] / "rencana.json") or {}
        for st in (rr.get("langkah") or []) + (rr.get("pemulihan") or []):
            if st.get("ubah_data") and isinstance(st.get("target"), dict):
                diuji_crud.add(str(st["target"].get("label") or st["target"].get("label_mengandung") or "").lower())
    aksi_data = [{**x, "diuji_crud": any(d and (d in x["label"].lower() or x["label"].lower() in d) for d in diuji_crud)}
                 for x in cak["aksi_data"]]
    rekaman = [str(r["folder"] / r["ringkasan"]["rekaman"]) for r in peta["runs"] if r["ringkasan"].get("rekaman")]
    info = info_aplikasi(sesi, ctx["cfg"], peta)
    atur = sesi.atur
    overtesting = atur.get("mode") == "overtesting"
    lap = {"judul": f"Laporan {'Overtesting' if overtesting else 'Evaluasi'} UI/UX - {info['nama']} (peran: {sesi.peran or '-'})",
           "dibuat": sekarang(), "sesi": sesi.folder.name, "aplikasi": info, "peran": sesi.peran, "mode": atur.get("mode"),
           "boleh_ubah_data": bool(atur["boleh_ubah_data"]),
           "tujuan": sesi.data["tujuan"], "jumlah_putaran": len(sesi.putaran), "keyakinan": ev["keyakinan"],
           "ambang_yakin": atur["ambang_yakin"], "alasan_keputusan": ev.get("alasan_keputusan"),
           "durasi_menit": round(menit_berjalan(sesi)),
           "ringkasan_eksekutif": ev.get("ringkasan_eksekutif"), "jumlah_temuan": jumlah, "kelompok_temuan": kelompok,
           "temuan": ev["temuan"], "hal_positif": ev.get("hal_positif", []), "cakupan": ev.get("cakupan", {}),
           "cakupan_elemen": {**{k: v for k, v in cak.items() if k not in ("belum", "aksi_data", "tak_terjangkau")},
                              "belum": cak["belum"][:60], "aksi_data": aksi_data, "tak_terjangkau": cak["tak_terjangkau"]},
           "daftar_uji": sesi.data.get("daftar_uji") or [], "log_galat": log_galat_sesi(peta, maks=100),
           "perubahan_data": perubahan_sesi(sesi), "hook": sesi.data.get("hook") or {},
           "layar_di_luar_aplikasi": peta.get("luar") or {},
           "pengukuran": {**ukur, "langkah": langkah, "layar": layar,
                          "ringkasan_tap": {"sampai_stabil": ringkas_waktu(waktu_tap), "mulai_berubah": ringkas_waktu(waktu_ubah)}},
           "riwayat": riwayat, "rekaman": rekaman,
           "model": {"perancang": ctx["llm"]["model_perancang"], "evaluator": ev.get("model")},
           "metode": [eks.CATATAN_METODE, CATATAN_FRAME,
                      "Penjelajah otomatis (tanpa LLM) mengetuk setiap elemen yang bisa dijangkau di setiap layar, menggulir "
                      "untuk menemukan elemen di bawah, dan kembali lewat Back/tab semula. Item daftar yang sejenis diwakili "
                      "beberapa contoh bila contoh-contoh itu menuju layar yang sama. Tombol pengubah data tidak diketuk "
                      "penjelajah; tombol itu diuji LLM Perancang lewat CRUD berpenanda.",
                      "Ketukan hanya diarahkan ke bagian elemen yang terlihat (bukan bilah status/navigasi HP, papan ketik, atau "
                      "elemen yang digambar di atasnya); elemen yang tertutup digulir dulu ke area terlihat.",
                      f"Kontras dihitung otomatis dari piksel screenshot (perkiraan, galat sekitar 0,2) dan hanya dilaporkan di "
                      f"bawah {BATAS_KONTRAS_LAPOR}:1; elemen yang terpotong/tertutup dan tinta setipis garis tidak diukur. "
                      "Ukuran area sentuh tidak dinilai untuk elemen yang terpotong tepi layar/kontainer gulir.",
                      ("CRUD diizinkan: data buatan uji memuat penanda unik (UJI-...), hapus/ubah status hanya pada data "
                       "berpenanda, isian lama dikembalikan, dan setiap perubahan dicatat beserta status pemulihannya."
                       if atur["boleh_ubah_data"] else "Data tidak diubah: tombol simpan/kirim/hapus/setujui diblokir."),
                      ("Izin aplikasi diberikan otomatis lewat adb sebelum uji supaya fitur lanjutan bisa diuji; alur "
                       "permintaan izin aplikasi tidak dinilai. Kalau aplikasi tetap membuka Pengaturan sistem, skrip "
                       "menyalakan izinnya dan langkah itu ditandai." if atur["beri_izin_otomatis"] else
                       "Izin aplikasi tidak diberikan otomatis, jadi alur permintaan izin aplikasi ikut dinilai; "
                       "halaman Pengaturan sistem yang dibuka aplikasi ditangani skrip dan ditandai."),
                      "Temuan ditulis oleh LLM Evaluator berdasarkan data di atas; verifikasi temuan penting secara "
                      "manual (misalnya dengan TalkBack dan Accessibility Scanner) sebelum dijadikan keputusan.",
                      "Data akun tidak dikirim ke LLM: teks disensor, kolom/teks berisi data akun ditutup pada gambar, dan "
                      "token/email di log disamarkan."]}
    return lap, peta


def _ya(v):
    return "-" if v is None else ("ya" if v else "tidak")


def _md_tabel(kolom, baris):
    if not baris:
        return "_(tidak ada)_\n"
    garis = ["| " + " | ".join(kolom) + " |", "|" + "---|" * len(kolom)]
    garis += ["| " + " | ".join(str(x if x is not None else "-").replace("|", "/").replace("\n", " ") for x in b) + " |"
              for b in baris]
    return "\n".join(garis) + "\n"


def _status_waktu(x):
    if not x["berhasil"]:
        return "GAGAL: " + str(x["galat"])
    if x.get("tanpa_perubahan"):
        return "layar tidak berubah"
    return ">= 1 detik" if (x["waktu_ms"] or 0) >= BATAS_JEDA_MS else "ok"


def _temuan_per_kelompok(lap):
    for nama, kat in KELOMPOK_KATEGORI:
        isi = [t for t in lap["temuan"] if t["kategori"] in kat]
        if isi:
            yield nama, isi


def tulis_markdown(lap, peta, sesi):
    a = lap["aplikasi"]
    ce = lap["cakupan_elemen"]
    md = [f"# {lap['judul']}", "",
          f"- Aplikasi: {a['nama']} `{a['package']}` versi {a.get('versi') or '-'}",
          f"- Perangkat: {a.get('perangkat') or '-'} (Android {a.get('android') or '-'}, {a.get('dpi') or '-'} dpi)",
          f"- Dibuat: {lap['dibuat']} - sesi `{lap['sesi']}` - {lap['jumlah_putaran']} putaran uji, sekitar {lap['durasi_menit']} menit"
          f" - mode {lap.get('mode') or 'evaluasi'}",
          f"- Keyakinan evaluator: **{lap['keyakinan']:.2f}** (ambang {lap['ambang_yakin']}) - {lap.get('alasan_keputusan') or ''}",
          f"- Cakupan: **{ce['sudah']} dari {ce['total']}** elemen yang bisa diklik di {ce.get('jumlah_layar', '-')} layar",
          f"- Tujuan uji: {lap['tujuan']}", "", "## Ringkasan", "", lap.get("ringkasan_eksekutif") or "-", "",
          "Jumlah temuan: " + ", ".join(f"{t} {n}" for t, n in lap["jumlah_temuan"].items()), "",
          "Per jenis: " + ", ".join(f"{k['nama']} {k['jumlah']}" for k in lap["kelompok_temuan"] if k["jumlah"]), ""]
    for nama, isi in _temuan_per_kelompok(lap):
        md += [f"## {nama}", ""]
        for t in isi:
            md += [f"### {t['id']} [{t['tingkat'].upper()}] {t['judul']}", "",
                   f"*Kategori:* {t['kategori']} - *Sumber:* {t.get('sumber') or '-'}", "", t["deskripsi"], ""]
            if t.get("langkah_reproduksi"):
                md += ["*Langkah reproduksi:*", ""] + [f"{i}. {x}" for i, x in enumerate(t["langkah_reproduksi"], 1)] + [""]
            for b in t["bukti"]:
                s = _ref_ke_gambar(peta, b.get("ref"), b.get("layar"), b.get("kode"))
                gambar = f" ![{s['id']}]({os.path.relpath(s['screenshot'], sesi.folder).replace(os.sep, '/')})" if s and s["screenshot"] else ""
                md.append(f"- Bukti {b.get('kode') or b.get('ref') or b.get('layar') or ''}: {b.get('keterangan', '')}{gambar}")
            md += ["", f"*Dasar:* {t.get('dasar') or '-'}", "", f"**Rekomendasi:** {t.get('rekomendasi') or '-'}", ""]
    if lap["hal_positif"]:
        md += ["## Hal yang sudah baik", ""] + [f"- {x}" for x in lap["hal_positif"]] + [""]
    md += ["## Cakupan fitur", "",
           f"Elemen yang bisa diklik: **{ce['sudah']} dari {ce['total']}** sudah dijelajah ({ce.get('diwakili', 0)} diwakili "
           f"item sejenis), di {ce.get('jumlah_layar', '-')} layar aplikasi.", ""]
    if ce["belum"]:
        md += ["Belum dijelajah: " + ", ".join(f"{x['layar']} '{x['label']}'" for x in ce["belum"][:30]) + ".", ""]
    if ce.get("tak_terjangkau"):
        md += ["Tidak terjangkau (tertutup dan tidak bisa digulir): "
               + ", ".join(f"{x['layar']} '{x['label']}'" for x in ce["tak_terjangkau"]) + ".", ""]
    if ce.get("aksi_data"):
        md += ["Tombol pengubah data: " + ", ".join(f"{x['layar']} '{x['label']}'" + (" (diuji CRUD)" if x.get("diuji_crud") else "")
                                                  for x in ce["aksi_data"]) + ".", ""]
    if lap["daftar_uji"]:
        md += ["### Daftar uji mendalam", "",
               _md_tabel(["Tugas", "Isi", "Status"], [[x["id"], x["judul"], x["status"]] for x in lap["daftar_uji"]])]
    md += ["Sudah diuji (menurut evaluator):", ""] + [f"- {x}" for x in lap["cakupan"].get("sudah_diuji", [])] + \
          ["", "Belum diuji (menurut evaluator):", ""] + [f"- {x}" for x in lap["cakupan"].get("belum_diuji", [])] + [""]
    md += ["## Log error aplikasi", "",
           _md_tabel(["Ref", "Layar", "Jenis", "Pesan"], [[x["ref"], x.get("layar"), x["jenis"], x["pesan"][:200]] for x in lap["log_galat"]])]
    if lap["boleh_ubah_data"] or lap["perubahan_data"]:
        md += ["## Perubahan data dan pemulihan", "",
               _md_tabel(["Ref", "Jenis", "Objek", "Penanda", "Status pemulihan"],
                         [[x["ref"], x["jenis"], x.get("objek"), x.get("penanda"), x["status_pulih"]] for x in lap["perubahan_data"]])]
        for nama, hk in (lap.get("hook") or {}).items():
            md += [f"Hook {nama}: `{hk['perintah']}` -> kode keluar {hk['kode_keluar']}", ""]
    u = lap["pengukuran"]
    rt = u.get("ringkasan_tap") or {}
    md += ["## Hasil pengukuran otomatis", "", "### Startup", "",
           _md_tabel(["Ref", "TotalTime (ms)", "Jenis", "Batas Android vitals (ms)", "Berlebihan"],
                     [[x["ref"], x["total_time_ms"], x["jenis"], x["batas_vitals_ms"], "ya" if x["berlebihan"] else "tidak"]
                      for x in u["startup"]]),
           "### Ringkasan waktu tap", "",
           _md_tabel(["Ukuran", "n", "Median (ms)", "P90 (ms)", "Maks (ms)"],
                     [[nama, v["n"], v["median_ms"], v["p90_ms"], v["maks_ms"]] for nama, v in
                      (("mulai berubah", rt.get("mulai_berubah")), ("sampai stabil", rt.get("sampai_stabil"))) if v]),
           "### Waktu per langkah (run penjelajah: hanya yang lambat/gagal)", "",
           _md_tabel(["Ref", "Langkah", "Aksi", "Mulai berubah (ms)", "Waktu (ms)", "Jenis", "Status"],
                     [[x["ref"], x["langkah"], x["aksi"], x.get("mulai_berubah_ms"), x["waktu_ms"], x["jenis_waktu"], _status_waktu(x)]
                      for x in u["langkah"]])]
    if u["statistik_berulang"]:
        md += ["### Pengukuran berulang", "",
               _md_tabel(["Langkah", "n", "Median (ms)", "Min", "Maks"],
                         [[x["langkah"], x["n"], x["median_ms"], x["min_ms"], x["maks_ms"]] for x in u["statistik_berulang"]])]
    md += ["### Masalah otomatis per layar", "",
           "A tanpa label, T target <48dp, H hanya hint, B tombol tak bisa diklik, K kontras, D pembungkus kembar tanpa label, "
           "E teks janggal, O elemen bertumpuk, U tidak terjangkau.", "",
           _md_tabel(["Layar", "Judul", "Node", "Interaktif", "A", "T", "H", "B", "K", "D", "E", "O", "U"],
                     [[x["id"], x["judul"], x["jumlah_node"], x["elemen_interaktif"], x["jumlah_tanpa_label"],
                       x["jumlah_target_kecil"], x["jumlah_hanya_hint"], x["jumlah_tombol_tidak_bisa_diklik"],
                       x["jumlah_kontras_rendah"], x["jumlah_duplikat"], x["jumlah_teks_janggal"], x["jumlah_bertumpuk"],
                       x["jumlah_tak_terjangkau"]] for x in u["layar"]]),
           "## Riwayat putaran", "",
           _md_tabel(["Putaran", "Jenis", "Rencana", "Langkah", "Gagal", "Keyakinan", "Keputusan"],
                     [[r["putaran"], r["jenis"], r["judul"], r["jumlah_langkah"], r.get("jumlah_gagal"),
                       f"{r['keyakinan']:.2f}" if r["keyakinan"] is not None else "-", r["keputusan"]] for r in lap["riwayat"]]),
           "## Metode dan keterbatasan", ""] + [f"- {x}" for x in lap["metode"]] + \
          ["", f"Model: perancang `{lap['model']['perancang']}`, evaluator `{lap['model']['evaluator']}`.", ""]
    if lap["rekaman"]:
        md += ["Rekaman layar: " + ", ".join(f"`{os.path.relpath(r, sesi.folder)}`" for r in lap["rekaman"]), ""]
    return "\n".join(md)


CSS = """body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:1040px;margin:24px auto;
padding:0 16px;color:#1f2328;line-height:1.55}h1{font-size:1.6em;margin-bottom:.2em}h2{border-bottom:1px solid #d0d7de;
padding-bottom:4px;margin-top:2em}.meta{color:#57606a;font-size:.95em}table{border-collapse:collapse;width:100%;
font-size:.88em;margin:8px 0 16px}th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa}.lbl{display:inline-block;padding:1px 9px;border-radius:10px;font-size:.78em;font-weight:600;
color:#fff;vertical-align:middle}.kritis{background:#b42318}.tinggi{background:#c4320a}.sedang{background:#946200}
.rendah{background:#57606a}.kartu{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:14px 0}
.kartu h3{margin:.1em 0 .4em}.bukti{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}.bukti figure{margin:0;
width:250px}.bukti img,.galeri img{width:100%;border:1px solid #d0d7de;border-radius:4px}figcaption{font-size:.8em;
color:#57606a}.galeri{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.angka{display:flex;gap:10px;flex-wrap:wrap}.angka div{border:1px solid #d0d7de;border-radius:8px;padding:8px 14px;
min-width:110px}.angka b{font-size:1.4em;display:block}.jelek{color:#b42318;font-weight:600}.baik{color:#1a7f37;font-weight:600}"""


def tulis_html(lap, peta, sensor):
    e = html.escape
    a, u, ce = lap["aplikasi"], lap["pengukuran"], lap["cakupan_elemen"]
    gambar_cache = {}

    def img(s, anotasi=False, lebar=420):
        kunci = (s["id"], anotasi)
        if kunci not in gambar_cache:
            kotak = [(m["kode"], m["bounds"], WARNA_MASALAH[m["kode"][0]]) for m in masalah_berkode(s)] if anotasi else None
            gambar_cache[kunci] = gambar_url(s["screenshot"], lebar, kotak, kualitas=68,
                                             tutup=kotak_rahasia(s["elemen"], sensor))
        return gambar_cache[kunci]

    def tabel(kolom, baris):
        if not baris:
            return "<p><i>(tidak ada)</i></p>"
        return ("<table><tr>" + "".join(f"<th>{e(k)}</th>" for k in kolom) + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{x}</td>" for x in b) + "</tr>" for b in baris) + "</table>")

    h = [f"<!DOCTYPE html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{e(lap['judul'])}</title><style>{CSS}</style></head><body>",
         f"<h1>{e(lap['judul'])}</h1><p class='meta'>{e(a['nama'])} <code>{e(a['package'] or '')}</code> versi "
         f"{e(str(a.get('versi') or '-'))} &middot; {e(a.get('perangkat') or '-')} (Android {e(str(a.get('android') or '-'))}) "
         f"&middot; {e(lap['dibuat'])} &middot; {lap['jumlah_putaran']} putaran, sekitar {lap['durasi_menit']} menit &middot; "
         f"mode {e(str(lap.get('mode') or 'evaluasi'))} &middot; sesi {e(lap['sesi'])}</p>",
         f"<p><b>Tujuan uji:</b> {e(lap['tujuan'])}</p>", "<div class='angka'>"]
    h += [f"<div><b>{lap['keyakinan']:.2f}</b>keyakinan (ambang {lap['ambang_yakin']})</div>",
          f"<div><b>{ce['sudah']}/{ce['total']}</b>elemen dijelajah</div>",
          f"<div><b>{ce.get('jumlah_layar', '-')}</b>layar aplikasi</div>"]
    h += [f"<div><b class='{'jelek' if n and t in ('kritis', 'tinggi') else ''}'>{n}</b>temuan {t}</div>"
          for t, n in lap["jumlah_temuan"].items()]
    st = [x for x in u["startup"] if x["jenis"] == "COLD"]
    if st:
        h.append(f"<div><b>{round(statistics.median(x['total_time_ms'] for x in st))} ms</b>cold start (median)</div>")
    crash = sum(1 for x in lap["log_galat"] if x["jenis"] in ("crash", "anr"))
    h.append(f"<div><b class='{'jelek' if crash else 'baik'}'>{crash}</b>crash/ANR</div>")
    h += ["</div>", f"<h2>Ringkasan</h2><p>{e(lap.get('ringkasan_eksekutif') or '-')}</p>",
          f"<p class='meta'>{e(lap.get('alasan_keputusan') or '')}</p>",
          "<p>" + " &middot; ".join(f"{e(k['nama'])}: <b>{k['jumlah']}</b>" for k in lap["kelompok_temuan"] if k["jumlah"]) + "</p>"]
    for nama, isi in _temuan_per_kelompok(lap):
        h.append(f"<h2>{e(nama)}</h2>")
        for t in isi:
            h.append(f"<div class='kartu'><h3><span class='lbl {t['tingkat']}'>{t['tingkat'].upper()}</span> {e(t['id'])} "
                     f"{e(t['judul'])}</h3><p class='meta'>Kategori: {e(t['kategori'])} &middot; Sumber: {e(t.get('sumber') or '-')}</p>"
                     f"<p>{e(t['deskripsi'])}</p>")
            if t.get("langkah_reproduksi"):
                h.append("<p><b>Langkah reproduksi</b></p><ol>" + "".join(f"<li>{e(x)}</li>" for x in t["langkah_reproduksi"]) + "</ol>")
            h.append("<div class='bukti'>")
            sudah = set()
            for b in t["bukti"][:4]:
                s = _ref_ke_gambar(peta, b.get("ref"), b.get("layar"), b.get("kode"))
                label = " ".join(filter(None, [b.get("kode"), b.get("ref")])) or (s["id"] if s else "")
                if s and s["screenshot"] and s["screenshot"].exists() and s["id"] not in sudah:
                    sudah.add(s["id"])
                    h.append(f"<figure><img src='{img(s, anotasi=bool(masalah_berkode(s)))}' alt='Screenshot {e(s['id'])}'>"
                             f"<figcaption>{e(label)}: {e(b.get('keterangan', ''))}</figcaption></figure>")
                else:
                    h.append(f"<figure><figcaption>{e(label)}: {e(b.get('keterangan', ''))}</figcaption></figure>")
            h.append(f"</div><p><b>Dasar:</b> {e(t.get('dasar') or '-')}</p><p><b>Rekomendasi:</b> {e(t.get('rekomendasi') or '-')}</p></div>")
    if lap["hal_positif"]:
        h.append("<h2>Hal yang sudah baik</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in lap["hal_positif"]) + "</ul>")
    h.append(f"<h2>Cakupan fitur</h2><p>Elemen yang bisa diklik: <b>{ce['sudah']} dari {ce['total']}</b> sudah dijelajah "
             f"({ce.get('diwakili', 0)} diwakili item sejenis), di {ce.get('jumlah_layar', '-')} layar aplikasi.</p>")
    if ce["belum"]:
        h.append("<p><b>Belum dijelajah:</b> " + e(", ".join(f"{x['layar']} '{x['label']}'" for x in ce["belum"][:40])) + "</p>")
    if ce.get("tak_terjangkau"):
        h.append("<p><b>Tidak terjangkau</b> (tertutup, tidak bisa digulir): "
                 + e(", ".join(f"{x['layar']} '{x['label']}'" for x in ce["tak_terjangkau"])) + "</p>")
    if ce.get("aksi_data"):
        h.append("<p><b>Tombol pengubah data:</b> " + ", ".join(
            e(f"{x['layar']} '{x['label']}'") + (" <span class='baik'>(diuji CRUD)</span>" if x.get("diuji_crud") else "")
            for x in ce["aksi_data"]) + "</p>")
    if lap["daftar_uji"]:
        h.append("<h3>Daftar uji mendalam</h3>" + tabel(["Tugas", "Isi", "Status"], [
            [e(x["id"]), e(x["judul"]), f"<span class='{'baik' if x['status'] != 'belum' else 'jelek'}'>{e(x['status'])}</span>"]
            for x in lap["daftar_uji"]]))
    h.append("<h2>Log error aplikasi</h2>" + tabel(["Ref", "Layar", "Jenis", "Pesan"], [
        [e(x["ref"]), e(str(x.get("layar") or "-")), f"<span class='{'jelek' if x['jenis'] in ('crash', 'anr') else ''}'>"
         f"{e(x['jenis'])}</span>", e(x["pesan"][:300])] for x in lap["log_galat"]]))
    if lap["boleh_ubah_data"] or lap["perubahan_data"]:
        h.append("<h2>Perubahan data dan pemulihan</h2>" + tabel(["Ref", "Jenis", "Objek", "Penanda", "Status pemulihan"], [
            [e(x["ref"]), e(x["jenis"]), e(str(x.get("objek") or "-")), e(str(x.get("penanda") or "-")),
             f"<span class='{'baik' if x['status_pulih'] in ('dipulihkan', 'tidak_perlu') else 'jelek'}'>{e(x['status_pulih'])}</span>"]
            for x in lap["perubahan_data"]]))
        for nama, hk in (lap.get("hook") or {}).items():
            h.append(f"<p class='meta'>Hook {e(nama)}: <code>{e(hk['perintah'])}</code> &rarr; kode keluar {hk['kode_keluar']}</p>")
    rt = u.get("ringkasan_tap") or {}
    h.append("<h2>Hasil pengukuran otomatis</h2><h3>Startup</h3>")
    h.append(tabel(["Ref", "TotalTime (ms)", "Jenis", "Batas Android vitals (ms)", "Berlebihan"],
                   [[e(x["ref"]), x["total_time_ms"], e(x["jenis"]), x["batas_vitals_ms"],
                     "<span class='jelek'>ya</span>" if x["berlebihan"] else "tidak"] for x in u["startup"]]))
    h.append("<h3>Ringkasan waktu tap</h3>" + tabel(["Ukuran", "n", "Median (ms)", "P90 (ms)", "Maks (ms)"], [
        [nama, v["n"], v["median_ms"], v["p90_ms"], v["maks_ms"]] for nama, v in
        (("mulai berubah", rt.get("mulai_berubah")), ("sampai stabil", rt.get("sampai_stabil"))) if v]))
    h.append("<h3>Waktu per langkah</h3><p class='meta'>Run penjelajah otomatis: hanya langkah yang lambat atau gagal.</p>")
    h.append(tabel(["Ref", "Langkah", "Aksi", "Mulai berubah (ms)", "Waktu (ms)", "Jenis", "Status"],
                   [[e(x["ref"]), e(x["langkah"]), e(x["aksi"] or ""),
                     x["mulai_berubah_ms"] if x.get("mulai_berubah_ms") is not None else "-",
                     x["waktu_ms"] if x["waktu_ms"] is not None else "-", e(x["jenis_waktu"]),
                     (f"<span class='jelek'>{e(_status_waktu(x))}</span>" if _status_waktu(x) not in ("ok",) else "ok")]
                    for x in u["langkah"]]))
    if u["statistik_berulang"]:
        h.append("<h3>Pengukuran berulang</h3>" + tabel(["Langkah", "n", "Median (ms)", "Min", "Maks"],
                 [[e(x["langkah"]), x["n"], x["median_ms"], x["min_ms"], x["maks_ms"]] for x in u["statistik_berulang"]]))
    h.append("<h3>Masalah otomatis per layar</h3><p class='meta'>A tanpa label, T target &lt;48dp, H hanya hint, B tombol tak "
             "bisa diklik, K kontras, D pembungkus kembar tanpa label, E teks janggal, O elemen bertumpuk, U tidak terjangkau.</p>")
    h.append(tabel(["Layar", "Judul", "Node", "Interaktif", "A", "T", "H", "B", "K", "D", "E", "O", "U"],
                   [[e(x["id"]), e(x["judul"]), x["jumlah_node"], x["elemen_interaktif"], x["jumlah_tanpa_label"],
                     x["jumlah_target_kecil"], x["jumlah_hanya_hint"], x["jumlah_tombol_tidak_bisa_diklik"],
                     x["jumlah_kontras_rendah"], x["jumlah_duplikat"], x["jumlah_teks_janggal"], x["jumlah_bertumpuk"],
                     x["jumlah_tak_terjangkau"]] for x in u["layar"]]))
    h.append("<h3>Galeri layar (kotak = masalah otomatis)</h3><div class='galeri'>")
    for s in peta["layar"][:60]:
        if s["screenshot"] and s["screenshot"].exists():
            h.append(f"<figure style='margin:0'><img src='{img(s, anotasi=True, lebar=320)}' alt='Layar {e(s['id'])}'>"
                     f"<figcaption>{e(s['id'])} - {e(s['judul'])}</figcaption></figure>")
    h.append("</div><p><b>Sudah diuji (menurut evaluator)</b></p><ul>"
             + "".join(f"<li>{e(x)}</li>" for x in lap["cakupan"].get("sudah_diuji", []))
             + "</ul><p><b>Belum diuji (menurut evaluator)</b></p><ul>"
             + "".join(f"<li>{e(x)}</li>" for x in lap["cakupan"].get("belum_diuji", [])) + "</ul>")
    h.append("<h2>Riwayat putaran</h2>" + tabel(["Putaran", "Jenis", "Rencana", "Langkah", "Gagal", "Keyakinan", "Keputusan"],
             [[r["putaran"], e(r["jenis"]), e(str(r["judul"])), r["jumlah_langkah"], r.get("jumlah_gagal") if r.get("jumlah_gagal") is not None else "-",
               f"{r['keyakinan']:.2f}" if r["keyakinan"] is not None else "-", e(str(r["keputusan"] or ""))]
              for r in lap["riwayat"]]))
    h.append("<h2>Metode dan keterbatasan</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in lap["metode"])
             + f"</ul><p class='meta'>Model: perancang {e(str(lap['model']['perancang']))}, evaluator "
             f"{e(str(lap['model']['evaluator']))}.</p></body></html>")
    return "".join(h)


def cmd_laporan(args, ctx):
    sesi = Sesi(args.sesi)
    perintah = sesi.atur.get("perintah_pulihkan")
    if perintah and not (sesi.data.get("hook") or {}).get("pulihkan"):
        sesi.data.setdefault("hook", {})["pulihkan"] = jalankan_hook("pulihkan", perintah)
        sesi.simpan()
    log("--- Menyusun laporan")
    lap, peta = data_laporan(sesi, ctx)
    tulis_json(sesi.folder / "laporan.json", lap)
    (sesi.folder / "laporan.md").write_text(tulis_markdown(lap, peta, sesi), encoding="utf-8")
    (sesi.folder / "laporan.html").write_text(tulis_html(lap, peta, ctx["sensor"]), encoding="utf-8")
    sesi.data["status"] = "selesai"
    sesi.data["laporan"] = {"html": "laporan.html", "md": "laporan.md", "json": "laporan.json", "dibuat": lap["dibuat"]}
    sesi.simpan()
    log(f"  laporan: {sesi.folder / 'laporan.html'}")
    return keluaran(sesi, laporan_html=str(sesi.folder / "laporan.html"), laporan_md=str(sesi.folder / "laporan.md"),
                    keyakinan=lap["keyakinan"], jumlah_temuan=lap["jumlah_temuan"], aplikasi=sesi.app, peran=sesi.peran)


def cmd_ringkasan(args, ctx):
    baris = []
    for f in args.sesi:
        try:
            sesi = Sesi(f)
        except GagalOrkestrasi as ex:
            baris.append({"sesi": f, "galat": str(ex)})
            continue
        lap = baca_json(sesi.folder / "laporan.json")
        baris.append({"sesi": sesi.folder.name, "folder": sesi.folder, "aplikasi": sesi.app, "peran": sesi.peran,
                      "lap": lap, "status": sesi.data.get("status")})
    waktu = f"{datetime.now():%Y%m%d_%H%M%S}"
    e = html.escape
    h = [f"<!DOCTYPE html><html lang='id'><head><meta charset='utf-8'><title>Ringkasan uji UI/UX {waktu}</title>"
         f"<style>{CSS}</style></head><body><h1>Ringkasan evaluasi UI/UX</h1><p class='meta'>{e(sekarang())}</p>"
         "<table><tr><th>Aplikasi</th><th>Peran</th><th>Keyakinan</th>" + "".join(f"<th>{t}</th>" for t in TINGKAT)
         + "<th>Temuan teratas</th><th>Laporan</th></tr>"]
    md = [f"# Ringkasan evaluasi UI/UX ({sekarang()})", "",
          "| Aplikasi | Peran | Keyakinan | " + " | ".join(TINGKAT) + " | Laporan |", "|" + "---|" * (4 + len(TINGKAT))]
    for b in baris:
        lap = b.get("lap")
        if not lap:
            h.append(f"<tr><td colspan='{4 + len(TINGKAT)}'>{e(b['sesi'])}: {e(b.get('galat') or 'belum ada laporan (status ' + str(b.get('status')) + ')')}</td></tr>")
            md.append(f"| {b['sesi']} | - | - | " + " | ".join("-" for _ in TINGKAT) + f" | {b.get('galat') or 'belum ada laporan'} |")
            continue
        rel = os.path.relpath(b["folder"] / "laporan.html", eks.FOLDER_DASAR).replace(os.sep, "/")
        teratas = "; ".join(t["judul"] for t in lap["temuan"][:3])
        h.append(f"<tr><td>{e(lap['aplikasi']['nama'])}</td><td>{e(str(b['peran']))}</td><td>{lap['keyakinan']:.2f}</td>"
                 + "".join(f"<td>{lap['jumlah_temuan'].get(t, 0)}</td>" for t in TINGKAT)
                 + f"<td>{e(teratas)}</td><td><a href='{e(rel)}'>buka</a></td></tr>")
        md.append(f"| {lap['aplikasi']['nama']} | {b['peran']} | {lap['keyakinan']:.2f} | "
                  + " | ".join(str(lap["jumlah_temuan"].get(t, 0)) for t in TINGKAT) + f" | {rel} |")
    h.append("</table></body></html>")
    f_html = eks.FOLDER_DASAR / f"ringkasan_{waktu}.html"
    f_html.parent.mkdir(parents=True, exist_ok=True)
    f_html.write_text("".join(h), encoding="utf-8")
    f_html.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"status": "ok", "ringkasan_html": str(f_html), "ringkasan_md": str(f_html.with_suffix(".md")),
            "jumlah_sesi": len(baris)}


# ====================================================================== perintah gabungan
def cmd_siklus(args, ctx):
    hasil = cmd_mulai(args, ctx)
    sesi_arg = argparse.Namespace(sesi=hasil["sesi"], paksa=False, ulang=False)
    while True:
        cmd_rancang(sesi_arg, ctx)
        cmd_jalankan(sesi_arg, ctx)
        if not cmd_evaluasi(sesi_arg, ctx)["lanjut"]:
            break
    return cmd_laporan(sesi_arg, ctx)


def cmd_semua(args, ctx):
    nomor = args.skenario if args.skenario else list(range(len(ctx["skenario"])))
    if not nomor:
        raise GagalOrkestrasi(f"tidak ada skenario di {FILE_SKENARIO}")
    hasil = []
    for n in nomor:
        log(f"\n######## Skenario {n}: {ctx['skenario'][n].get('aplikasi')} / {ctx['skenario'][n].get('peran')}")
        a = argparse.Namespace(skenario=n, maks_putaran=args.maks_putaran, ambang=args.ambang,
                               tanpa_rekam=args.tanpa_rekam, izinkan_berisiko=False,
                               jelajah=args.jelajah, tanpa_jelajah=args.tanpa_jelajah,
                               overtesting=args.overtesting, batas_menit=args.batas_menit)
        try:
            hasil.append(cmd_siklus(a, ctx))
        except (GagalOrkestrasi, eks.Gagal) as ex:
            log(f"  skenario {n} gagal: {ex}")
            hasil.append({"status": "error", "skenario": n, "pesan": str(ex)})
    sesi = [h["sesi"] for h in hasil if h.get("sesi")]
    rk = cmd_ringkasan(argparse.Namespace(sesi=sesi), ctx) if sesi else {}
    return {"status": "ok" if all(h["status"] == "ok" for h in hasil) else "sebagian_gagal",
            "hasil": hasil, "ringkasan_html": rk.get("ringkasan_html")}


def gambar_uji(kode):
    pil = _pil()
    if pil is None:
        return None
    Image, ImageDraw, ImageFont = pil
    im = Image.new("RGB", (640, 360), "white")
    g = ImageDraw.Draw(im)
    try:
        g.text((60, 90), kode, fill=(20, 20, 20), font=ImageFont.load_default(size=160))
    except TypeError:  # Pillow lama tanpa font berskala: gambar kecil lalu perbesar
        kecil = Image.new("RGB", (80, 45), "white")
        ImageDraw.Draw(kecil).text((8, 14), kode, fill=(20, 20, 20))
        im = kecil.resize((640, 360), Image.NEAREST)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def cmd_cek_llm(args, ctx):
    conf, klien = ctx["llm"], ctx["klien"]
    hasil = {"base_url": conf["base_url"], "model_perancang": conf["model_perancang"],
             "model_evaluator": conf["model_evaluator"], "model_cadangan": conf.get("model_cadangan")}
    try:
        daftar = klien.daftar_model()
        hasil["model_terdaftar"] = {m: m in daftar for m in {conf["model_perancang"], conf["model_evaluator"],
                                                              conf.get("model_cadangan")} if m}
    except (LLMSementara, GagalOrkestrasi) as ex:
        hasil["model_terdaftar"] = f"tidak bisa dibaca: {ex}"
    t0 = time.perf_counter()
    teks, model = klien.chat([{"role": "system", "content": "Jawab hanya dengan satu objek JSON."},
                              {"role": "user", "content": 'Kembalikan persis: {"status": "siap", "angka": 42}'}],
                             "evaluator", "cek_teks")
    try:
        hasil["json_ok"] = ambil_json(teks).get("angka") == 42
    except ValueError:
        hasil["json_ok"] = False
    hasil["teks_ms"] = round((time.perf_counter() - t0) * 1000)
    hasil["model_dipakai"] = model
    kode = str(random.randint(1000, 9999))
    url = gambar_uji(kode)
    if url is None:
        hasil["gambar_ok"] = None
        hasil["catatan"] = "Pillow belum terpasang (pip install pillow), tes gambar dilewati"
    else:
        t0 = time.perf_counter()
        teks, _ = klien.chat([{"role": "user", "content": [
            {"type": "text", "text": 'Angka 4 digit berapa yang tertulis besar di gambar ini? Jawab hanya JSON {"angka": "...."}'},
            {"type": "image_url", "image_url": {"url": url}}]}], "evaluator", "cek_gambar")
        hasil["gambar_ok"] = kode in teks
        hasil["gambar_ms"] = round((time.perf_counter() - t0) * 1000)
        if not hasil["gambar_ok"]:
            hasil["jawaban_gambar"] = teks[:200]
    hasil["ffmpeg"] = bool(ffmpeg())
    hasil["status"] = "ok" if hasil["json_ok"] and hasil.get("gambar_ok") is not False else "peringatan"
    return hasil


def cmd_daftar_skenario(args, ctx):
    return {"status": "ok", "pengaturan": ctx["pengaturan"],
            "skenario": [{"no": i, **{k: v for k, v in s.items() if not k.startswith("_")}} for i, s in enumerate(ctx["skenario"])]}


# ====================================================================== main
def buat_parser():
    p = argparse.ArgumentParser(description="Orkestrator LLM Perancang + Pelaksana + LLM Evaluator untuk uji UI/UX")
    p.add_argument("--akun", default=str(eks.FILE_AKUN), help="lokasi akun.json")
    sub = p.add_subparsers(dest="perintah", required=True)
    sub.add_parser("cek-llm", help="tes koneksi LLM, jawaban JSON, dan pembacaan gambar")
    sub.add_parser("daftar-skenario", help="tampilkan skenario.json")

    def opsi_mulai(sp):
        sp.add_argument("--skenario", type=int, help="nomor skenario di skenario.json (mulai dari 0)")
        sp.add_argument("--app")
        sp.add_argument("--peran")
        sp.add_argument("--tujuan")
        sp.add_argument("--tujuan-b64", help="tujuan dalam base64 UTF-8 (aman untuk n8n)")
        sp.add_argument("--maks-putaran", type=int)
        sp.add_argument("--ambang", type=float, help="ambang keyakinan 0-1 untuk langsung membuat laporan")
        sp.add_argument("--tanpa-rekam", action="store_true")
        sp.add_argument("--jelajah", action="store_true", help="mode jelajah menyeluruh: lanjut sampai semua menu tersentuh")
        sp.add_argument("--tanpa-jelajah", action="store_true", help="matikan mode jelajah menyeluruh")
        sp.add_argument("--izinkan-berisiko", action="store_true",
                        help="izinkan CRUD (simpan/kirim/hapus) dengan pemulihan wajib")
        sp.add_argument("--overtesting", action="store_true",
                        help="penjelajah otomatis + uji mendalam sampai semua fitur, layar, dan tugas tercakup")
        sp.add_argument("--batas-menit", type=float, help="batas waktu sesi dalam menit (mode overtesting)")

    opsi_mulai(sub.add_parser("mulai", help="buat sesi baru + observasi awal di HP"))
    opsi_mulai(sub.add_parser("siklus", help="sesi penuh: observasi, rancang, jalankan, evaluasi (berulang), laporan"))
    for nama, bantuan in (("rancang", "LLM Perancang menyusun rencana putaran berikutnya"),
                          ("jalankan", "jalankan rencana putaran terakhir di HP"),
                          ("evaluasi", "LLM Evaluator menilai putaran terakhir"),
                          ("laporan", "buat laporan.html/.md/.json")):
        sp = sub.add_parser(nama, help=bantuan)
        sp.add_argument("--sesi", required=True, help="folder sesi (atau nama foldernya)")
        if nama == "rancang":
            sp.add_argument("--paksa", action="store_true", help="tambah putaran walau tidak diminta evaluator")
        if nama == "jalankan":
            sp.add_argument("--ulang", action="store_true", help="jalankan ulang rencana putaran terakhir")
    ps = sub.add_parser("semua", help="jalankan siklus untuk semua (atau sebagian) skenario + ringkasan")
    ps.add_argument("--skenario", type=int, nargs="*")
    ps.add_argument("--maks-putaran", type=int)
    ps.add_argument("--ambang", type=float)
    ps.add_argument("--tanpa-rekam", action="store_true")
    ps.add_argument("--jelajah", action="store_true")
    ps.add_argument("--tanpa-jelajah", action="store_true")
    ps.add_argument("--overtesting", action="store_true")
    ps.add_argument("--batas-menit", type=float)
    pr = sub.add_parser("ringkasan", help="ringkasan gabungan beberapa sesi")
    pr.add_argument("--sesi", nargs="+", required=True)
    return p


PERINTAH = {"cek-llm": cmd_cek_llm, "daftar-skenario": cmd_daftar_skenario, "mulai": cmd_mulai,
            "rancang": cmd_rancang, "jalankan": cmd_jalankan, "evaluasi": cmd_evaluasi, "laporan": cmd_laporan,
            "siklus": cmd_siklus, "semua": cmd_semua, "ringkasan": cmd_ringkasan}


def main(argv=None):
    args = buat_parser().parse_args(argv)
    eks.pakai_utf8()
    try:
        cfg = eks.muat_konfigurasi(args.akun)
        pengaturan, skenario = muat_skenario()
        pengaturan_file = (baca_json(FILE_SKENARIO, {}) or {}).get("pengaturan", {})
        conf = muat_llm()
        sensor = Sensor(cfg)
        ctx = {"cfg": cfg, "llm": conf, "pengaturan": pengaturan, "skenario": skenario, "pengaturan_file": pengaturan_file,
               "sensor": sensor, "klien": KlienLLM(conf, sensor)}
        hasil = PERINTAH[args.perintah](args, ctx)
        print(json.dumps(hasil, ensure_ascii=False, default=str))
        return 0
    except (GagalOrkestrasi, eks.Gagal) as ex:
        print(json.dumps({"status": "error", "pesan": str(ex), "sesi": getattr(args, "sesi", None)},
                         ensure_ascii=False, default=str))
        log(f"ERROR: {ex}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
