"""Configuration centralisée (variables d'environnement / fichier .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- Extraction ---
    backend: str = os.environ.get("EXTRACTOR_BACKEND", "auto").lower()  # auto|claude|tesseract
    api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    tesseract_lang: str = os.environ.get("TESSERACT_LANG", "fra+eng")
    workers: int = int(_f("WORKERS", 4))
    cache_dir: Path = Path(os.environ.get("CACHE_DIR", ROOT / "data" / "cache"))
    jobs_dir: Path = Path(os.environ.get("JOBS_DIR", ROOT / "data" / "jobs"))
    # --- Seuils de comparaison (0-100) ---
    name_tolerate: float = _f("NAME_TOLERATE", 90)   # >= : nom accepté avec tolérance
    name_close: float = _f("NAME_CLOSE", 80)         # >= : nom proche, à vérifier
    link_threshold: float = _f("LINK_THRESHOLD", 80)  # rattachement scan -> ligne
    min_confidence: float = _f("MIN_CONFIDENCE", 0.6)  # confiance minimale d'une lecture (0-1)

    def resolved_backend(self) -> str:
        if self.backend in ("claude", "tesseract"):
            return self.backend
        return "claude" if self.api_key else "tesseract"


def get_settings() -> Settings:
    return Settings()
