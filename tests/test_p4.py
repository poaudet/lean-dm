"""tests/test_p4.py — UI/UX endpoints, streaming, auth, export, and campaign management."""
import base64
import io
import json
import zipfile
import pytest
import dm


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Static asset serving
# ---------------------------------------------------------------------------

def test_serve_index_html():
    with dm.app.test_client() as client:
        res = client.get("/")
        assert res.status_code == 200
        assert b"lean-dm" in res.data
        assert b"btn-send" in res.data


def test_serve_config_html():
    with dm.app.test_client() as client:
        res = client.get("/config")
        assert res.status_code == 200
        assert b"Settings" in res.data
        assert b"config-search" in res.data


def test_serve_static_css_and_js():
    with dm.app.test_client() as client:
        res_css = client.get("/web/style.css")
        assert res_css.status_code == 200
        assert b":root" in res_css.data

        res_js = client.get("/web/app.js")
        assert res_js.status_code == 200
        assert b"function" in res_js.data


# ---------------------------------------------------------------------------
# 2. State API: guards, meter, chips
# ---------------------------------------------------------------------------

def test_api_state_fields(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    with dm.app.test_client() as client:
        res = client.get("/api/state")
        assert res.status_code == 200
        d = res.get_json()
        assert "can_undo" in d
        assert "can_retry" in d
        assert "undo_blocked_reason" in d
        assert "ctx_budget_tok" in d
        assert d["can_undo"] is False  # empty history, cannot undo


# ---------------------------------------------------------------------------
# 3. Premise view/edit & atomic backup
# ---------------------------------------------------------------------------

def test_premise_save(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "premise.md").write_text("Original premise", encoding="utf-8")

    with dm.app.test_client() as client:
        res = client.post("/api/premise", json={"text": "Updated premise"})
        assert res.status_code == 200
        assert res.get_json()["ok"] is True

    assert (root / "premise.md").read_text(encoding="utf-8") == "Updated premise"
    assert (root / "premise.md.bak").read_text(encoding="utf-8") == "Original premise"


# ---------------------------------------------------------------------------
# 4. Ledger Pinning (D-12) & Delete
# ---------------------------------------------------------------------------

def test_ledger_pin_action(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    initial = [{"id": "sword", "kind": "item", "name": "Ancient Sword", "state": "gleaming", "status": "active"}]
    (root / "ledger.json").write_text(json.dumps(initial), encoding="utf-8")

    with dm.app.test_client() as client:
        # Pin the entry
        res = client.post("/api/ledger", json={"action": "pin", "id": "sword", "pinned": True})
        assert res.status_code == 200

    ledger = dm.load_ledger()
    assert ledger[0]["id"] == "sword"
    assert ledger[0]["pinned"] is True

    # Automatic ledger ops must now skip updating or deleting the pinned entry (D-12)
    ops = {
        "upsert": [{"id": "sword", "name": "Ancient Sword", "state": "broken"}],
        "close": ["sword"],
        "delete": ["sword"],
    }
    updated = dm.apply_ledger_ops(ledger, ops)
    # Entry state must remain unchanged!
    assert updated[0]["state"] == "gleaming"
    assert updated[0]["status"] == "active"


# ---------------------------------------------------------------------------
# 5. Export endpoints: transcript markdown & campaign zip
# ---------------------------------------------------------------------------

def test_export_transcript(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "premise.md").write_text("Test Premise", encoding="utf-8")
    msgs = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Welcome"}]
    (root / "history.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs), encoding="utf-8")

    with dm.app.test_client() as client:
        res = client.get("/api/export/transcript")
        assert res.status_code == 200
        text = res.data.decode("utf-8")
        assert "Test Premise" in text
        assert "Hello" in text
        assert "Welcome" in text


def test_export_campaign_zip(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "premise.md").write_text("Premise", encoding="utf-8")
    # Put a mock settings file that should be EXCLUDED
    (root / "settings.json").write_text("{}", encoding="utf-8")

    with dm.app.test_client() as client:
        res = client.get("/api/export/campaign.zip")
        assert res.status_code == 200
        buf = io.BytesIO(res.data)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            assert "premise.md" in names
            assert "settings.json" not in names  # secrets excluded!


# ---------------------------------------------------------------------------
# 6. Optional Auth: HTTP Basic with web_password
# ---------------------------------------------------------------------------

def test_auth_challenge_when_password_set(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "web_password": "my-secret-pass"})

    with dm.app.test_client() as client:
        # Unauthenticated request -> 401
        res = client.get("/")
        assert res.status_code == 401

        # Bad password -> 401
        bad_creds = base64.b64encode(b":wrong").decode("ascii")
        res2 = client.get("/", headers={"Authorization": f"Basic {bad_creds}"})
        assert res2.status_code == 401

        # Correct password -> 200
        good_creds = base64.b64encode(b":my-secret-pass").decode("ascii")
        res3 = client.get("/", headers={"Authorization": f"Basic {good_creds}"})
        assert res3.status_code == 200


def test_auth_warning_banner_when_host_remote(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "host": "0.0.0.0",
        "web_password": "",
    })

    with dm.app.test_client() as client:
        res = client.get("/api/state")
        d = res.get_json()
        assert d.get("auth_warning") is not None
        assert "0.0.0.0" in d["auth_warning"]


# ---------------------------------------------------------------------------
# 7. Campaign management API
# ---------------------------------------------------------------------------

def test_campaigns_api_list_and_create(tmp_path):
    with dm.app.test_client() as client:
        res = client.get("/api/campaigns")
        assert res.status_code == 200
        d = res.get_json()
        assert "campaigns" in d

        # Create new campaign
        res_create = client.post("/api/campaigns", json={"action": "create", "name": "new_quest"})
        assert res_create.status_code == 200
        assert res_create.get_json()["ok"] is True

        history = dm.CAMPAIGNS_DIR / "new_quest" / "history.jsonl"
        history.write_text("existing history", encoding="utf-8")
        res_duplicate = client.post("/api/campaigns", json={"action": "create", "name": "new_quest"})
        assert res_duplicate.status_code == 409
        assert res_duplicate.get_json()["error"] == "campaign exists"
        assert history.read_text(encoding="utf-8") == "existing history"

        for name in (".", ".."):
            res_invalid = client.post("/api/campaigns", json={"action": "create", "name": name})
            assert res_invalid.status_code == 400
            assert res_invalid.get_json()["error"] == "invalid name"


def test_api_restart(monkeypatch):
    calls = []

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            assert daemon is True

        def start(self):
            calls.append("started")
            self.target()

    monkeypatch.setattr(dm.threading, "Thread", FakeThread)
    monkeypatch.setattr(dm.time, "sleep", lambda delay: calls.append(("sleep", delay)))
    monkeypatch.setenv("WERKZEUG_SERVER_FD", "42")
    monkeypatch.setattr(dm.os, "set_inheritable", lambda fd, value: calls.append(("inheritable", fd, value)))
    monkeypatch.setattr(dm.os, "execv", lambda exe, args: calls.append(("execv", exe, args)))

    with dm.app.test_client() as client:
        res = client.post("/api/restart")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True}
    assert calls == ["started", ("sleep", 0.5),
                     ("inheritable", 42, False),
                     ("execv", dm.sys.executable, [dm.sys.executable] + dm.sys.argv)]


# ---------------------------------------------------------------------------
# 8. Streaming endpoint (/api/stream)
# ---------------------------------------------------------------------------

def test_api_stream_flow(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test",
        "model": "test-model",
        "analyst_enabled": False,
        "arc_enabled": False,
        "ledger_enabled": False,
    })

    # Mock _raw_post streaming response
    class FakeStreamResponse:
        status_code = 200
        def iter_lines(self):
            chunks = ["Hello ", "brave ", "adventurer."]
            for c in chunks:
                data = json.dumps({"choices": [{"delta": {"content": c}}]})
                yield f"data: {data}".encode("utf-8")
            yield b"data: [DONE]"
        def close(self):
            pass

    monkeypatch.setattr(dm, "_raw_post", lambda payload, stream=False: FakeStreamResponse())

    with dm.app.test_client() as client:
        res = client.post("/api/stream", json={"text": "I enter the dungeon."})
        assert res.status_code == 200
        lines = [line.decode("utf-8") for line in res.response if line.strip()]
        content = "".join(lines)
        assert "adventurer" in content

    # History must contain the complete exchange committed together
    history = dm.load_history()
    assert len(history) == 2
    assert history[0]["content"] == "I enter the dungeon."
    assert history[1]["content"] == "Hello brave adventurer."
