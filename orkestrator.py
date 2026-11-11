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
    "maks_gambar_perancang": 4,
    "maks_gambar_evaluator": 14,
}
PENGATURAN_BAWAAN = {"ambang_yakin": 0.75, "maks_putaran": 3, "rekam": True, "boleh_ubah_data": False}
TUJUAN_BAWAAN = (
    "Evaluasi UI/UX alur utama peran ini: (1) aksesibilitas - label elemen untuk pembaca layar, ukuran area "
    "sentuh, kontras, dukungan teks besar; (2) waktu respons - startup, login, perpindahan layar, ada tidaknya "
    "indikator loading; (3) kerumitan UI - kepadatan elemen, kedalaman navigasi, kejelasan hierarki; "
    "(4) konsistensi dan umpan balik - pola navigasi, istilah, pesan. Jelajahi menu utama tanpa mengubah data."
)
TINGKAT = ("kritis", "tinggi", "sedang", "rendah")
KATEGORI = ("aksesibilitas", "waktu_respons", "kerumitan_ui", "konsistensi", "umpan_balik", "navigasi",
            "keterbacaan", "penanganan_error", "lainnya")
BATAS_JEDA_MS = 1000         # Nielsen: lebih dari 1 detik, alur pikir pengguna terputus
BATAS_PERHATIAN_MS = 10000   # Nielsen: batas perhatian 10 detik
VITALS_STARTUP_MS = {"COLD": 5000, "WARM": 2000, "HOT": 1500}   # Android vitals: dianggap berlebihan
AKSI_BERWAKTU = ("tap", "kembali", "gulir", "buka_aplikasi", "login", "atur_skala_font")
KODE_MASALAH = {"interaktif_tanpa_label": "A", "target_sentuh_di_bawah_48dp": "T",
                "input_label_hanya_dari_hint": "H", "tombol_tidak_bisa_diklik": "B"}
WARNA_MASALAH = {"A": (220, 38, 38), "T": (234, 88, 12), "H": (202, 138, 4), "B": (147, 51, 234),
                 "K": (37, 99, 235)}
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
        if e.get("kelas") == "EditText" or not e.get("label") or not e.get("kotak"):
            continue
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
        rasio = rasio_kontras(latar, tinta)
        if rasio < 4.5:
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


# ====================================================================== peta layar & pengukuran
def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def bangun_peta(sesi, kontras=False):
    """Layar unik dari semua run sesi (L1, L2, ...) + pemetaan ref langkah (P1.3) -> layar."""
    layar, ref_layar, runs = [], {}, []
    for rid, folder in sesi.daftar_run():
        r = baca_json(folder / "ringkasan.json")
        dpi = (r.get("perangkat") or {}).get("dpi")
        density = dpi / 160 if dpi else None
        runs.append({"id": rid, "folder": folder, "ringkasan": r, "density": density})
        jalur = []
        for lk in r.get("langkah", []):
            ref = f"{rid}.{lk['no']}"
            if lk.get("berhasil"):
                jalur.append(lk["langkah"])
            if lk.get("privasi") or not lk.get("xml") or not (folder / lk["xml"]).exists():
                continue  # langkah privasi (misalnya layar login Google) tidak pernah dipakai sebagai bukti/gambar
            try:
                elemen = eks.daftar_elemen((folder / lk["xml"]).read_text(encoding="utf-8"), density, maks=120, kotak=True)
            except ET.ParseError:
                continue
            tanda = frozenset((e["kelas"], (e.get("label") or e.get("hint") or "")[:60])
                              for e in elemen if e.get("label") or e.get("hint"))
            sama = next((s for s in layar if _jaccard(s["_tanda"], tanda) >= 0.8), None)
            if sama:
                sama["refs"].append(ref)
                ref_layar[ref] = sama["id"]
                continue
            label = [e["label"].replace("\n", " ") for e in elemen if e.get("label")]
            s = {"id": f"L{len(layar) + 1}", "_tanda": tanda, "refs": [ref], "run": rid, "folder": folder,
                 "screenshot": folder / lk["screenshot"] if lk.get("screenshot") else None,
                 "elemen": elemen, "metrik": lk.get("metrik") or {}, "density": density,
                 "jalur": jalur[-6:], "judul": " / ".join(label[:2])[:70] or "(tanpa teks)"}
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
    return {"layar": layar, "ref_layar": ref_layar, "runs": runs}


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
                if w >= BATAS_JEDA_MS:
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
                "respon_ms": lk.get("respon_ms"), "stabil_ms": lk.get("sampai_stabil_ms"),
                "durasi_ms": lk.get("durasi_aksi_ms"), "layar": ref_layar.get(ref), "galat": lk.get("galat"),
                "catatan": lk.get("catatan"), "startup": lk.get("startup") or None}
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


