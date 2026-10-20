"""Campagnes : stockage sur disque (JSON + scans). DATA_DIR doit être un volume persistant en production."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
from . import persist
from .images import SUPPORTED_EXT

_ID = re.compile(r"^[a-zA-Z0-9-]{1,80}$")


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", ROOT / "data"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_id(cid: str | None) -> bool:
    return bool(cid and _ID.match(cid))


def safe_name(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", base).strip(". ")
    return base or "fichier"


class Store:
    def __init__(self, root: Path | None = None):
        self.root = (root or data_dir()) / "campaigns"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # -- chemins
    def dir(self, cid: str) -> Path:
        if not valid_id(cid):
            raise ValueError("identifiant de campagne invalide")
        return self.root / cid

    def scans_dir(self, cid: str) -> Path:
        d = self.dir(cid) / "scans"
        persist.ensure(d)               # images restaurées depuis le stockage externe au premier accès
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exists(self, cid: str) -> bool:
        return valid_id(cid) and (self.dir(cid) / "campaign.json").is_file()

    # -- CRUD
    def create(self, title: str = "Nouvelle campagne") -> dict:
        cid = str(uuid.uuid4())
        c = {"id": cid, "title": title[:160] or "Nouvelle campagne", "contracts": [], "inputs": {}, "aliases": {},
             "updated": "", "loadedAt": None}
        self.dir(cid).mkdir(parents=True)
        self.save(c, touch=False)
        return c

    def load(self, cid: str) -> dict | None:
        p = self.dir(cid) / "campaign.json"
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, c: dict, *, touch: bool = True, imported: bool = False) -> dict:
        with self.lock:
            d = self.dir(c["id"])
            d.mkdir(parents=True, exist_ok=True)
            if touch:
                c["updated"] = now()
            if imported:
                c["loadedAt"] = c["updated"] or now()
            tmp = d / "campaign.json.tmp"
            tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            tmp.replace(d / "campaign.json")
        return c

    def delete(self, cid: str) -> bool:
        with self.lock:
            d = self.dir(cid)
            if not d.exists():
                return False
            shutil.rmtree(d, ignore_errors=True)
            persist.delete_prefix(d)
            return True

    def list(self) -> list[dict]:
        out = []
        for d in self.root.iterdir():
            p = d / "campaign.json"
            if not p.is_file():
                continue
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            out.append({"id": c["id"], "title": c.get("title", ""), "updated": c.get("updated", ""),
                        "loadedAt": c.get("loadedAt"), "contracts": len(c.get("contracts", []))})
        out.sort(key=lambda x: x.get("loadedAt") or x.get("updated") or "", reverse=True)
        return out

    # -- scans
    def scan_names(self, cid: str) -> list[str]:
        d = self.dir(cid) / "scans"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXT)

    def add_scan(self, cid: str, name: str, data: bytes) -> str:
        name = safe_name(name)
        if Path(name).suffix.lower() not in SUPPORTED_EXT:
            raise ValueError("format non pris en charge")
        d = self.scans_dir(cid)
        target = d / name
        n = 1
        while target.exists():
            if target.read_bytes() == data:
                return target.name
            target = d / f"{Path(name).stem}__{n}{Path(name).suffix}"
            n += 1
        target.write_bytes(data)
        return target.name

    # -- extractions (ocr.json)
    def load_ocr(self, cid: str) -> dict:
        p = self.dir(cid) / "ocr.json"
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def save_ocr(self, cid: str, ocr: dict) -> None:
        with self.lock:
            (self.dir(cid) / "ocr.json").write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")
