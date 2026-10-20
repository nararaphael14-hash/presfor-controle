"""Qualité d'un scan (flou, exposition, résolution) et similarité entre deux scans — OpenCV, sans modèle externe."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Metrics:
    ok: bool = True               # image décodable
    width: int = 0
    height: int = 0
    sharp: float = 0.0            # netteté normalisée (indépendante du contraste)
    mean: float = 0.0
    std: float = 0.0
    dhash: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _load(path: Path):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _gray_small(img, side: int = 1000):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    s = side / max(g.shape)
    return cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else g


def dhash(g, size: int = 8) -> int:
    r = cv2.resize(g, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (r[:, 1:] > r[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def sharpness(g) -> float:
    """Gradient moyen des 15 % de pixels les plus contrastés / écart-type global : un texte net a des arêtes fortes,
    un scan flou n'en a pas, quelle que soit la luminosité de la photo."""
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy).ravel()
    top = np.partition(mag, int(mag.size * 0.85))[int(mag.size * 0.85):]
    return float(top.mean() / (g.std() + 10.0))


def measure(path: Path) -> tuple[Metrics, np.ndarray | None]:
    img = _load(path)
    if img is None or min(img.shape[:2]) < 8:
        return Metrics(ok=False), None
    g = _gray_small(img)
    h, w = img.shape[:2]
    return Metrics(True, w, h, round(sharpness(g), 3), round(float(g.mean()), 1), round(float(g.std()), 1), dhash(g)), g


def similar_views(g1, g2, min_inliers: int = 35) -> int:
    """Nombre de correspondances ORB cohérentes (RANSAC) : ≥ min_inliers => même face photographiée deux fois."""
    orb = cv2.ORB_create(1500)
    k1, d1 = orb.detectAndCompute(g1, None)
    k2, d2 = orb.detectAndCompute(g2, None)
    if d1 is None or d2 is None or len(k1) < 15 or len(k2) < 15:
        return 0
    m = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
    if len(m) < 15:
        return 0
    p1 = np.float32([k1[x.queryIdx].pt for x in m]).reshape(-1, 1, 2)
    p2 = np.float32([k2[x.trainIdx].pt for x in m]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(p1, p2, cv2.RANSAC, 6.0)
    return int(mask.sum()) if mask is not None else 0
