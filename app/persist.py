"""Persistance sur un stockage objet compatible S3 (Supabase Storage, Cloudflare R2, Backblaze B2…).

Sur l'hébergement gratuit le disque est effacé à chaque redémarrage. Quand S3_* est configuré, tout DATA_DIR est
recopié vers le bucket en tâche de fond et restauré au démarrage ; sans configuration, rien ne change (disque seul).
- démarrage : restauration immédiate des petits fichiers (campagnes, résultats, caches, base des pièces validées) ;
- images : restaurées à la demande (ensure) au premier accès d'un dossier de scans ;
- écriture : un thread envoie toutes les 15 s les fichiers nouveaux ou modifiés.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("presfor.persist")

LAZY_PARTS = ("/scans/", "/Images/", "/pieces/")      # restaurés à la demande
SKIP_PARTS = ("/_tmp/", "/exports/", "/jobs/")
_state = {"client": None, "root": None, "restored": set(), "synced": {}, "thread": None}
_lock = threading.RLock()


def enabled() -> bool:
    return bool(os.getenv("S3_BUCKET") and os.getenv("S3_ACCESS_KEY") and os.getenv("S3_SECRET_KEY"))


def _client():
    if _state["client"] is None:
        import boto3
        from botocore.config import Config

        _state["client"] = boto3.client(
            "s3", endpoint_url=os.getenv("S3_ENDPOINT") or None, region_name=os.getenv("S3_REGION", "auto"),
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                          connect_timeout=8, read_timeout=40,
                          s3={"addressing_style": "path"}))
    return _state["client"]


def _bucket() -> str:
    return os.environ["S3_BUCKET"]


def _prefix() -> str:
    return os.getenv("S3_PREFIX", "presfor/").lstrip("/")


def _key(root: Path, p: Path) -> str:
    return _prefix() + p.relative_to(root).as_posix()


def _skip(rel: str) -> bool:
    r = "/" + rel
    return any(s in r for s in SKIP_PARTS) or rel.endswith((".tmp", ".zip"))


def _list(prefix: str):
    pager = _client().get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=_bucket(), Prefix=prefix):
        yield from page.get("Contents", [])


def _download(root: Path, obj: dict) -> None:
    rel = obj["Key"][len(_prefix()):]
    dest = root / rel
    if dest.exists() and dest.stat().st_size == obj["Size"]:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    _client().download_file(_bucket(), obj["Key"], str(tmp))
    os.replace(tmp, dest)
    with _lock:
        _state["synced"][rel] = dest.stat().st_mtime        # déjà dans le bucket : ne pas le renvoyer


def restore_small(root: Path) -> int:
    """Au démarrage : tout sauf les images."""
    n = 0
    for obj in _list(_prefix()):
        rel = obj["Key"][len(_prefix()):]
        if not rel or _skip(rel) or any(s in "/" + rel for s in LAZY_PARTS):
            continue
        _download(root, obj)
        n += 1
    return n


def ensure(directory: Path) -> None:
    """Restaure (une fois par processus) les images d'un dossier absent du disque."""
    if not enabled() or _state["root"] is None:
        return
    root = _state["root"]
    try:
        rel = directory.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return
    with _lock:
        if rel in _state["restored"]:
            return
        _state["restored"].add(rel)
    try:
        for obj in _list(_prefix() + rel + "/"):
            _download(root, obj)
    except Exception:  # noqa: BLE001
        log.exception("restauration de %s impossible", rel)
        with _lock:
            _state["restored"].discard(rel)


def push_all(root: Path) -> int:
    n = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        rel = p.relative_to(root).as_posix()
        if _skip(rel):
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if _state["synced"].get(rel) == mt:
            continue
        try:
            _client().upload_file(str(p), _bucket(), _key(root, p))
            _state["synced"][rel] = mt
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("envoi impossible : %s", rel)
    return n


def delete_prefix(directory: Path) -> None:
    """Suppression d'une campagne / session : on retire aussi les fichiers du bucket."""
    if not enabled() or _state["root"] is None:
        return
    try:
        rel = directory.resolve().relative_to(_state["root"].resolve()).as_posix()
        keys = [{"Key": o["Key"]} for o in _list(_prefix() + rel + "/")]
        for i in range(0, len(keys), 900):
            _client().delete_objects(Bucket=_bucket(), Delete={"Objects": keys[i:i + 900]})
        with _lock:
            _state["synced"] = {k: v for k, v in _state["synced"].items() if not k.startswith(rel + "/")}
            _state["restored"].discard(rel)
    except Exception:  # noqa: BLE001
        log.exception("suppression distante impossible : %s", directory)


def _loop(root: Path, every: int) -> None:
    while True:
        time.sleep(every)
        try:
            push_all(root)
        except Exception:  # noqa: BLE001
            log.exception("synchronisation en erreur")


def start(root: Path, every: int = 15) -> bool:
    """À appeler au démarrage du serveur (racine = dossier qui contient DATA_DIR et CACHE_DIR)."""
    if not enabled():
        return False
    _state["root"] = root
    root.mkdir(parents=True, exist_ok=True)
    try:
        n = restore_small(root)
        log.info("persistance S3 : %d fichiers restaurés", n)
    except Exception:  # noqa: BLE001
        log.exception("restauration impossible (le disque local sert de repli)")
    if _state["thread"] is None:
        t = threading.Thread(target=_loop, args=(root, every), daemon=True, name="persist")
        t.start()
        _state["thread"] = t
    return True


def flush() -> int:
    return push_all(_state["root"]) if enabled() and _state["root"] else 0


def status() -> dict:
    return {"enabled": enabled(), "bucket": os.getenv("S3_BUCKET", ""), "fichiers_synchronises": len(_state["synced"])}