def agen_perancang(sesi, k, ctx):
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
    sebelumnya = sesi.putaran[k - 2] if k >= 2 else None
    ev = sesi.evaluasi_terakhir() if k >= 2 else None
    teks = [f"APLIKASI: {json.dumps(info, ensure_ascii=False)}",
            f"PERAN UJI: {sesi.peran or '(tanpa login)'}",
            f"TUJUAN UJI: {sesi.data['tujuan']}",
            f"PUTARAN: {k} dari maksimal {atur['maks_putaran']}",
            "BATASAN: " + ("tombol yang mengubah data DIIZINKAN untuk sesi ini." if atur["boleh_ubah_data"]
                           else "tombol yang mengubah data/keluar dari aplikasi diblokir pelaksana."),
            "", "PETA LAYAR (JSON):",
            json.dumps([ringkas_layar(s, "perancang") for s in peta["layar"]], ensure_ascii=False)]
    if sebelumnya:
        run = next((r for r in peta["runs"] if r["id"] == f"P{k - 1}"), None)
        teks += ["", f"HASIL PUTARAN {k - 1} (rencana '{(baca_json(sesi.folder / sebelumnya['rencana']) or {}).get('judul')}'):",
                 json.dumps([{x: y for x, y in b.items() if x in ("ref", "langkah", "aksi", "berhasil", "galat", "layar")}
                             for b in tabel_langkah(run, peta["ref_layar"])] if run else [], ensure_ascii=False)]
    if ev:
        teks += ["", "TEMUAN SEMENTARA EVALUATOR: " + "; ".join(
            f"{t.get('id')} {t.get('judul')}" for t in ev.get("temuan", [])[:15]),
            "", "PERMINTAAN EVALUATOR UNTUK PUTARAN INI (prioritas utama):",
            json.dumps(ev.get("permintaan_uji_ulang", []), ensure_ascii=False)]
    teks += ["", "Susun rencana uji untuk putaran ini. Keluarkan HANYA objek JSON rencana."]
    isi = [{"type": "text", "text": "\n".join(teks)}]

    if conf.get("kirim_gambar", True):
        terakhir = peta["runs"][-1]["id"]
        pilih = [peta["layar"][0]] + [s for s in reversed(peta["layar"][1:]) if s["run"] == terakhir]
        pilih += [s for s in reversed(peta["layar"][1:]) if s not in pilih]
        for s in pilih[:int(conf["maks_gambar_perancang"])]:
            if not s["screenshot"] or not s["screenshot"].exists():
                continue
            kotak = [(str(e["no"]), e.get("kotak"), WARNA_NOMOR[i % len(WARNA_NOMOR)]) for i, e in enumerate(s["elemen"][:60])]
            url = gambar_url(s["screenshot"], int(conf["lebar_gambar_perancang"]), kotak,
                             tutup=kotak_rahasia(s["elemen"], ctx["sensor"]))
            isi += [{"type": "text", "text": f"Screenshot layar {s['id']} ({s['judul']}); nomor kotak = 'no' di daftar elemen {s['id']}:"},
                    {"type": "image_url", "image_url": {"url": url}}]

    pesan = [{"role": "system", "content": sistem}, {"role": "user", "content": isi}]
    izinkan = bool(atur["boleh_ubah_data"])
    salah = []
    for coba in range(1, 4):
        jawaban, model = ctx["klien"].chat(pesan, "perancang", f"perancang_{coba}", folder)
        try:
            rencana = rapikan_rencana(ambil_json(jawaban), sesi)
            salah = eks.validasi_rencana(rencana, cfg, izinkan)
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
            "bukti": bukti, "dasar": str(t.get("dasar") or ""), "rekomendasi": str(t.get("rekomendasi") or "")})
    urutan = {t: i for i, t in enumerate(TINGKAT)}
    hasil["temuan"].sort(key=lambda t: urutan[t["tingkat"]])
    for p in (ev.get("permintaan_uji_ulang") or [])[:3]:
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
        teks += ["", f"LANGKAH UJI {run['id']} - judul '{rencana.get('judul')}', tujuan: {rencana.get('tujuan') or '-'}; "
                 f"berhasil={rencana.get('berhasil')}, rekaman={'ada' if rencana.get('rekaman') else 'tidak ada'}:",
                 json.dumps(tabel_langkah(run, peta["ref_layar"]), ensure_ascii=False)]
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

    if conf.get("kirim_gambar", True):
        maks, lebar, n = int(conf["maks_gambar_evaluator"]), int(conf["lebar_gambar_evaluator"]), 0
        baru = [s for s in peta["layar"] if s["run"] in run_baru and s["screenshot"] and s["screenshot"].exists()]
        lama = [s for s in peta["layar"] if s not in baru and s["screenshot"] and s["screenshot"].exists()]
        antre = [("asli", s) for s in baru[:8]]
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
                                                 "daftar masalah layar ini (A merah, T oranye, H kuning, B ungu, K biru):"},
                        {"type": "image_url", "image_url": {"url": gambar_url(x["screenshot"], lebar, kotak,
                                                                              tutup=kotak_rahasia(x["elemen"], ctx["sensor"]))}}]
            else:
                isi += [{"type": "text", "text": f"Screenshot layar {x['id']} ({x['judul']}), langkah {', '.join(x['refs'][:4])}:"},
                        {"type": "image_url", "image_url": {"url": gambar_url(x["screenshot"], lebar,
                                                                              tutup=kotak_rahasia(x["elemen"], ctx["sensor"]))}}]
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
    if ev["keyakinan"] >= ambang:
        alasan = f"keyakinan {ev['keyakinan']:.2f} >= ambang {ambang}"
        lanjut = False
    elif k >= maks_putaran:
        alasan = f"keyakinan {ev['keyakinan']:.2f} < ambang {ambang}, tetapi putaran maksimal ({maks_putaran}) tercapai"
        lanjut = False
    elif not ev["permintaan_uji_ulang"]:
        alasan = f"keyakinan {ev['keyakinan']:.2f} < ambang {ambang}, tetapi evaluator tidak punya permintaan uji ulang"
        lanjut = False
    else:
        alasan = f"keyakinan {ev['keyakinan']:.2f} < ambang {ambang}; {len(ev['permintaan_uji_ulang'])} permintaan uji ulang"
        lanjut = True
    ev.update({"lanjut": lanjut, "alasan_keputusan": alasan, "putaran": k, "model": model, "waktu": sekarang()})
    return ev


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
        app, peran, atur = args.app, args.peran, dict(pengaturan)
        tujuan = args.tujuan or (base64.b64decode(args.tujuan_b64).decode("utf-8") if args.tujuan_b64 else None)
    for k, attr in (("maks_putaran", "maks_putaran"), ("ambang_yakin", "ambang")):
        if getattr(args, attr, None) is not None:
            atur[k] = getattr(args, attr)
    if getattr(args, "tanpa_rekam", False):
        atur["rekam"] = False
    if getattr(args, "izinkan_berisiko", False):
        atur["boleh_ubah_data"] = True
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
    log(f"=== Sesi baru: {sesi.folder.name}\n--- Observasi awal (buka aplikasi{', login ' + peran if peran else ''}, tangkap layar)")
    langkah = [{"aksi": "buka_aplikasi", "nama": "buka_aplikasi"}]
    if peran:
        langkah.append({"aksi": "login", "nama": f"login_{peran}", "peran": peran})
    langkah += [{"aksi": "tangkap", "nama": "layar_awal"},
                {"aksi": "gulir", "nama": "gulir_bawah", "arah": "bawah", "lanjut_jika_gagal": True},
                {"aksi": "tangkap", "nama": "layar_awal_bawah"},
                {"aksi": "gulir", "nama": "gulir_atas", "arah": "atas", "lanjut_jika_gagal": True}]
    rencana = {"aplikasi": app, "peran": peran, "judul": "observasi_awal",
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
    if k > int(sesi.atur["maks_putaran"]) and not getattr(args, "paksa", False):
        raise GagalOrkestrasi(f"putaran maksimal ({sesi.atur['maks_putaran']}) tercapai")
    log(f"--- Putaran {k}: LLM Perancang menyusun rencana")
    rencana, model = agen_perancang(sesi, k, ctx)
    file_rencana = f"putaran_{k}/rencana.json"
    tulis_json(sesi.folder / file_rencana, rencana)
    ev = sesi.evaluasi_terakhir() if k > 1 else None
    data = {"ke": k, "status": "rencana_siap", "rencana": file_rencana, "folder_uji": f"putaran_{k}/uji",
            "model_perancang": model, "dibuat": sekarang(),
            "permintaan": (ev or {}).get("permintaan_uji_ulang", []) if k > 1 else []}
    if p is not None and p["ke"] == k:
        sesi.putaran[-1] = data
    else:
        sesi.putaran.append(data)
    sesi.data["status"] = "berjalan"
    sesi.simpan()
    log(f"  rencana '{rencana.get('judul')}' ({len(rencana['langkah'])} langkah) -> {file_rencana}")
    return keluaran(sesi, putaran=k, judul=rencana.get("judul"), jumlah_langkah=len(rencana["langkah"]), lanjut=True)


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
    hasil = eks.jalankan_rencana(rencana, ctx["cfg"], rekam=atur["rekam"], folder=sesi.folder / p["folder_uji"],
                                 izinkan_berisiko=atur["boleh_ubah_data"])
    if hasil["status"] == "rencana_tidak_valid":
        p["status"] = "rencana_ditolak"
        sesi.simpan()
        raise GagalOrkestrasi("rencana ditolak pelaksana: " + "; ".join(hasil.get("kesalahan", [])))
    p.update({"status": "diuji", "hasil_uji": {k: hasil.get(k) for k in ("status", "berhasil", "jumlah_langkah", "langkah_gagal", "rekaman")}})
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
    file_ev = f"putaran_{p['ke']}/evaluasi.json"
    tulis_json(sesi.folder / file_ev, ev)
    p.update({"status": "dievaluasi", "evaluasi": file_ev, "keyakinan": ev["keyakinan"], "lanjut": ev["lanjut"],
              "alasan_keputusan": ev["alasan_keputusan"]})
    sesi.data["status"] = "perlu_uji_ulang" if ev["lanjut"] else "siap_laporan"
    sesi.simpan()
    log(f"  keyakinan {ev['keyakinan']:.2f}, {len(ev['temuan'])} temuan -> "
        + ("UJI ULANG: " + " | ".join(x["tugas"][:80] for x in ev["permintaan_uji_ulang"]) if ev["lanjut"] else "LAPORAN"))
    return keluaran(sesi, putaran=p["ke"], keyakinan=ev["keyakinan"], lanjut=ev["lanjut"],
                    alasan=ev["alasan_keputusan"], jumlah_temuan=len(ev["temuan"]),
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
    layar = []
    for s in peta["layar"]:
        m, masalah = s["metrik"], masalah_berkode(s)
        layar.append({"id": s["id"], "judul": s["judul"], "ref": s["refs"], "jumlah_node": m.get("jumlah_node"),
                      "kedalaman_maks": m.get("kedalaman_maks"), "elemen_interaktif": m.get("elemen_interaktif"),
                      "rasio_interaktif_berlabel": m.get("rasio_interaktif_berlabel"),
                      **{f"jumlah_{j}": sum(1 for x in masalah if x["kode"][0] == h)
                         for j, h in (("tanpa_label", "A"), ("target_kecil", "T"), ("hanya_hint", "H"),
                                      ("tombol_tidak_bisa_diklik", "B"), ("kontras_rendah", "K"))},
                      "masalah": [{k: v for k, v in x.items() if k != "bounds" and v is not None} for x in masalah]})
    langkah = []
    for run in peta["runs"]:
        for lk in run["ringkasan"].get("langkah", []):
            w = waktu_langkah(lk)
            if lk.get("aksi") in AKSI_BERWAKTU and (w is not None or not lk.get("berhasil")):
                langkah.append({"ref": f"{run['id']}.{lk['no']}", "langkah": lk["langkah"], "aksi": lk.get("aksi"),
                                "berhasil": lk.get("berhasil"), "waktu_ms": w,
                                "jenis_waktu": "respon" if lk.get("respon_ms") is not None else "sampai_stabil",
                                "galat": lk.get("galat")})
    riwayat = []
    for p in sesi.putaran:
        r = baca_json(sesi.folder / p["rencana"]) or {}
        riwayat.append({"putaran": p["ke"], "judul": r.get("judul"), "tujuan": r.get("tujuan"),
                        "jumlah_langkah": len(r.get("langkah", [])), "berhasil": (p.get("hasil_uji") or {}).get("berhasil"),
                        "keyakinan": p.get("keyakinan"), "keputusan": p.get("alasan_keputusan"),
                        "permintaan_sebelumnya": [x.get("tugas") for x in p.get("permintaan") or []]})
    rekaman = [str(r["folder"] / r["ringkasan"]["rekaman"]) for r in peta["runs"] if r["ringkasan"].get("rekaman")]
    info = info_aplikasi(sesi, ctx["cfg"], peta)
    lap = {"judul": f"Laporan Evaluasi UI/UX - {info['nama']} (peran: {sesi.peran or '-'})",
           "dibuat": sekarang(), "sesi": sesi.folder.name, "aplikasi": info, "peran": sesi.peran,
           "tujuan": sesi.data["tujuan"], "jumlah_putaran": len(sesi.putaran), "keyakinan": ev["keyakinan"],
           "ambang_yakin": sesi.atur["ambang_yakin"], "alasan_keputusan": ev.get("alasan_keputusan"),
           "ringkasan_eksekutif": ev.get("ringkasan_eksekutif"), "jumlah_temuan": jumlah, "temuan": ev["temuan"],
           "hal_positif": ev.get("hal_positif", []), "cakupan": ev.get("cakupan", {}),
           "pengukuran": {**ukur, "langkah": langkah, "layar": layar}, "riwayat": riwayat, "rekaman": rekaman,
           "model": {"perancang": ctx["llm"]["model_perancang"], "evaluator": ev.get("model")},
           "metode": [eks.CATATAN_METODE, CATATAN_FRAME,
                      "Kontras dihitung otomatis dari piksel screenshot (perkiraan) dan perlu dikonfirmasi visual.",
                      "Temuan ditulis oleh LLM Evaluator berdasarkan data di atas; verifikasi temuan penting secara "
                      "manual (misalnya dengan TalkBack dan Accessibility Scanner) sebelum dijadikan keputusan.",
                      "Data akun tidak dikirim ke LLM: teks disensor dan kolom input berisi data akun ditutup "
                      "pada gambar sebelum dikirim."]}
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


def tulis_markdown(lap, peta, sesi):
    a = lap["aplikasi"]
    md = [f"# {lap['judul']}", "",
          f"- Aplikasi: {a['nama']} `{a['package']}` versi {a.get('versi') or '-'}",
          f"- Perangkat: {a.get('perangkat') or '-'} (Android {a.get('android') or '-'}, {a.get('dpi') or '-'} dpi)",
          f"- Dibuat: {lap['dibuat']} - sesi `{lap['sesi']}` - {lap['jumlah_putaran']} putaran uji",
          f"- Keyakinan evaluator: **{lap['keyakinan']:.2f}** (ambang {lap['ambang_yakin']}) - {lap.get('alasan_keputusan') or ''}",
          f"- Tujuan uji: {lap['tujuan']}", "", "## Ringkasan", "", lap.get("ringkasan_eksekutif") or "-", "",
          "Jumlah temuan: " + ", ".join(f"{t} {n}" for t, n in lap["jumlah_temuan"].items()), "",
          "## Temuan dan rekomendasi", ""]
    for t in lap["temuan"]:
        md += [f"### {t['id']} [{t['tingkat'].upper()}] {t['judul']}", "",
               f"*Kategori:* {t['kategori']} - *Sumber:* {t.get('sumber') or '-'}", "", t["deskripsi"], ""]
        for b in t["bukti"]:
            s = _ref_ke_gambar(peta, b.get("ref"), b.get("layar"), b.get("kode"))
            gambar = f" ![{s['id']}]({os.path.relpath(s['screenshot'], sesi.folder).replace(os.sep, '/')})" if s and s["screenshot"] else ""
            md.append(f"- Bukti {b.get('kode') or b.get('ref') or b.get('layar') or ''}: {b.get('keterangan', '')}{gambar}")
        md += ["", f"*Dasar:* {t.get('dasar') or '-'}", "", f"**Rekomendasi:** {t.get('rekomendasi') or '-'}", ""]
    if lap["hal_positif"]:
        md += ["## Hal yang sudah baik", ""] + [f"- {x}" for x in lap["hal_positif"]] + [""]
    u = lap["pengukuran"]
    md += ["## Hasil pengukuran otomatis", "", "### Startup", "",
           _md_tabel(["Ref", "TotalTime (ms)", "Jenis", "Batas Android vitals (ms)", "Berlebihan"],
                     [[x["ref"], x["total_time_ms"], x["jenis"], x["batas_vitals_ms"], "ya" if x["berlebihan"] else "tidak"]
                      for x in u["startup"]]),
           "### Waktu per langkah", "",
           _md_tabel(["Ref", "Langkah", "Aksi", "Waktu (ms)", "Jenis", "Status"],
                     [[x["ref"], x["langkah"], x["aksi"], x["waktu_ms"], x["jenis_waktu"],
                       ("GAGAL: " + str(x["galat"])) if not x["berhasil"] else (">= 1 detik" if (x["waktu_ms"] or 0) >= BATAS_JEDA_MS else "ok")]
                      for x in u["langkah"]])]
    if u["statistik_berulang"]:
        md += ["### Pengukuran berulang", "",
               _md_tabel(["Langkah", "n", "Median (ms)", "Min", "Maks"],
                         [[x["langkah"], x["n"], x["median_ms"], x["min_ms"], x["maks_ms"]] for x in u["statistik_berulang"]])]
    md += ["### Aksesibilitas dan kerumitan per layar", "",
           _md_tabel(["Layar", "Judul", "Node", "Kedalaman", "Interaktif", "Tanpa label", "Target <48dp", "Hanya hint",
                      "Tombol tak bisa diklik", "Kontras <4,5"],
                     [[x["id"], x["judul"], x["jumlah_node"], x["kedalaman_maks"], x["elemen_interaktif"],
                       x["jumlah_tanpa_label"], x["jumlah_target_kecil"], x["jumlah_hanya_hint"],
                       x["jumlah_tombol_tidak_bisa_diklik"], x["jumlah_kontras_rendah"]] for x in u["layar"]]),
           "## Cakupan uji", "", "Sudah diuji:", ""] + [f"- {x}" for x in lap["cakupan"].get("sudah_diuji", [])] + \
          ["", "Belum diuji:", ""] + [f"- {x}" for x in lap["cakupan"].get("belum_diuji", [])] + \
          ["", "## Riwayat putaran", "",
           _md_tabel(["Putaran", "Rencana", "Langkah", "Berhasil", "Keyakinan", "Keputusan"],
                     [[r["putaran"], r["judul"], r["jumlah_langkah"], _ya(r["berhasil"]),
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
min-width:110px}.angka b{font-size:1.4em;display:block}.jelek{color:#b42318;font-weight:600}"""


def tulis_html(lap, peta, sensor):
    e = html.escape
    a, u = lap["aplikasi"], lap["pengukuran"]
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
         f"&middot; {e(lap['dibuat'])} &middot; {lap['jumlah_putaran']} putaran uji &middot; sesi {e(lap['sesi'])}</p>",
         f"<p><b>Tujuan uji:</b> {e(lap['tujuan'])}</p>", "<div class='angka'>"]
    h += [f"<div><b>{lap['keyakinan']:.2f}</b>keyakinan (ambang {lap['ambang_yakin']})</div>"]
    h += [f"<div><b class='{'jelek' if n and t in ('kritis', 'tinggi') else ''}'>{n}</b>temuan {t}</div>"
          for t, n in lap["jumlah_temuan"].items()]
    st = [x for x in u["startup"] if x["jenis"] == "COLD"]
    if st:
        h.append(f"<div><b>{round(statistics.median(x['total_time_ms'] for x in st))} ms</b>cold start (median)</div>")
    h += ["</div>", f"<h2>Ringkasan</h2><p>{e(lap.get('ringkasan_eksekutif') or '-')}</p>",
          f"<p class='meta'>{e(lap.get('alasan_keputusan') or '')}</p>", "<h2>Temuan dan rekomendasi</h2>"]
    for t in lap["temuan"]:
        h.append(f"<div class='kartu'><h3><span class='lbl {t['tingkat']}'>{t['tingkat'].upper()}</span> {e(t['id'])} "
                 f"{e(t['judul'])}</h3><p class='meta'>Kategori: {e(t['kategori'])} &middot; Sumber: {e(t.get('sumber') or '-')}</p>"
                 f"<p>{e(t['deskripsi'])}</p><div class='bukti'>")
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
    h.append("<h2>Hasil pengukuran otomatis</h2><h3>Startup</h3>")
    h.append(tabel(["Ref", "TotalTime (ms)", "Jenis", "Batas Android vitals (ms)", "Berlebihan"],
                   [[e(x["ref"]), x["total_time_ms"], e(x["jenis"]), x["batas_vitals_ms"],
                     "<span class='jelek'>ya</span>" if x["berlebihan"] else "tidak"] for x in u["startup"]]))
    h.append("<h3>Waktu per langkah</h3>")
    h.append(tabel(["Ref", "Langkah", "Aksi", "Waktu (ms)", "Jenis", "Status"],
                   [[e(x["ref"]), e(x["langkah"]), e(x["aksi"] or ""), x["waktu_ms"] if x["waktu_ms"] is not None else "-",
                     e(x["jenis_waktu"]),
                     f"<span class='jelek'>GAGAL: {e(str(x['galat']))}</span>" if not x["berhasil"] else
                     ("<span class='jelek'>&ge; 1 detik</span>" if (x["waktu_ms"] or 0) >= BATAS_JEDA_MS else "ok")]
                    for x in u["langkah"]]))
    if u["statistik_berulang"]:
        h.append("<h3>Pengukuran berulang</h3>" + tabel(["Langkah", "n", "Median (ms)", "Min", "Maks"],
                 [[e(x["langkah"]), x["n"], x["median_ms"], x["min_ms"], x["maks_ms"]] for x in u["statistik_berulang"]]))
    h.append("<h3>Aksesibilitas dan kerumitan per layar</h3>")
    h.append(tabel(["Layar", "Judul", "Node", "Kedalaman", "Interaktif", "Tanpa label", "Target <48dp", "Hanya hint",
                    "Tombol tak bisa diklik", "Kontras <4,5"],
                   [[e(x["id"]), e(x["judul"]), x["jumlah_node"], x["kedalaman_maks"], x["elemen_interaktif"],
                     x["jumlah_tanpa_label"], x["jumlah_target_kecil"], x["jumlah_hanya_hint"],
                     x["jumlah_tombol_tidak_bisa_diklik"], x["jumlah_kontras_rendah"]] for x in u["layar"]]))
    h.append("<h3>Galeri layar (kotak = masalah otomatis: A tanpa label, T target kecil, H hanya hint, "
             "B tombol tak bisa diklik, K kontras)</h3><div class='galeri'>")
    for s in peta["layar"][:16]:
        if s["screenshot"] and s["screenshot"].exists():
            h.append(f"<figure style='margin:0'><img src='{img(s, anotasi=True, lebar=360)}' alt='Layar {e(s['id'])}'>"
                     f"<figcaption>{e(s['id'])} - {e(s['judul'])}</figcaption></figure>")
    h.append("</div><h2>Cakupan uji</h2><p><b>Sudah diuji</b></p><ul>"
             + "".join(f"<li>{e(x)}</li>" for x in lap["cakupan"].get("sudah_diuji", []))
             + "</ul><p><b>Belum diuji</b></p><ul>"
             + "".join(f"<li>{e(x)}</li>" for x in lap["cakupan"].get("belum_diuji", [])) + "</ul>")
    h.append("<h2>Riwayat putaran</h2>" + tabel(["Putaran", "Rencana", "Langkah", "Berhasil", "Keyakinan", "Keputusan"],
             [[r["putaran"], e(str(r["judul"])), r["jumlah_langkah"], _ya(r["berhasil"]),
               f"{r['keyakinan']:.2f}" if r["keyakinan"] is not None else "-", e(str(r["keputusan"] or ""))]
              for r in lap["riwayat"]]))
    h.append("<h2>Metode dan keterbatasan</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in lap["metode"])
             + f"</ul><p class='meta'>Model: perancang {e(str(lap['model']['perancang']))}, evaluator "
             f"{e(str(lap['model']['evaluator']))}.</p></body></html>")
    return "".join(h)


def cmd_laporan(args, ctx):
    sesi = Sesi(args.sesi)
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
                               tanpa_rekam=args.tanpa_rekam, izinkan_berisiko=False)
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
        sp.add_argument("--izinkan-berisiko", action="store_true",
                        help="izinkan tap tombol yang mengubah data (hapus/simpan/kirim/bayar/keluar)")

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
        conf = muat_llm()
        sensor = Sensor(cfg)
        ctx = {"cfg": cfg, "llm": conf, "pengaturan": pengaturan, "skenario": skenario,
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
