"""API FastAPI + interface web.  Lancement : uvicorn app.server:app --reload"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .campaign_api import router as campaign_router
from .config import ROOT, Settings, get_settings
from .csv_loader import build_rows, guess_mapping, load_csv
from .export import to_xlsx
from .extractors import get_extractor
from .images import SUPPORTED_EXT, load_page, to_jpeg_bytes
from .pipeline import (ScanItem, collect_scans, extract_all, extract_scan, results_to_dict, run_control,
                       summarize)

log = logging.getLogger("presfor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="PRESFOR — Contrôle des contrats", version="3.0")
STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Authentification HTTP Basic si APP_PASSWORD est défini (OBLIGATOIRE hors poste local)."""
    pwd = os.environ.get("APP_PASSWORD", "")
    if not pwd or request.url.path == "/healthz":
        return await call_next(request)
    user = os.environ.get("APP_USER", "admin")
    try:
        got = base64.b64decode(request.headers.get("authorization", "")[6:]).decode()
    except Exception:  # noqa: BLE001
        got = ""
    if not secrets.compare_digest(got, f"{user}:{pwd}"):
        return Response("Authentification requise", 401, headers={"WWW-Authenticate": 'Basic realm="PRESFOR"'})
    return await call_next(request)


app.include_router(campaign_router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}

MAX_ZIP_BYTES = 1_500_000_000
MAX_ZIP_FILES = 3000
JOB_TTL_S = 24 * 3600


@dataclass
class Job:
    id: str
    dir: Path
    backend: str
    status: str = "pending"          # pending | extracting | done | error
    done: int = 0
    total: int = 0
    error: str | None = None
    results: list = field(default_factory=list)
    orphans: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    scans: list[ScanItem] = field(default_factory=list)
    created: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def _purge_old_jobs(settings: Settings) -> None:
    now = time.time()
    for jid in [j for j, job in JOBS.items() if now - job.created > JOB_TTL_S and job.status != "extracting"]:
        shutil.rmtree(JOBS[jid].dir, ignore_errors=True)
        JOBS.pop(jid, None)


def _save_upload(up: UploadFile, dest: Path) -> None:
    with dest.open("wb") as f:
        shutil.copyfileobj(up.file, f)


def _unzip_safe(zpath: Path, dest: Path) -> list[Path]:
    out: list[Path] = []
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise HTTPException(413, "archive trop volumineuse")
        for i in infos:
            name = Path(i.filename).name  # jamais de chemin : pas de zip-slip
            if Path(name).suffix.lower() not in SUPPORTED_EXT or name.startswith("."):
                continue
            target = dest / name
            n = 1
            while target.exists():
                target = dest / f"{target.stem}__{n}{target.suffix}"
                n += 1
            with z.open(i) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def _worker(job: Job, rows, settings: Settings) -> None:
    try:
        job.status = "extracting"
        extractor = get_extractor(settings, job.backend)
        job.scans = collect_scans(sorted((job.dir / "scans").iterdir()))
        job.total = len(job.scans)

        def cb(done: int, total: int) -> None:
            job.done, job.total = done, total

        extract_all(job.scans, extractor, settings, cb)
        results, orphans = run_control(rows, job.scans, settings)
        job.results, job.orphans = results_to_dict(results), orphans
        job.summary = summarize(results)
        job.summary["SCANS"] = len(job.scans)
        job.summary["SCANS_NON_RATTACHES"] = len(orphans)
        job.status = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("job %s en erreur", job.id)
        job.status, job.error = "error", f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ routes
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "app.html")


@app.get("/static/jszip.min.js", include_in_schema=False)
def jszip() -> FileResponse:
    return FileResponse(STATIC / "jszip.min.js", media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/identite", include_in_schema=False)
def identity_tool() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "backend_par_defaut": s.resolved_backend(), "modele": s.model,
            "cle_api_presente": bool(s.api_key)}


@app.post("/api/csv/preview")
async def csv_preview(csv_file: UploadFile = File(...), tables: str = Form("")) -> dict:
    data = await csv_file.read()
    try:
        loaded = load_csv(data, json.loads(tables) if tables else None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"CSV illisible : {e}") from e
    mapping = guess_mapping(loaded.headers)
    rows = build_rows(loaded, mapping)
    return {"kind": loaded.kind, "tables": loaded.tables, "selected_tables": loaded.selected_tables,
            "headers": loaded.headers, "mapping": mapping, "n_rows": len(rows),
            "sample": [{"nom_complet": r.full, "numero": r.numero, "code": r.code, "role": r.role} for r in rows[:5]]}


