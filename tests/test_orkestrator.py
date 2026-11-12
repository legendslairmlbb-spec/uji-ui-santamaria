"""Uji ujung-ke-ujung orkestrator.py: simulator HP + server LLM tiruan (pengganti 9router)."""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="orkes_")
os.environ["UJI_UI_FOLDER"] = TMP
AKAR = Path(__file__).resolve().parent.parent
os.environ["UJI_UI_AKUN"] = str(AKAR / "tests" / "fixtures" / "akun_uji.json")
os.environ["UJI_UI_SKENARIO"] = str(AKAR / "skenario.json")
sys.path.insert(0, str(AKAR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_llm  # noqa: E402

srv, ST = mock_llm.mulai()
LLM_JSON = Path(TMP) / "llm.json"
LLM_JSON.write_text(json.dumps({"base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1", "api_key": "sk-test",
                                "jeda_coba_detik": 0.1, "maks_coba": 3}), encoding="utf-8")
os.environ["UJI_UI_LLM"] = str(LLM_JSON)

import agen_uji as ag  # noqa: E402
import simulator  # noqa: E402
import orkestrator as ork  # noqa: E402

SIM = simulator.pasang(ag)
ork.random.randint = lambda a, b: int(mock_llm.KODE_GAMBAR)
lulus = 0


def cek(kondisi, pesan):
    global lulus
    assert kondisi, pesan
    lulus += 1


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        kode = ork.main(list(argv))
    baris = [b for b in out.getvalue().splitlines() if b.strip()]
    cek(len(baris) == 1, f"stdout harus tepat satu baris JSON, dapat {len(baris)}:\n{out.getvalue()[:500]}")
    return kode, json.loads(baris[0]), err.getvalue()


# ---------------------------------------------------------------- 1. cek-llm
kode, h, err = cli("cek-llm")
cek(kode == 0 and h["status"] == "ok" and h["json_ok"] and h["gambar_ok"], f"cek-llm: {h}\n{err}")
cek(all(h["model_terdaftar"].values()), "model harus terdaftar")
cek(ST.permintaan[-1]["auth"] == "Bearer sk-test", "API key harus dikirim sebagai Bearer")
print("1. cek-llm: OK ->", {k: h[k] for k in ("json_ok", "gambar_ok", "ffmpeg")})

# ---------------------------------------------------------------- 2. daftar-skenario
kode, h, _ = cli("daftar-skenario")
cek(len(h["skenario"]) == 4 and h["skenario"][1]["peran"] == "consumer", f"skenario: {h}")
print("2. daftar-skenario: OK ->", [(s["no"], s["aplikasi"], s["peran"]) for s in h["skenario"]])

# ---------------------------------------------------------------- 3. siklus penuh (owner): 2 putaran
n0 = len(ST.permintaan)
kode, h, err = cli("siklus", "--app", "santamaria", "--peran", "owner", "--tanpa-jelajah", "--maks-putaran", "3")
cek(kode == 0 and h["status"] == "ok", f"siklus: {h}\n{err[-3000:]}")
sesi = Path(h["sesi"])
data = json.loads((sesi / "sesi.json").read_text(encoding="utf-8"))
cek(len(data["putaran"]) == 2 and data["status"] == "selesai", f"harus 2 putaran: {data['putaran']}")
cek(data["putaran"][0]["lanjut"] is True and data["putaran"][1]["lanjut"] is False, "putaran 1 lanjut, putaran 2 berhenti")
cek(abs(data["putaran"][1]["keyakinan"] - 0.86) < 1e-9, "keyakinan '86%' harus jadi 0.86")
cek(len(data["putaran"][1]["permintaan"]) == 2, "permintaan putaran 1 (termasuk string) harus diteruskan ke putaran 2")
cek("rencana ditolak validator" in err and "rencana_salah" not in err, "perbaikan rencana oleh validator harus terjadi")
cek("gagal (coba 1)" in err and "HTTP 429" in err, "HTTP 429 harus dicoba ulang")
r1 = json.loads((sesi / "putaran_1" / "rencana.json").read_text(encoding="utf-8"))
cek(r1["aplikasi"] == "santamaria" and r1["langkah"][1]["peran"] == "owner", "rencana putaran 1")
ev2 = json.loads((sesi / "putaran_2" / "evaluasi.json").read_text(encoding="utf-8"))
cek([t["tingkat"] for t in ev2["temuan"]] == ["tinggi", "sedang", "sedang"], f"urut tingkat: {[t['tingkat'] for t in ev2['temuan']]}")
cek(ev2["temuan"][2]["kategori"] == "lainnya", "kategori tak dikenal -> lainnya")
cek(any(b.get("catatan_validasi") for t in ev2["temuan"] for b in t["bukti"]), "ref palsu harus ditandai")
lap = json.loads((sesi / "laporan.json").read_text(encoding="utf-8"))
st = lap["pengukuran"]["statistik_berulang"]
cek(any(x["langkah"] == "buka_laporan" and x["n"] >= 3 and 1100 <= x["median_ms"] <= 2500 for x in st), f"statistik: {st}")
cek(any(x["langkah"] == "buka_laporan" for x in lap["pengukuran"]["langkah_lambat"]), "laporan >1 s harus tercatat lambat")
cek(lap["pengukuran"]["startup"] and lap["pengukuran"]["startup"][0]["total_time_ms"] == 812, "startup")
cek(any(x["jumlah_tanpa_label"] >= 1 and x["jumlah_target_kecil"] >= 1 for x in lap["pengukuran"]["layar"]), "masalah layar")
html_lap = (sesi / "laporan.html").read_text(encoding="utf-8")
cek(html_lap.count("data:image/jpeg;base64,") >= 3 and "Ikon pojok kanan atas tanpa label" in html_lap, "HTML + gambar")
md = (sesi / "laporan.md").read_text(encoding="utf-8")
cek("## Temuan dan rekomendasi" in md and "putaran_" not in md.split("## Temuan")[0], "markdown")
frame = list((sesi / "putaran_2" / "frame").glob("*.jpg")) + list((sesi / "putaran_1" / "frame").glob("*.jpg"))
cek(len(frame) >= 2 or not ork.ffmpeg(), f"frame rekaman langkah lambat harus diambil: {frame}")
font = [p for p in SIM.panggilan if p[0] == "adb" and "font_scale" in p[1]]
cek(any("put system font_scale 1.3" in p[1] for p in font) and font[-1][1].endswith("font_scale 1"),
    f"skala font harus diatur lalu dikembalikan: {font}")
print(f"3. siklus owner: OK -> 2 putaran, {len(lap['temuan'])} temuan, frame={len(frame)}, statistik={st[0]['median_ms']} ms")

# isi permintaan ke LLM
req = ST.permintaan[n0:]
perancang = [r["body"] for r in req if "LLM PERANCANG UJI" in json.dumps(r["body"]["messages"][0])]
evaluator = [r["body"] for r in req if "LLM EVALUATOR UI/UX" in json.dumps(r["body"]["messages"][0])]
gambar = lambda b: sum(1 for p in b["messages"] if isinstance(p["content"], list) for x in p["content"] if x.get("type") == "image_url")  # noqa: E731
cek(perancang and all(gambar(b) >= 1 for b in perancang), "perancang harus menerima screenshot")
cek(evaluator and 3 <= gambar(evaluator[-1]) <= 14, f"evaluator: jumlah gambar {gambar(evaluator[-1])}")
cek(perancang[0]["reasoning_effort"] == "low" and evaluator[-1]["reasoning_effort"] == "medium", "reasoning_effort")
teks_eval = json.dumps(evaluator[-1]["messages"][1]["content"][0]["text"], ensure_ascii=False)
cek("EVALUASI SEBELUMNYA" in teks_eval and "P2." in teks_eval and "statistik_berulang" in teks_eval, "isi prompt evaluator")
cek("izin diberikan otomatis lewat adb sebelum uji: " in teks_eval and "SYSTEM_ALERT_WINDOW" in teks_eval,
    "evaluator harus tahu izin diberikan otomatis")
obs = json.loads((sesi / "observasi" / "ringkasan.json").read_text(encoding="utf-8"))
cek(obs["beri_izin_otomatis"] is True and obs["izin_otomatis"][0]["saat"] == "awal uji", f"izin observasi: {obs['izin_otomatis']}")
teks_p2 = perancang[-1]["messages"][1]["content"][0]["text"]
cek("PERMINTAAN EVALUATOR" in teks_p2 and "Laporan Keuangan" in teks_p2, "perancang putaran 2 harus melihat permintaan & peta layar")
print("   prompt: perancang", [gambar(b) for b in perancang], "gambar; evaluator", [gambar(b) for b in evaluator], "gambar")

# ---------------------------------------------------------------- 4. langkah terpisah ala n8n (consumer)
tujuan = "Uji konsumen: kejelasan layanan & aksesibilitas lansia"
kode, h, err = cli("mulai", "--app", "santamaria", "--peran", "consumer",
                   "--tujuan-b64", base64.b64encode(tujuan.encode()).decode(), "--maks-putaran", "1", "--tanpa-rekam")
cek(kode == 0 and h["lanjut"], f"mulai: {h}\n{err[-2000:]}")
s2 = h["sesi"]
cek(json.loads(Path(s2, "sesi.json").read_text(encoding="utf-8"))["tujuan"] == tujuan, "tujuan-b64")
for perintah in ("rancang", "jalankan", "evaluasi"):
    kode, h, err = cli(perintah, "--sesi", s2)
    cek(kode == 0 and h["status"] == "ok" and h["sesi"] == s2, f"{perintah}: {h}\n{err[-2000:]}")
cek(h["lanjut"] is False and "putaran maksimal" in h["alasan"], f"maks putaran 1 harus menghentikan loop: {h}")
kode, h, err = cli("rancang", "--sesi", s2)
cek(kode == 2 and h["status"] == "error" and "tidak meminta uji ulang" in h["pesan"], f"rancang setelah selesai: {h}")
kode, h, err = cli("laporan", "--sesi", Path(s2).name)
cek(kode == 0 and Path(h["laporan_html"]).exists(), f"laporan via nama folder: {h}")
print("4. langkah terpisah (n8n): OK ->", Path(s2).name)

# ---------------------------------------------------------------- 5. ringkasan
kode, h, _ = cli("ringkasan", "--sesi", str(sesi), s2, "folder_tidak_ada")
cek(kode == 0 and h["jumlah_sesi"] == 3 and Path(h["ringkasan_html"]).exists(), f"ringkasan: {h}")
ring = Path(h["ringkasan_html"]).read_text(encoding="utf-8")
cek(ring.count("<a href=") == 2 and "folder_tidak_ada" in ring, "ringkasan isi")
print("5. ringkasan: OK ->", Path(h["ringkasan_html"]).name)

# ---------------------------------------------------------------- 6. kesalahan yang rapi
kode, h, _ = cli("mulai", "--app", "santamaria", "--peran", "presiden")
cek(kode == 2 and "presiden" in h["pesan"], f"peran salah: {h}")
kode, h, _ = cli("jalankan", "--sesi", "/tidak/ada")
cek(kode == 2 and "sesi tidak ditemukan" in h["pesan"], f"sesi salah: {h}")
mati = ork.KlienLLM({**ork.muat_llm(), "base_url": "http://127.0.0.1:9/v1"}, lambda t: t)
try:
    mati.chat([{"role": "user", "content": "hai"}], "evaluator", "x")
    cek(False, "harus gagal")
except ork.GagalOrkestrasi as e:
    cek("9router" in str(e), f"pesan koneksi ditolak: {e}")
print("6. pesan kesalahan: OK")

# ---------------------------------------------------------------- 7. rahasia tidak pernah terkirim ke LLM
semua = json.dumps([r["body"] for r in ST.permintaan], ensure_ascii=False)
for rahasia in ag.nilai_rahasia(ag.muat_konfigurasi(os.environ["UJI_UI_AKUN"])):
    cek(rahasia not in semua, f"nilai akun bocor ke LLM: {rahasia[:4]}...")
cek("@contoh.test" not in semua, "email akun bocor ke LLM")
print(f"7. tidak ada data akun di {len(ST.permintaan)} permintaan LLM: OK")

# ---------------------------------------------------------------- 8. ambil_json & sensor
cek(ork.ambil_json('teks {"a": 1} lagi {"b": 2}') == {"a": 1}, "ambil_json pertama")
cek(ork.ambil_json('```json\n{"x": [1, {"y": 2}]}\n```') == {"x": [1, {"y": 2}]}, "ambil_json fence")
try:
    ork.ambil_json("tidak ada json")
    cek(False, "harus error")
except ValueError:
    cek(True, "")

# ---------------------------------------------------------------- 9. kolom berisi data akun ditutup di gambar
from PIL import Image  # noqa: E402
sensor = ork.Sensor(ag.muat_konfigurasi(os.environ["UJI_UI_AKUN"]))
obs = sesi / "observasi"
xml = (obs / "02_login_owner_isi_identitas.xml").read_text(encoding="utf-8")
elemen = ag.daftar_elemen(xml, 2.75, maks=200, kotak=True)
tutup = ork.kotak_rahasia(elemen, sensor)
cek(len(tutup) == 1 and tutup[0] == [72, 1062, 1148, 1236], f"kolom email harus ditutup: {tutup}")
url = ork.gambar_url(obs / "02_login_owner_isi_identitas.png", 1220, tutup=tutup, kualitas=95)
im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
abu = sum(1 for x in range(900, 1140, 6) for y in range(1150, 1230, 6) if all(abs(c - 226) < 12 for c in im.getpixel((x, y))))
cek(abu > 0.9 * len(range(900, 1140, 6)) * len(range(1150, 1230, 6)), f"area tertutup harus abu-abu rata ({abu})")
run0 = next(r for r in ork.bangun_peta(ork.Sesi(sesi))["runs"] if r["id"] == "P0")
tf = ork._tutup_untuk_frame(run0, 4, sensor)
cek(tf.get("lebar_asal") == 1220 and tf.get("tutup") == tutup, f"frame saat login harus ikut ditutup: {tf}")
login = next(x for x in ork.bangun_peta(ork.Sesi(sesi))["layar"] if "Masuk" in x["judul"])
a1 = next(m for m in ork.masalah_berkode(login) if m["kode"] == "A1")
cek("kontainer hampir selayar" in a1.get("catatan", ""), f"catatan kontainer: {a1}")
print("9. sensor gambar + catatan kontainer: OK")

# ---------------------------------------------------------------- 10. Wasiat (Google SSO): layar Google tidak ke LLM
n0 = len(ST.permintaan)
kode, h, err = cli("mulai", "--app", "wasiat", "--tanpa-rekam")
cek(kode == 0 and h["peran"] == "pengguna_google", f"mulai wasiat: {h}\n{err[-1500:]}")
sw = ork.Sesi(h["sesi"])
peta = ork.bangun_peta(sw)
sso = [f"P0.{l['no']}" for l in peta["runs"][0]["ringkasan"]["langkah"] if l.get("privasi")]
cek(sso and not any(r in peta["ref_layar"] for r in sso), f"langkah SSO tidak boleh jadi layar: {sso}")
kode, h, err = cli("rancang", "--sesi", str(sw.folder))
cek(kode == 0 and h["putaran"] == 1, f"rancang wasiat: {h}\n{err[-1500:]}")
teks_req = json.dumps([r["body"] for r in ST.permintaan[n0:]], ensure_ascii=False)
cek(len(ST.permintaan) > n0 and "@gmail.com" not in teks_req and "Beranda Wasiat" in teks_req, "isi permintaan wasiat")
print("10. Wasiat SSO di orkestrator: OK ->", sso)

# ---------------------------------------------------------------- 11. mode jelajah menyeluruh
n0 = len(ST.permintaan)
kode, h, err = cli("siklus", "--app", "santamaria", "--peran", "owner", "--jelajah", "--maks-putaran", "3", "--tanpa-rekam")
cek(kode == 0, f"siklus jelajah: {h}\n{err[-2000:]}")
sj = Path(h["sesi"])
dj = json.loads((sj / "sesi.json").read_text(encoding="utf-8"))
cek(len(dj["putaran"]) == 3, f"jelajah: harus 3 putaran, dapat {len(dj['putaran'])}")
p2 = dj["putaran"][1]
cek(p2["lanjut"] is True and "mode jelajah menyeluruh" in p2["alasan_keputusan"] and p2["keyakinan"] >= 0.75,
    f"putaran 2 harus lanjut karena masih ada elemen belum dijelajah walau evaluator yakin: {p2}")
cek("putaran maksimal (3)" in dj["putaran"][2]["alasan_keputusan"], f"putaran 3: {dj['putaran'][2]}")
pr = [r["body"] for r in ST.permintaan[n0:] if "LLM PERANCANG UJI" in json.dumps(r["body"]["messages"][0])]
cek("ELEMEN BELUM DIJELAJAH - mode jelajah menyeluruh AKTIF" in pr[-1]["messages"][1]["content"][0]["text"],
    "perancang harus menerima daftar elemen yang belum dijelajah")
lj = json.loads((sj / "laporan.json").read_text(encoding="utf-8"))
ce = lj["cakupan_elemen"]
cek(ce["total"] > 0 and ce["sudah"] >= 1 and all(x["label"] != "Keluar" for x in ce["belum"])
    and not any(x["label"] in ("Masuk", "Konsumen", "Periksa ulang") for x in ce["belum"]),
    f"cakupan elemen (tanpa tombol berisiko, form login, gerbang izin): {ce}")
cek("Elemen yang bisa diklik:" in (sj / "laporan.md").read_text(encoding="utf-8"), "cakupan di laporan")
print(f"11. jelajah menyeluruh: OK -> cakupan {ce['sudah']}/{ce['total']}, belum: {[x['label'] for x in ce['belum']][:6]}")

print(f"\nSEMUA {lulus} CEK LULUS (hasil di {TMP})")
