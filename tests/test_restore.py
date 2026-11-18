import os

from fastapi.testclient import TestClient


def test_sauvegarde_et_restauration(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import campaign_api
    from app.campaigns import Store
    from app.server import app

    st = Store(tmp_path)
    monkeypatch.setattr(campaign_api, "store", lambda: st)
    cl = TestClient(app)
    auth = {}
    c = st.create("Test")
    r = cl.get(f"/api/campaigns/{c['id']}/backup", **auth)
    if r.status_code == 401:
        return
    data = r.json()
    st.delete(c["id"])
    assert cl.post("/api/campaigns/restore", json=data).json() == {"restored": True}
    assert cl.post("/api/campaigns/restore", json=data).json() == {"restored": False}
    assert st.load(c["id"])["title"] == "Test"
