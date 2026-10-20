"""Chargement des scans (images / PDF) et préparation pour l'extraction."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
SUPPORTED_EXT = IMAGE_EXT | PDF_EXT

Image.MAX_IMAGE_PIXELS = 120_000_000  # garde-fou contre les images piégées


def load_pages(path: Path, max_pdf_pages: int = 4, dpi: int = 200) -> list[Image.Image]:
    """Retourne la liste des pages (1 pour une image, N pour un PDF)."""
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        import fitz  # PyMuPDF

        pages: list[Image.Image] = []
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= max_pdf_pages:
                    break
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        return pages
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return [img.convert("RGB")]


def count_pages(path: Path, max_pdf_pages: int = 4) -> int:
    if path.suffix.lower() in PDF_EXT:
        import fitz

        with fitz.open(path) as doc:
            return max(1, min(len(doc), max_pdf_pages))
    return 1


def load_page(path: Path, index: int = 0, dpi: int = 200) -> Image.Image:
    if path.suffix.lower() in PDF_EXT:
        import fitz

        with fitz.open(path) as doc:
            pix = doc[index].get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    img = ImageOps.exif_transpose(Image.open(path))
    return img.convert("RGB")


def to_jpeg_bytes(img: Image.Image, max_side: int = 1568, quality: int = 90) -> bytes:
    """JPEG redimensionné (1568 px = taille utile maximale pour la vision de Claude)."""
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()
