"""Server OpenAI-compatible tiruan (pengganti 9router) dengan jawaban berskenario untuk uji orkestrator.

- Perancang putaran 1: jawaban pertama sengaja tidak valid (aksi 'klik') untuk menguji perbaikan oleh validator,
  jawaban kedua valid dalam blok ```json.
- Evaluator: panggilan pertama dijawab HTTP 429 (uji coba ulang), putaran 1 keyakinan 0.55 + permintaan uji
  ulang, putaran 2 keyakinan 0.86 -> laporan.
Jalankan mandiri: python mock_llm.py --port 18080
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KODE_GAMBAR = "4321"

RENCANA_SALAH = {"aplikasi": "santamaria", "judul": "salah", "langkah": [{"aksi": "klik", "target": {"label": "Laporan"}}]}
RENCANA_1 = {
    "aplikasi": "santamaria", "judul": "jelajah_menu_utama", "tujuan": "jelajahi menu laporan dan ikon pojok",
    "langkah": [
        {"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
        {"aksi": "login", "nama": "login", "peran": "__PERAN__"},
        {"aksi": "tap", "nama": "buka_laporan", "target": {"label": "Laporan"},
         "tunggu_muncul": {"label": "Laporan Keuangan"}, "catatan": "ukur waktu buka laporan"},
        {"aksi": "tangkap", "nama": "layar_laporan"},
        {"aksi": "kembali", "nama": "kembali_beranda", "tunggu_muncul": {"label": "Beranda"}},
        {"aksi": "tap", "nama": "ikon_pojok", "target": {"kelas": "ImageButton", "urutan": 0}},
        {"aksi": "tangkap", "nama": "setelah_ikon"},
    ]}
RENCANA_2 = {
    "aplikasi": "santamaria", "judul": "ukur_ulang_dan_teks_besar", "tujuan": "permintaan evaluator",
    "langkah": [
        {"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
        {"aksi": "login", "nama": "login", "peran": "__PERAN__"},
    ] + [x for i in (1, 2, 3) for x in (
        {"aksi": "tap", "nama": f"buka_laporan_{i}", "target": {"label": "Laporan"}, "tunggu_muncul": {"label": "Laporan Keuangan"}},
        {"aksi": "kembali", "nama": f"kembali_{i}", "tunggu_muncul": {"label": "Beranda"}})] + [
        {"aksi": "atur_skala_font", "nama": "font_besar", "skala": 1.3},
        {"aksi": "buka_aplikasi", "nama": "buka_dengan_font_besar"},
        {"aksi": "tangkap", "nama": "beranda_font_besar"},
        {"aksi": "atur_skala_font", "nama": "font_normal", "skala": 1.0},
    ]}
EVAL_1 = {
    "ringkasan_eksekutif": "Beranda cukup sederhana, tetapi ada ikon tanpa label dan menu Laporan memuat lebih dari 1 detik.",
    "keyakinan": 0.55, "alasan_keyakinan": "waktu laporan baru diukur sekali", "keputusan": "uji_ulang",
    "temuan": [
        {"id": "T1", "judul": "Ikon pojok kanan atas tanpa label", "kategori": "aksesibilitas", "tingkat": "tinggi",
         "sumber": "terukur+visual", "deskripsi": "ImageButton 29 dp tidak punya label.",
         "bukti": [{"ref": "P0.6", "layar": "L1", "kode": "L1-A1", "keterangan": "ImageButton tanpa label"}],
         "dasar": "WCAG 4.1.2", "rekomendasi": "Tambahkan tooltip/Semantics label dan perbesar ke 48 dp."}],
    "cakupan": {"sudah_diuji": ["beranda", "laporan"], "belum_diuji": ["teks besar"]},
    "permintaan_uji_ulang": [
        {"jenis": "ulang_pengukuran", "tugas": "Tap 'Laporan' lalu kembali 3 kali", "alasan": "baru sekali"},
        "Uji teks besar 1.3 di beranda"]}
EVAL_2 = {
    "ringkasan_eksekutif": "Dua masalah utama: ikon tanpa label dan menu Laporan konsisten > 1 detik tanpa umpan balik jelas.",
    "keyakinan": "86%", "alasan_keyakinan": "pengukuran berulang konsisten", "keputusan": "laporan",
    "temuan": [
        {"id": "T2", "judul": "Menu Laporan memuat lebih dari 1 detik", "kategori": "waktu_respons", "tingkat": "sedang",
         "sumber": "terukur", "deskripsi": "Median sekitar 1,2 detik pada 4 pengukuran.",
         "bukti": [{"ref": "P2.3", "keterangan": "buka_laporan_1"}, {"ref": "P9.9", "keterangan": "ref palsu"}],
         "dasar": "Nielsen 1 detik", "rekomendasi": "Tampilkan skeleton/indikator dan cache data laporan."},
        {"id": "T1", "judul": "Ikon pojok kanan atas tanpa label", "kategori": "aksesibilitas", "tingkat": "tinggi",
         "sumber": "terukur+visual", "deskripsi": "ImageButton 29 dp tidak punya label.",
         "bukti": [{"ref": "P0.6", "layar": "L1", "kode": "L1-A1", "keterangan": "ImageButton tanpa label"}],
         "dasar": "WCAG 4.1.2", "rekomendasi": "Tambahkan tooltip/Semantics label dan perbesar ke 48 dp."},
        {"id": "T3", "judul": "Kategori aneh", "kategori": "astrologi", "tingkat": "parah", "deskripsi": "uji normalisasi",
         "bukti": ["teks bebas"], "rekomendasi": "-"}],
    "hal_positif": ["Login cepat (P0.4)"],
    "cakupan": {"sudah_diuji": ["beranda", "laporan", "teks besar"], "belum_diuji": []},
    "permintaan_uji_ulang": []}


POLA_PENANDA = re.compile(r"UJI-\d{4}-\d{6}")


def rencana_demo(teks_user):
    """Aplikasi demo (mode overtesting): putaran rencana = CRUD + teks besar + tema gelap + tanpa internet, dengan
    pemulihan yang SENGAJA tidak menghapus data (menguji putaran pemulihan otomatis); putaran pemulihan = hapus data
    berpenanda yang disebut di prompt."""
    if "jenis putaran: pemulihan" in teks_user:
        m = re.search(r"PERUBAHAN DATA YANG BELUM DIPULIHKAN[^\n]*\n(\[.*?\])\n", teks_user, re.S)
        penanda = sorted({x["penanda"] for x in json.loads(m.group(1)) if x.get("penanda")}) if m else []
        langkah = [{"aksi": "buka_aplikasi", "nama": "buka_aplikasi"}]
        for i, m in enumerate(penanda, 1):
            langkah += [{"aksi": "tap", "nama": f"buka_data_uji_{i}", "target": {"label_mengandung": m}},
                        {"aksi": "tap", "nama": f"hapus_data_uji_{i}", "target": {"label": "Hapus"}, "ubah_data": "hapus",
                         "objek": "pesanan"},
                        {"aksi": "pastikan_tidak_ada", "nama": f"data_uji_hilang_{i}", "target": {"label_mengandung": m}}]
        return {"aplikasi": "demo", "judul": "pemulihan_data_uji", "tujuan": "hapus data uji yang tertinggal", "langkah": langkah}
    m = re.search(r"DAFTAR UJI MENDALAM[^\n]*\n(\[.*?\])\n", teks_user, re.S)
    tugas = [x["id"] for x in json.loads(m.group(1))] if m else []
    return {"aplikasi": "demo", "judul": "overtesting_crud", "tujuan": "form, CRUD, teks besar, tema gelap, tanpa internet",
            "tugas": tugas, "langkah": [
                {"aksi": "buka_aplikasi", "nama": "buka_aplikasi"},
                {"aksi": "tap", "nama": "tambah", "target": {"label": "Tambah Pesanan"}},
                {"aksi": "tap", "nama": "simpan_kosong", "target": {"label": "Simpan"}, "ubah_data": "buat", "objek": "pesanan",
                 "catatan": "form kosong harus ditolak dengan pesan jelas"},
                {"aksi": "tap", "nama": "tambah_lagi", "target": {"label": "Tambah Pesanan"}},
                {"aksi": "ketik", "nama": "isi_judul", "target": {"hint": "Judul pesanan"}, "teks": "Pesanan {penanda} \U0001F600"},
                {"aksi": "tap", "nama": "simpan", "target": {"label": "Simpan"}, "ubah_data": "buat", "objek": "pesanan"},
                {"aksi": "pastikan_ada", "nama": "pesanan_tampil", "target": {"label_mengandung": "{penanda}"}},
                {"aksi": "atur_skala_font", "nama": "font_besar", "skala": 1.3},
                {"aksi": "buka_aplikasi", "nama": "buka_font_besar"},
                {"aksi": "tangkap", "nama": "dasbor_font_besar"},
                {"aksi": "atur_skala_font", "nama": "font_normal", "skala": 1.0},
                {"aksi": "atur_tema", "nama": "tema_gelap", "tema": "gelap"},
                {"aksi": "buka_aplikasi", "nama": "buka_tema_gelap"},
                {"aksi": "tangkap", "nama": "dasbor_gelap"},
                {"aksi": "atur_tema", "nama": "tema_terang", "tema": "terang"},
                {"aksi": "atur_jaringan", "nama": "tanpa_internet", "status": "mati"},
                {"aksi": "tap", "nama": "muat_ulang_offline", "target": {"label": "Muat ulang"}},
                {"aksi": "atur_jaringan", "nama": "internet_hidup", "status": "hidup"},
                {"aksi": "tap", "nama": "ketuk_cepat_1", "target": {"label": "Notifikasi"}},
                {"aksi": "kembali", "nama": "kembali_1"},
                {"aksi": "tap", "nama": "ketuk_cepat_2", "target": {"label": "Notifikasi"}},
                {"aksi": "kembali", "nama": "kembali_2"}],
            "pemulihan": [{"aksi": "pastikan_ada", "nama": "cek_saja", "target": {"label_mengandung": "{penanda}"}}]}


EVAL_DEMO = {
    "ringkasan_eksekutif": "Aplikasi demo crash saat Sinkronkan dan menampilkan NaN pada notifikasi.",
    "keyakinan": 0.82, "alasan_keyakinan": "crash dan log konsisten", "keputusan": "laporan",
    "temuan": [
        {"id": "T1", "judul": "Sinkronkan membuat aplikasi crash", "kategori": "crash", "tingkat": "kritis", "sumber": "terukur",
         "deskripsi": "Aplikasi tertutup dengan FATAL EXCEPTION.", "langkah_reproduksi": ["Buka aplikasi", "Ketuk tab Peta",
                                                                                          "Ketuk Sinkronkan"],
         "bukti": [{"ref": "P1.1", "keterangan": "log FATAL EXCEPTION"}], "dasar": "Nielsen #9",
         "rekomendasi": "Tangani IllegalStateException saat sinkronisasi."},
        {"id": "T2", "judul": "Total tagihan tampil NaN", "kategori": "data_janggal", "tingkat": "tinggi", "sumber": "terukur+visual",
         "deskripsi": "Teks NaN bocor ke tampilan.", "langkah_reproduksi": ["Buka aplikasi", "Ketuk Notifikasi"],
         "bukti": [{"layar": "L2", "kode": "L2-E1", "keterangan": "Total tagihan: NaN"}], "dasar": "Nielsen #1",
         "rekomendasi": "Format angka dengan NumberFormat dan nilai bawaan."}],
    "hal_positif": ["Navigasi tab konsisten"], "cakupan": {"sudah_diuji": ["semua menu"], "belum_diuji": []},
    "permintaan_uji_ulang": []}


class Status:
    def __init__(self):
        self.permintaan = []
        self.kirim_429 = True
        self.lock = threading.Lock()


def buat_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _kirim(self, kode, data, header=None):
            body = json.dumps(data).encode()
            self.send_response(kode)
            self.send_header("Content-Type", "application/json")
            for k, v in (header or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.endswith("/models"):
                return self._kirim(200, {"data": [{"id": "gemini/gemini-3.6-flash"}, {"id": "gemini/gemini-3.5-flash-lite"}]})
            self._kirim(404, {"error": "tidak ada"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with st.lock:
                st.permintaan.append({"auth": self.headers.get("Authorization"), "body": body})
            pesan = body["messages"]
            sistem = pesan[0]["content"] if pesan[0]["role"] == "system" else ""
            isi_user = pesan[1]["content"] if len(pesan) > 1 else pesan[0]["content"]
            teks_user = isi_user if isinstance(isi_user, str) else " ".join(b.get("text", "") for b in isi_user if b.get("type") == "text")
            ada_gambar = any(isinstance(p["content"], list) and any(b.get("type") == "image_url" for b in p["content"]) for p in pesan)
            m = re.search(r"PUTARAN: (\d+) dari", teks_user)
            k = int(m.group(1)) if m else 0
            peran = (re.search(r"PERAN UJI: (\w+)", teks_user) or [None, "owner"])[1]
            if "LLM PERANCANG UJI" in sistem and "Dasbor Demo" in teks_user:
                jawab = json.dumps(rencana_demo(teks_user), ensure_ascii=False)
            elif "LLM EVALUATOR UI/UX" in sistem and "Dasbor Demo" in teks_user:
                jawab = json.dumps(EVAL_DEMO, ensure_ascii=False)
            elif "LLM PERANCANG UJI" in sistem:
                if k == 1 and len(pesan) == 2:
                    jawab = "Berikut rencananya:\n" + json.dumps(RENCANA_SALAH)
                else:
                    r = RENCANA_1 if k == 1 else RENCANA_2
                    jawab = "```json\n" + json.dumps(r).replace("__PERAN__", peran) + "\n```"
            elif "LLM EVALUATOR UI/UX" in sistem:
                if st.kirim_429:
                    st.kirim_429 = False
                    return self._kirim(429, {"error": {"message": "rate limit"}}, {"Retry-After": "0"})
                jawab = json.dumps(EVAL_1 if k <= 1 else EVAL_2)
            elif ada_gambar:
                jawab = json.dumps({"angka": KODE_GAMBAR})
            else:
                jawab = '{"status": "siap", "angka": 42}'
            self._kirim(200, {"id": "x", "object": "chat.completion", "model": body["model"],
                              "choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": jawab}}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return H


def mulai(port=0):
    st = Status()
    srv = ThreadingHTTPServer(("127.0.0.1", port), buat_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), buat_handler(Status()))
    print(f"mock LLM di http://127.0.0.1:{a.port}/v1", flush=True)
    srv.serve_forever()
