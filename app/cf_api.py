"""API « Contrôle pièce CF »."""
from __future__ import annotations

import io
import json
import logging
import shutil
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook
from starlette.background import BackgroundTask

from . import cf
from .campaigns import data_dir, now, safe_name, valid_id
from .config import get_settings
from .csv_loader import decode
from .extractors import get_extractor

log = logging.getLogger("presfor.cf")
router = APIRouter(prefix="/api/cf")
JOBS: dict[str, dict] = {}


def root() -> Path:
    d = data_dir() / "cf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sdir(sid: str) -> Path:
    if not valid_id(sid):
        raise HTTPException(400, "identifiant invalide")
    d = root() / sid
    if not d.is_dir():
        raise HTTPException(404, "session introuvable")
    return d


def _results(d: Path) -> dict | None:
    p = d / "results.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


@router.get("")
def list_sessions() -> list[dict]:
    out = []
    for d in root().iterdir():
        meta = d / "meta.json"
        if meta.is_file():
            m = json.loads(meta.read_text(encoding="utf-8"))
            r = _results(d)
            out.append({**m, "summary": r["summary"] if r else None})
    return sorted(out, key=lambda x: x.get("created", ""), reverse=True)


@router.post("")
def create(title: str = Form("Export CF")) -> dict:
    sid = str(uuid.uuid4())
    d = root() / sid
    (d / "Images").mkdir(parents=True)
    meta = {"id": sid, "title": title[:160], "created": now()}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


@router.post("/{sid}/files")
def upload(sid: str, files: list[UploadFile] = File(...)) -> dict:
    d = sdir(sid)
    n_img = n_csv = 0
    for up in files:
        name = safe_name(up.filename or "")
        ext = Path(name).suffix.lower()
        if ext == ".csv":
            (d / name).write_bytes(up.file.read())
            n_csv += 1
        elif ext in cf.IMG_EXT:
            (d / "Images" / name).write_bytes(up.file.read())
            n_img += 1
    return {"csv": n_csv, "images": n_img, "total_images": len(cf.list_images(d / "Images"))}


def _worker(sid: str, backend: str) -> None:
    job = JOBS[sid]
    d = root() / sid
    try:
        s = get_settings()
        ext = get_extractor(s, backend)
        data = cf.run_session(d, s, ext, lambda done, total, label: job.update(done=done, total=total, label=label))
        job.update(label="Enregistrement des pièces validées")
        data["stored"] = cf.store_valid(data["results"], d, sid)
        data["summary"]["STOCKEES"] = data["stored"]
        (d / "results.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("contrôle CF en erreur")
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"


@router.post("/{sid}/run")
def run(sid: str, backend: str = Form("auto")) -> dict:
    d = sdir(sid)
    if (j := JOBS.get(sid)) and j["status"] == "running":
        return j
    if not list(d.glob("*.csv")):
        raise HTTPException(400, "CSV de l'export manquant")
    if not cf.list_images(d / "Images"):
        raise HTTPException(400, "aucune image dans l'export")
    try:
        get_extractor(get_settings(), backend)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    JOBS[sid] = {"status": "running", "done": 0, "total": 0, "label": "Démarrage", "error": None}
    threading.Thread(target=_worker, args=(sid, backend), daemon=True).start()
    return JOBS[sid]


@router.get("/{sid}/status")
def status(sid: str) -> dict:
    sdir(sid)
    return JOBS.get(sid) or {"status": "idle"}


@router.get("/{sid}/results")
def results(sid: str) -> dict:
    r = _results(sdir(sid))
    if not r:
        raise HTTPException(404, "pas encore analysée")
    return r


@router.get("/{sid}/export/invalides.xlsx")
def export_invalid(sid: str) -> Response:
    r = _results(sdir(sid))
    if not r:
        raise HTTPException(409, "analyse non terminée")
    return Response(cf.invalid_workbook(r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Controle_piece_CF_invalidees.xlsx"'})


@router.get("/{sid}/scan/{name}")
def scan(sid: str, name: str) -> FileResponse:
    p = sdir(sid) / "Images" / safe_name(name)
    if not p.is_file():
        raise HTTPException(404, "scan introuvable")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/{sid}")
def delete(sid: str) -> dict:
    d = sdir(sid)
    JOBS.pop(sid, None)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---- base des pièces validées
@router.get("/valid/list")
def valid_list() -> list[dict]:
    return cf.valid_rows()


@router.get("/valid/file/{path:path}")
def valid_file(path: str) -> FileResponse:
    p = (cf.valid_root() / path).resolve()
    if cf.valid_root().resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "fichier introuvable")
    return FileResponse(p)


@router.get("/valid/export.zip")
def valid_zip() -> FileResponse:
    rows = cf.valid_rows()
    tmp = cf.valid_root() / f"_export_{uuid.uuid4().hex}.zip"
    wb = Workbook()
    ws = wb.active
    ws.title = "Pièces validées"
    ws.append(["Numéro de demande", "Sous-prefecture", "Village", "Nom", "Prénoms", "Type de pièce", "N° pièce", "Recto", "Verso", "Validée le"])
    for r in rows:
        ws.append([r["demande"], r["sous_prefecture"], r["village"], r["nom"], r["prenoms"], r["type_piece"],
                   r["numero_piece"], r["recto"], r["verso"], r["valide_le"]])
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        buf = io.BytesIO()
        wb.save(buf)
        z.writestr("Base_pieces_validees.xlsx", buf.getvalue())
        for r in rows:
            for k in ("recto", "verso"):
                if r[k] and (cf.valid_root() / r[k]).is_file():
                    z.write(cf.valid_root() / r[k], "pieces/" + Path(r[k]).relative_to("pieces").as_posix())
    return FileResponse(tmp, media_type="application/zip", filename="Base_pieces_CF_validees.zip",
                        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))
