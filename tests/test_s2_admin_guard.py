"""Sprint 2 / 09 - admin actions are locked when exposed without a password."""
import base64

import dm


def _cfg(monkeypatch, **kw):
    monkeypatch.delenv("DM_ALLOW_OPEN_CONFIG", raising=False)
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, **kw})


def test_exposed_without_password_blocks_global_config_and_restart(monkeypatch):
    _cfg(monkeypatch, host="0.0.0.0", web_password="")
    before = dm.cfg.base_url
    with dm.app.test_client() as c:
        r = c.post("/api/config", json={"changes": {"base_url": "https://evil.example/v1"}})
        assert r.status_code == 403
        assert c.post("/api/restart").status_code == 403
        assert c.post("/api/config", json={"reset": True}).status_code == 403
    assert dm.cfg.base_url == before


def test_campaign_scope_still_allowed_when_exposed(monkeypatch):
    _cfg(monkeypatch, host="0.0.0.0", web_password="")
    monkeypatch.setattr(dm.cfg, "_campaign_vals", {})
    with dm.app.test_client() as c:
        r = c.post("/api/config?scope=campaign", json={"changes": {"difficulty": "hard"}})
        assert r.status_code == 200


def test_password_or_loopback_unlocks(monkeypatch):
    _cfg(monkeypatch, host="127.0.0.1", web_password="")
    assert dm.config_locked() is False
    _cfg(monkeypatch, host="0.0.0.0", web_password="pw")
    assert dm.config_locked() is False
    creds = base64.b64encode(b":pw").decode("ascii")
    with dm.app.test_client() as c:
        r = c.post("/api/config", json={"changes": {"nonexistent_key": 1}},
                   headers={"Authorization": f"Basic {creds}"})
        assert r.status_code == 400


def test_escape_hatch(monkeypatch):
    _cfg(monkeypatch, host="0.0.0.0", web_password="")
    monkeypatch.setenv("DM_ALLOW_OPEN_CONFIG", "1")
    assert dm.config_locked() is False


def test_cross_origin_post_refused(monkeypatch):
    _cfg(monkeypatch, host="127.0.0.1", web_password="")
    with dm.app.test_client() as c:
        assert c.post("/api/undo", headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.post("/api/undo", headers={"Origin": "http://localhost"}).status_code != 403


def test_non_ascii_password_attempt_is_401_not_500(monkeypatch):
    _cfg(monkeypatch, web_password="my-secret-pass")
    bad = base64.b64encode("x:pässword".encode("utf-8")).decode("ascii")
    with dm.app.test_client() as c:
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
