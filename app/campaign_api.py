"""API des campagnes de contrôle des contrats (port de la plateforme v1)."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from .campaign_export import export_full, export_remarks
from .campaigns import Store, safe_name, valid_id
from .config import get_settings
from .contracts import evaluate, from_snapshot, parse_csv
from .csv_loader import decode
from .extractors import get_extractor
from .identity_check import apply_identity, inventory_aliases
from .images import SUPPORTED_EXT, load_page, to_jpeg_bytes
from .pipeline import collect_scans, extract_all

log = logging.getLogger("presfor.campaigns")
router = APIRouter(prefix="/api/campaigns")

MAX_ZIP_BYTES = 1_500_000_000
MAX_ZIP_FILES = 5000
_store: Store | None = None
IDENT_JOBS: dict[str, dict] = {}


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _get(cid: str) -> dict:
    if not valid_id(cid):
        raise HTTPException(400, "identifiant invalide")
    c = store().load(cid)
    if not c:
        raise HTTPException(404, "campagne introuvable")
    return c


def _payload(c: dict) -> dict:
    ocr = store().load_ocr(c["id"])
    return {**c, "scanNames": store().scan_names(c["id"]), "ocrCount": len(ocr),
            "contracts": [evaluate(x) for x in c["contracts"]]}


def _refresh(c: dict, only_stale: bool = True) -> dict:
    s = store()
    return apply_identity(c, s.load_ocr(c["id"]), s.scans_dir(c["id"]), s.scan_names(c["id"]), get_settings(), only_stale)


# ------------------------------------------------------------------ CRUD
@router.get("")
def list_campaigns() -> list[dict]:
    return store().list()


@router.post("")
def create_campaign(title: str = Form("Nouvelle campagne")) -> dict:
    return _payload(store().create(title))


@router.get("/{cid}")
def get_campaign(cid: str) -> dict:
    return _payload(_get(cid))


@router.put("/{cid}")
def save_campaign(cid: str, body: dict) -> dict:
    c = _get(cid)
    contracts = body.get("contracts")
    if not isinstance(contracts, list) or len(contracts) > 20000:
        raise HTTPException(400, "données invalides")
    c["title"] = str(body.get("title") or c["title"])[:160]
    c["contracts"] = contracts
    c = _refresh(c, only_stale=True)
    store().save(c)
    return {"updated": c["updated"], "loadedAt": c.get("loadedAt"), "contracts": c["contracts"]}


@router.delete("/{cid}")
def delete_campaign(cid: str) -> dict:
    _get(cid)
    IDENT_JOBS.pop(cid, None)
    store().delete(cid)
    return {"ok": True}


@router.post("/{cid}/evaluate")
def evaluate_contract(cid: str, contract: dict) -> dict:
    """Réévalue UN contrat modifié (règles + contrôle de pièces) sans l'enregistrer."""
    c = _get(cid)
    tmp = {**c, "contracts": [contract]}
    return _refresh(tmp, only_stale=True)["contracts"][0]


# ------------------------------------------------------------------ import
def _unzip(zpath: Path, st: Store, cid: str, out: dict) -> None:
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise HTTPException(413, "archive trop volumineuse")
        names = [i.filename for i in infos]
        manifest = next((i for i in infos if i.filename.lower().endswith("inventaire_pieces.csv")), None)
        stored: dict[str, str] = {}
        for i in infos:
            base = safe_name(Path(i.filename).name)
            if base.startswith(".") or "__MACOSX" in i.filename:
                continue
            ext = Path(base).suffix.lower()
            if ext in SUPPORTED_EXT:
                stored[base] = st.add_scan(cid, base, z.read(i))
                out["scans"] += 1
        if manifest:
            import csv, io
            rows = list(csv.DictReader(io.StringIO(decode(z.read(manifest))), delimiter=";"))
            out["aliases"].update(inventory_aliases(rows, list(stored.values())))
        snap = next((i for i in infos if i.filename.lower().endswith(("resultat.json", "campagne.json"))), None)
        if snap:
            _read_json(z.read(snap), out)
            snap_name = snap.filename
        else:
            snap_name = None
        for i in infos:
            if i.filename == snap_name or i is manifest:
                continue
            if i.filename.lower().endswith(".csv") and not snap:
                _read_csv(Path(i.filename).name, z.read(i), out)


def _read_csv(name: str, data: bytes, out: dict) -> None:
    text = decode(data)
    out["inputs"][name] = text
    out["contracts"].extend(parse_csv(text, name))


def _read_json(data: bytes, out: dict) -> None:
    d = json.loads(decode(data))
    if isinstance(d, dict) and "Tous les contrats" in d:
        out["contracts"].extend(from_snapshot(d))
    elif isinstance(d, dict) and isinstance(d.get("contracts"), list):
        out["contracts"].extend(d["contracts"])
        out["inputs"].update(d.get("inputs") or {})


