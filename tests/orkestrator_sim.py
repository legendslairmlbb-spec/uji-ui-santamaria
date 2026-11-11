"""Pembungkus orkestrator.py untuk uji workflow n8n: HP = simulator, LLM = mock_llm (proses terpisah)."""
import os
import sys
from pathlib import Path

AKAR = Path(__file__).resolve().parent.parent
os.environ.setdefault("UJI_UI_AKUN", str(AKAR / "tests" / "fixtures" / "akun_uji.json"))
sys.path.insert(0, str(AKAR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agen_uji  # noqa: E402
import simulator  # noqa: E402
import orkestrator  # noqa: E402

simulator.pasang(agen_uji)
sys.exit(orkestrator.main(sys.argv[1:]))
