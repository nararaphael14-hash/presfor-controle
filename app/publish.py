"""Synchronisation d'une campagne contrôlée en LOCAL vers le serveur central (facultative, à la demande).

Le poste du contrôleur travaille seul (aucune dépendance au serveur). Quand il le décide, « Synchroniser » envoie :
la campagne (contrats, saisies manuelles, lectures) puis les pièces (JPEG réduits) par petits lots avec reprise.
Configuration locale (.env) : CENTRAL_URL, CENTRAL_USER (défaut admin), CENTRAL_PASSWORD.
"""
from __future__ import annotations

import io
import logging
import os
import threading

import httpx
from fastapi import APIRouter, Form, HTTPException

from .campaign_api import store
from .images import load_page, to_jpeg_bytes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/campaigns", tags=["publication"])
JOBS: dict[str, dict] = {}
BATCH_BYTES = 6_000_000


def central() -> dict | None:
    url = os.getenv("CENTRAL_URL", "").rstrip("/")
    if not url:
        return None
    return {"url": url, "auth": (os.getenv("CENTRAL_USER", "admin"), os.getenv("CENTRAL_PASSWORD", ""))}


def _payload(path, name: str) -> tuple[str, bytes, str]:
    """Pièce à envoyer : JPEG réduit à 1800 px (le nom est conservé) ; les autres formats partent tels quels."""
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return name, to_jpeg_bytes(load_page(path, 0, max_side=3600), max_side=1800, quality=82), "image/jpeg"
    return name, path.read_bytes(), "application/octet-stream"


def _run(cid: str, overwrite: bool, job: dict) -> None:
    cfg = central()
    try:
        st = store()
        c = st.load(cid)
        if c is None:
            raise RuntimeError("campagne introuvable en local")
        names = st.scan_names(cid)
        job.update(total=len(names), done=0, step="campagne")
        with httpx.Client(base_url=cfg["url"], auth=cfg["auth"], timeout=120.0) as cl:
            r = cl.post("/api/campaigns/restore", json={"campaign": c, "ocr": st.load_ocr(cid), "overwrite": overwrite})
            if r.status_code == 401:
                raise RuntimeError("identifiants du serveur central refusés (CENTRAL_USER / CENTRAL_PASSWORD)")
            r.raise_for_status()
            if not r.json().get("restored") and not overwrite:
                job["note"] = "campagne déjà présente sur le serveur central : pièces complétées, saisies non écrasées"
            job["step"] = "pièces"
            files, size = [], 0

            def flush() -> None:
                nonlocal files, size
                if not files:
                    return
                for attempt in range(6):
                    try:
                        rr = cl.post(f"/api/campaigns/{cid}/import", data={"light": "1"},
                                     files=[("files", f) for f in files])
                        if rr.status_code in (429, 502, 503, 504):
                            raise httpx.HTTPError(f"HTTP {rr.status_code}")
                        rr.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 5:
                            raise
                        threading.Event().wait(5 * (attempt + 1))
                job["done"] += len(files)
                files, size = [], 0

            for n in names:
                p = st.scans_dir(cid) / n
                if not p.is_file():
                    job["done"] += 1
                    continue
                name, data, mime = _payload(p, n)
                files.append((name, io.BytesIO(data), mime))
                size += len(data)
                if size >= BATCH_BYTES:
                    flush()
            flush()
        job.update(status="done", step="terminé")
    except Exception as e:  # noqa: BLE001
        log.exception("synchronisation vers le serveur central impossible")
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@router.post("/{cid}/publish")
def publish(cid: str, overwrite: str = Form("")) -> dict:
    if not central():
        raise HTTPException(400, "serveur central non configuré (CENTRAL_URL dans .env)")
    if (j := JOBS.get(cid)) and j["status"] == "running":
        return j
    JOBS[cid] = {"status": "running", "total": 0, "done": 0, "step": "démarrage", "error": None, "note": ""}
    threading.Thread(target=_run, args=(cid, bool(overwrite), JOBS[cid]), daemon=True).start()
    return JOBS[cid]


@router.get("/{cid}/publish/status")
def publish_status(cid: str) -> dict:
    return JOBS.get(cid) or {"status": "idle", "total": 0, "done": 0}