@router.post("/{cid}/import")
async def import_files(cid: str, files: list[UploadFile] = File(...)) -> dict:
    c = _get(cid)
    st = store()
    out = {"contracts": [], "inputs": {}, "aliases": {}, "scans": 0}
    tmp = st.dir(cid) / "_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        for up in files:
            name = safe_name(up.filename or "")
            ext = Path(name).suffix.lower()
            if ext == ".zip":
                zp = tmp / name
                with zp.open("wb") as f:
                    shutil.copyfileobj(up.file, f)
                try:
                    _unzip(zp, st, cid, out)
                except zipfile.BadZipFile as e:
                    raise HTTPException(400, f"ZIP illisible : {name}") from e
            elif ext == ".csv":
                _read_csv(name, await up.read(), out)
            elif ext == ".json":
                try:
                    _read_json(await up.read(), out)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(400, f"JSON illisible : {name}") from e
            elif ext in SUPPORTED_EXT:
                st.add_scan(cid, name, await up.read())
                out["scans"] += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    merged = {x["key"]: x for x in c["contracts"]}
    for x in out["contracts"]:
        merged[x["key"]] = x
    if not c["contracts"] and files and c["title"] == "Nouvelle campagne":
        c["title"] = Path(safe_name(files[0].filename or "")).stem or c["title"]
    c["contracts"] = list(merged.values())
    c["inputs"] = {**c.get("inputs", {}), **out["inputs"]}
    c["aliases"] = {**c.get("aliases", {}), **out["aliases"]}
    c = _refresh(c, only_stale=False if out["scans"] and not out["contracts"] else True)
    st.save(c, imported=True)
    return {"imported": len(out["contracts"]), "scans": out["scans"], "campaign": _payload(c)}


# ------------------------------------------------------------------ scans
@router.get("/{cid}/scans/{name:path}")
def get_scan(cid: str, name: str, page: int = 0) -> Response:
    _get(cid)
    base = safe_name(name.split("#p")[0])
    p = store().scans_dir(cid) / base
    if not p.is_file():
        raise HTTPException(404, "scan introuvable")
    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and page == 0:
        return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})
    try:
        jpeg = to_jpeg_bytes(load_page(p, page), max_side=1800, quality=88)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"lecture impossible : {e}") from e
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{cid}/extraction/{name:path}")
def get_extraction(cid: str, name: str) -> dict:
    _get(cid)
    return store().load_ocr(cid).get(name) or {}


@router.post("/{cid}/attach")
async def attach_scan(cid: str, file: UploadFile = File(...), contract: str = Form(...), actor: int = Form(...),
                      replace: str = Form("")) -> dict:
    c = _get(cid)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(400, "Choisissez une image JPG, PNG, WEBP ou un PDF.")
    ct = next((x for x in c["contracts"] if x["key"] == contract), None)
    if not ct or not (0 <= actor < len(ct["actors"])):
        raise HTTPException(404, "contrat ou acteur introuvable")
    name = store().add_scan(cid, f"{ct['number']}_{file.filename}", await file.read())
    a = ct["actors"][actor]
    pieces = [name if p == replace else p for p in a.get("pieces", [])] if replace else [*a.get("pieces", []), name]
    a["pieces"] = list(dict.fromkeys(pieces))
    a.pop("automatic", None)
    c = _refresh(c, only_stale=True)
    store().save(c)
    return {"name": name, "campaign": _payload(c)}


# ------------------------------------------------------------------ contrôle des pièces (moteur d'identité v2)
def _ident_worker(cid: str, backend: str) -> None:
    job = IDENT_JOBS[cid]
    s = get_settings()
    try:
        st = store()
        extractor = get_extractor(s, backend)
        files = [st.scans_dir(cid) / n for n in st.scan_names(cid)]
        scans = collect_scans(files)
        job["total"] = len(scans)
        extract_all(scans, extractor, s, lambda d, t: job.update(done=d, total=t))
        ocr = st.load_ocr(cid)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for sc in scans:
            if sc.extraction is not None:
                ocr[sc.filename] = {"sha": sc.sha, "at": stamp, "extraction": sc.extraction.model_dump()}
        for key in [k for k in ocr if k.partition("#p")[0] not in {p.name for p in files}]:
            ocr.pop(key)
        st.save_ocr(cid, ocr)
        with st.lock:
            c = st.load(cid)
            if c is None:
                return
            c = apply_identity(c, ocr, st.scans_dir(cid), st.scan_names(cid), s, only_stale=False)
            st.save(c)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("contrôle des pièces en erreur")
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"


@router.post("/{cid}/identity/run")
def run_identity(cid: str, backend: str = Form("auto")) -> dict:
    _get(cid)
    if (j := IDENT_JOBS.get(cid)) and j["status"] == "running":
        return j
    s = get_settings()
    backend = (backend or "auto").lower()
    try:
        get_extractor(s, backend)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    if not store().scan_names(cid):
        raise HTTPException(400, "aucun scan importé dans cette campagne")
    IDENT_JOBS[cid] = {"status": "running", "done": 0, "total": 0, "error": None, "backend": backend}
    threading.Thread(target=_ident_worker, args=(cid, backend), daemon=True).start()
    return IDENT_JOBS[cid]


@router.get("/{cid}/identity/status")
def identity_status(cid: str) -> dict:
    _get(cid)
    return IDENT_JOBS.get(cid) or {"status": "idle", "done": 0, "total": 0}


# ------------------------------------------------------------------ exports
@router.get("/{cid}/export/remarks")
def download_remarks(cid: str) -> Response:
    c = _get(cid)
    name, data, mime, n = export_remarks(c["contracts"])
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                    "X-Contrats-Exportes": str(n)})


@router.get("/{cid}/export/full")
def download_full(cid: str) -> Response:
    c = _get(cid)
    st = store()
    tmp = st.dir(cid) / f"_export_{uuid.uuid4().hex}.zip"
    stats = export_full(c, st.scans_dir(cid), st.scan_names(cid), st.load_ocr(cid), tmp)
    return FileResponse(tmp, media_type="application/zip", filename="Rque_contrat_Resultats_complets.zip",
                        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)),
                        headers={"X-Pieces-Exportees": str(stats["savedPieces"]),
                                 "X-Pieces-Introuvables": str(stats["missingPieces"])})
