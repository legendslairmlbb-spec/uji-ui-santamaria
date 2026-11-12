"""Bangun file workflow n8n (JSON) untuk loop LLM Perancang -> Pelaksana -> LLM Evaluator -> laporan."""
import json
import sys
import uuid

PY = "{{ $('Konfigurasi').first().json.python }}"
SKRIP = "\"{{ $('Konfigurasi').first().json.skrip }}\""
SESI = "\"{{ JSON.parse($json.stdout.trim().split('\\n').pop()).sesi }}\""


def perintah(isi):
    return f"={PY} {SKRIP} {isi}"


def node(nama, jenis, versi, posisi, parameter, **lain):
    return {"parameters": parameter, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "uji-ui/" + nama)), "name": nama,
            "type": jenis, "typeVersion": versi, "position": posisi, **lain}


def exec_node(nama, posisi, isi, sekali=False, catatan=None):
    extra = {"notes": catatan, "notesInFlow": True} if catatan else {}
    return node(nama, "n8n-nodes-base.executeCommand", 1, posisi,
                {"executeOnce": sekali, "command": perintah(isi)}, **extra)


def buat(python="python", skrip="%USERPROFILE%\\uji-ui\\orkestrator.py"):
    catatan = (
        "## Uji UI/UX otomatis (overtesting): penjelajah + LLM Perancang + Pelaksana + LLM Evaluator\n"
        "Untuk tiap skenario di **skenario.json**: observasi awal di HP -> putaran 1 **penjelajah otomatis** (node LLM "
        "Perancang menyiapkannya tanpa LLM, node Pelaksana mengetuk semua elemen yang bisa dijangkau) -> **LLM Evaluator** "
        "menilai -> **LLM Perancang** menyusun uji mendalam (form, CRUD dengan pemulihan data, teks besar, tema gelap, "
        "tanpa internet) -> **Pelaksana** -> **LLM Evaluator** -> ... Loop berhenti kalau semua elemen, tugas uji, dan "
        "layar sudah tercakup dan data uji sudah dipulihkan (atau batas putaran/waktu di skenario.json). Lalu laporan; "
        "di akhir ada ringkasan semua aplikasi.\n\n"
        "**Sebelum Execute workflow:** 9router jalan, `appium` jalan di terminal lain (Inspector ditutup), HP "
        "tersambung (`adb devices`), dan n8n dijalankan dengan `$env:NODES_EXCLUDE='[]'` supaya node Execute "
        "Command aktif. Ubah lokasi Python/skrip di node **Konfigurasi**.\n\n"
        "Hasil: `C:\\Users\\<kamu>\\.n8n-files\\uji-ui\\sesi_.../laporan.html`. Satu skenario overtesting bisa 1-3 jam."
    )
    nodes = [
        node("Catatan", "n8n-nodes-base.stickyNote", 1, [-40, -240],
             {"content": catatan, "height": 300, "width": 760, "color": 7}),
        node("Mulai manual", "n8n-nodes-base.manualTrigger", 1, [0, 300], {}),
        node("Konfigurasi", "n8n-nodes-base.set", 3.4, [220, 300], {
            "assignments": {"assignments": [
                {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "uji-ui/python")), "name": "python", "value": python, "type": "string"},
                {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "uji-ui/skrip")), "name": "skrip", "value": skrip, "type": "string"},
            ]}, "options": {}}),
        exec_node("Daftar skenario", [440, 300], "daftar-skenario", sekali=True),
        node("Pecah per skenario", "n8n-nodes-base.code", 2, [660, 300], {"jsCode": (
            "// Satu item per skenario dari skenario.json.\n"
            "// Untuk menjalankan sebagian saja, misalnya: .filter(s => [0, 2].includes(s.no))\n"
            "const hasil = JSON.parse($input.first().json.stdout.trim().split('\\n').pop());\n"
            "return hasil.skenario\n"
            "  .map(s => ({ json: { no: s.no, aplikasi: s.aplikasi, peran: s.peran, tujuan: s.tujuan } }));\n")}),
        node("Per skenario", "n8n-nodes-base.splitInBatches", 3, [880, 300], {"options": {}}),
        exec_node("Mulai sesi + observasi (HP)", [1100, 420], "mulai --skenario {{ $json.no }}",
                  catatan="buka aplikasi, login, tangkap layar awal"),
        exec_node("LLM Perancang", [1320, 420], f"rancang --sesi {SESI}", catatan="menyusun rencana uji (JSON)"),
        exec_node("Pelaksana (Appium di HP)", [1540, 420], f"jalankan --sesi {SESI}", catatan="menjalankan rencana, ukur waktu & metrik"),
        exec_node("LLM Evaluator", [1760, 420], f"evaluasi --sesi {SESI}", catatan="menilai, menentukan yakin/uji ulang"),
        node("Perlu uji ulang?", "n8n-nodes-base.if", 2.2, [1980, 420], {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "uji-ui/lanjut")),
                    "leftValue": "={{ JSON.parse($json.stdout.trim().split('\\n').pop()).lanjut }}",
                    "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                "combinator": "and"},
            "options": {}}),
        exec_node("Buat laporan", [2200, 560], f"laporan --sesi {SESI}", catatan="laporan.html / .md / .json"),
        exec_node("Ringkasan semua aplikasi", [1100, 140],
                  "ringkasan --sesi {{ $input.all().map(i => '\"' + JSON.parse(i.json.stdout.trim().split('\\n').pop()).sesi + '\"').join(' ') }}",
                  sekali=True),
        node("Hasil akhir", "n8n-nodes-base.code", 2, [1320, 140], {"jsCode": (
            "const r = JSON.parse($input.first().json.stdout.trim().split('\\n').pop());\n"
            "return [{ json: { ringkasan_html: r.ringkasan_html, ringkasan_md: r.ringkasan_md, jumlah_sesi: r.jumlah_sesi } }];\n")}),
    ]

    def ke(nama, i=0):
        return {"node": nama, "type": "main", "index": i}

    conn = {
        "Mulai manual": {"main": [[ke("Konfigurasi")]]},
        "Konfigurasi": {"main": [[ke("Daftar skenario")]]},
        "Daftar skenario": {"main": [[ke("Pecah per skenario")]]},
        "Pecah per skenario": {"main": [[ke("Per skenario")]]},
        "Per skenario": {"main": [[ke("Ringkasan semua aplikasi")], [ke("Mulai sesi + observasi (HP)")]]},
        "Mulai sesi + observasi (HP)": {"main": [[ke("LLM Perancang")]]},
        "LLM Perancang": {"main": [[ke("Pelaksana (Appium di HP)")]]},
        "Pelaksana (Appium di HP)": {"main": [[ke("LLM Evaluator")]]},
        "LLM Evaluator": {"main": [[ke("Perlu uji ulang?")]]},
        "Perlu uji ulang?": {"main": [[ke("LLM Perancang")], [ke("Buat laporan")]]},
        "Buat laporan": {"main": [[ke("Per skenario")]]},
        "Ringkasan semua aplikasi": {"main": [[ke("Hasil akhir")]]},
    }
    return {"name": "Uji UI/UX - LLM Perancang & Evaluator (3 aplikasi)", "nodes": nodes, "connections": conn,
            "settings": {"executionOrder": "v1"}, "pinData": {}, "active": False,
            "id": "UjiUiPerancangEval"}


if __name__ == "__main__":
    keluar = sys.argv[1]
    kw = {}
    if len(sys.argv) > 3:
        kw = {"python": sys.argv[2], "skrip": sys.argv[3]}
    with open(keluar, "w", encoding="utf-8") as f:
        json.dump(buat(**kw), f, ensure_ascii=False, indent=2)
    print("ditulis:", keluar)
