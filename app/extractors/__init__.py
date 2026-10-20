from __future__ import annotations

from ..config import Settings


def get_extractor(settings: Settings, backend: str | None = None):
    """Retourne un extracteur ayant `.extract(PIL.Image) -> Extraction` et `.cache_id`."""
    backend = (backend or settings.resolved_backend()).lower()
    if backend == "auto":
        backend = settings.resolved_backend()
    if backend == "claude":
        if not settings.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY manquante : renseignez-la dans .env ou choisissez 'tesseract'.")
        from .claude_vision import ClaudeVisionExtractor

        return ClaudeVisionExtractor(settings.api_key, settings.model)
    if backend == "tesseract":
        from .tesseract_local import TesseractExtractor

        return TesseractExtractor(settings.tesseract_lang)
    raise ValueError(f"backend inconnu : {backend}")