@app.post("/api/extract")
async def extract_one(file: UploadFile = File(...), backend: str = Form("auto")) -> dict:
    """Extraction d'UNE pièce -> JSON {type_piece, face, nom, prenoms, numero_piece, confiance, …}."""
    settings = get_settings()
    name = Path(file.filename or "scan").name
    if Path(name).suffix.lower() not in SUPPORTED_EXT:
        raise HTTPException(400, "format non pris en charge (jpg, png, webp, tif, pdf)")
    tmp = settings.jobs_dir / "_single"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    try:
        _save_upload(file, path)
        try:
            extractor = get_extractor(settings, backend)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        scan = collect_scans([path])[0]
        ex = extract_scan(scan, extractor, settings.cache_dir)
        return ex.model_dump()
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/jobs")
async def create_job(csv_file: UploadFile = File(...), scans: list[UploadFile] = File(...),
                     mapping: str = Form(""), tables: str = Form(""), backend: str = Form("auto")) -> dict:
    settings = get_settings()
    _purge_old_jobs(settings)
    try:
        backend = (backend or "auto").lower()
        get_extractor(settings, backend)  # échoue tôt (clé manquante, tesseract absent…)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    jid = uuid.uuid4().hex[:12]
    jdir = settings.jobs_dir / jid
    (jdir / "scans").mkdir(parents=True)
    try:
        loaded = load_csv(await csv_file.read(), json.loads(tables) if tables else None)
        m = json.loads(mapping) if mapping else guess_mapping(loaded.headers)
        rows = build_rows(loaded, m)
        if not rows:
            raise HTTPException(400, "aucune ligne exploitable dans le CSV (vérifiez le mappage des colonnes)")
        for up in scans:
            name = Path(up.filename or "").name
            if not name or name.startswith("."):
                continue
            ext = Path(name).suffix.lower()
            dest = jdir / "scans" / name
            if ext == ".zip":
                zp = jdir / f"{uuid.uuid4().hex}.zip"
                _save_upload(up, zp)
                _unzip_safe(zp, jdir / "scans")
                zp.unlink(missing_ok=True)
            elif ext in SUPPORTED_EXT:
                n = 1
                while dest.exists():
                    dest = jdir / "scans" / f"{Path(name).stem}__{n}{ext}"
                    n += 1
                _save_upload(up, dest)
        if not any((jdir / "scans").iterdir()):
            raise HTTPException(400, "aucun scan exploitable (jpg, png, webp, tif, pdf ou zip)")
    except Exception:
        shutil.rmtree(jdir, ignore_errors=True)
        raise
    job = Job(jid, jdir, backend)
    with _LOCK:
        JOBS[jid] = job
    threading.Thread(target=_worker, args=(job, rows, settings), daemon=True).start()
    return {"job_id": jid, "n_rows": len(rows)}


def _job(jid: str) -> Job:
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "job introuvable (expiré ou serveur redémarré)")
    return job


@app.get("/api/jobs/{jid}")
def job_status(jid: str, full: bool = True) -> dict:
    job = _job(jid)
    out = {"id": job.id, "status": job.status, "done": job.done, "total": job.total, "error": job.error,
           "backend": job.backend}
    if job.status == "done" and full:
        out.update(results=job.results, orphans=job.orphans, summary=job.summary)
    return out


@app.get("/api/jobs/{jid}/export.xlsx")
def export_xlsx(jid: str) -> Response:
    job = _job(jid)
    if job.status != "done":
        raise HTTPException(409, "traitement en cours")
    return Response(to_xlsx(job.results, job.orphans, job.summary),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="controle_{jid}.xlsx"'})


@app.get("/api/jobs/{jid}/export.json")
def export_json(jid: str) -> JSONResponse:
    job = _job(jid)
    if job.status != "done":
        raise HTTPException(409, "traitement en cours")
    return JSONResponse({"summary": job.summary, "results": job.results, "orphans": job.orphans},
                        headers={"Content-Disposition": f'attachment; filename="controle_{jid}.json"'})


@app.get("/api/jobs/{jid}/scan/{sid}")
def job_scan(jid: str, sid: int) -> Response:
    job = _job(jid)
    scan = next((s for s in job.scans if s.id == sid), None)
    if not scan:
        raise HTTPException(404, "scan inconnu")
    jpeg = to_jpeg_bytes(load_page(scan.path, scan.page_index), max_side=1400, quality=85)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
