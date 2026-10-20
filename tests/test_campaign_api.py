"""Campagnes de bout en bout (import, contrôle des pièces avec extracteur factice, fiche, exports, suppression)."""
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.campaign_api as capi
from app import server
from app.campaigns import Store
from app.config import Settings
from app.schemas import Extraction
from tests.test_contracts import MINI


class Fake:
    cache_id = "fake-v1"

    def extract(self, img):
        return Extraction(lisible=True, type_piece="CNI", face="RECTO", nom="KOFFI", prenoms="JEAN", numero_piece="CI001",
                          confiance={"nom": .95, "prenoms": .95, "numero_piece": .95})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(capi, "_store", Store(tmp_path))
    monkeypatch.setattr(capi, "get_settings", lambda: Settings(workers=2, cache_dir=tmp_path / "cache", jobs_dir=tmp_path / "j"))
    monkeypatch.setattr(capi, "get_extractor", lambda s, b=None: Fake())
    return TestClient(server.app)


def _jpg():
    b = io.BytesIO()
    Image.new("RGB", (200, 120), "white").save(b, "JPEG")
    return b.getvalue()


def test_campagne_complete(client):
    cid = client.post("/api/campaigns", data={"title": "Test"}).json()["id"]
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("dossier/scan1.jpg", _jpg())
        z.writestr("../evil.jpg", _jpg())
    r = client.post(f"/api/campaigns/{cid}/import", files=[("files", ("src.csv", MINI.encode())), ("files", ("s.zip", zb.getvalue()))])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["imported"] == 2 and j["scans"] == 2 and "scan1.jpg" in j["campaign"]["scanNames"]
    assert not (client.app.state.__dict__.get("x"))      # pas d'écriture hors dossier
    # contrôle des pièces : scan1.jpg est référencé par Jean Koffi (CT-1) -> validé
    assert client.post(f"/api/campaigns/{cid}/identity/run", data={"backend": "tesseract"}).status_code == 200
    for _ in range(100):
        s = client.get(f"/api/campaigns/{cid}/identity/status").json()
        if s["status"] != "running":
            break
        time.sleep(0.1)
    assert s["status"] == "done", s
    camp = client.get(f"/api/campaigns/{cid}").json()
    c1 = next(c for c in camp["contracts"] if c["number"] == "CT-1")
    auto = c1["actors"][0]["automatic"]
    assert auto["status"] == "validé" and auto["pieces"] == ["scan1.jpg"]
    # édition d'une fiche : un numéro de pièce modifié rend le contrôle obsolète puis non conforme après réévaluation
    c1["actors"][0]["id"] = "CI999"
    ev = client.post(f"/api/campaigns/{cid}/evaluate", json=c1).json()
    assert ev["actors"][0]["automatic"]["idStatus"] == "Numero de piece incorrect"
    assert any("Numero de piece incorrect" in o["text"] for o in ev["observations"])
    # enregistrement + historique
    sv = client.put(f"/api/campaigns/{cid}", json={"title": "Renommée", "contracts": camp["contracts"]}).json()
    assert sv["updated"]
    assert client.get("/api/campaigns").json()[0]["title"] == "Renommée"
    # scan + exports
    assert client.get(f"/api/campaigns/{cid}/scans/scan1.jpg").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/campaigns/{cid}/scans/..%2F..%2Fcampaign.json").status_code in (404, 400)
    rem = client.get(f"/api/campaigns/{cid}/export/remarks")
    assert rem.status_code == 200 and rem.content[:2] == b"PK" and rem.headers["x-contrats-exportes"] == "2"
    full = client.get(f"/api/campaigns/{cid}/export/full")
    names = zipfile.ZipFile(io.BytesIO(full.content)).namelist()
    assert "Global/Rque_contrat_Global.xlsx" in names and "Global/Controle_pieces.xlsx" in names
    assert any(n.startswith("PIECE SCANNEE/CT-1/") and n.endswith(".jpg") for n in names)
    assert "RAS/Rque_contrat_src.xlsx" in names and "REAMRQUE/Rque_contrat_src.xlsx" in names
    # rattacher une pièce à un acteur
    att = client.post(f"/api/campaigns/{cid}/attach", data={"contract": "src.csv::CT-2", "actor": "0"},
                      files={"file": ("cni.jpg", _jpg(), "image/jpeg")})
    assert att.status_code == 200 and att.json()["name"].endswith("cni.jpg")
    # suppression
    assert client.delete(f"/api/campaigns/{cid}").json()["ok"]
    assert client.get(f"/api/campaigns/{cid}").status_code == 404
    assert client.get("/api/campaigns/../etc").status_code in (404, 400, 422)


def test_id_invalide(client):
    assert client.get("/api/campaigns/..%2Fx").status_code in (400, 404)
    assert client.put("/api/campaigns/zzz", json={"contracts": []}).status_code == 404
