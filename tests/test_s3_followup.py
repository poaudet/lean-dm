import base64
import io
import zipfile

import dm


def test_exposed_web_bind_requires_password(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "host": "0.0.0.0", "web_password": ""})
    try:
        dm.validate_web_security()
    except RuntimeError as exc:
        assert "web_password" in str(exc)
    else:
        raise AssertionError("exposed bind without password was allowed")

def test_health_endpoint_bypasses_basic_auth(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "web_password": "secret"})
    with dm.app.test_client() as client:
        assert client.get("/healthz").status_code == 200


def test_failed_basic_auth_is_throttled_before_posts_or_gets(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "web_password": "secret"})
    monkeypatch.setattr(dm, "AUTH_FAILURES", {})
    now = [100.0]
    monkeypatch.setattr(dm, "monotonic", lambda: now[0])
    bad = {"Authorization": "Basic " + base64.b64encode(b":wrong").decode("ascii")}
    good = {"Authorization": "Basic " + base64.b64encode(b":secret").decode("ascii")}

    with dm.app.test_client() as client:
        assert client.get("/").status_code == 401
        for _ in range(10):
            assert client.get("/", headers=bad).status_code == 401
        blocked = client.post("/api/turn", headers=good, json={"text": "move"})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0
        assert client.get("/healthz").status_code == 200
        assert client.get("/", headers=good, environ_overrides={"REMOTE_ADDR": "127.0.0.2"}).status_code == 200
        now[0] += 60
        assert client.get("/", headers=good).status_code == 200


def test_loopback_web_bind_does_not_require_password(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "host": "127.0.0.1", "web_password": ""})
    dm.validate_web_security()


def test_post_rejects_mismatched_origin():
    with dm.app.test_request_context(
        "/", method="POST",
        headers={"Host": "localhost", "Origin": "https://attacker.example"},
    ):
        assert not dm._same_origin()


def test_stream_emits_ooc_answer_inline(monkeypatch):
    dm.ROOT.mkdir(parents=True, exist_ok=True)
    dm.HISTORY.write_text("", encoding="utf-8")
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test",
        "model": "test-model",
        "notes_routing": True,
    })
    monkeypatch.setattr(dm.analyst, "handle_ooc_note", lambda *args, **kwargs: {"a": "The answer."})

    with dm.app.test_client() as client:
        response = client.post("/api/stream", json={"text": "// where are we?"})
        body = response.get_data(as_text=True)

    assert body.count('"chunk": "The answer."') == 1
    assert "data: [DONE]" in body


def test_campaign_zip_excludes_arc_files_unless_requested(monkeypatch):
    dm.ROOT.mkdir(parents=True, exist_ok=True)
    (dm.ROOT / "history.jsonl").write_text("", encoding="utf-8")
    (dm.ROOT / "arc.json").write_text('{"central_question":"secret"}', encoding="utf-8")
    (dm.ROOT / "records.jsonl").write_text('{"private":"secret"}', encoding="utf-8")
    (dm.ROOT / "arc_snap").mkdir()
    (dm.ROOT / "arc_snap" / "1.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "arc_debug": True})
    with dm.app.test_client() as client:
        default_zip = zipfile.ZipFile(io.BytesIO(client.get("/api/export/campaign.zip").data))
        full_zip = zipfile.ZipFile(io.BytesIO(client.get("/api/export/campaign.zip?include_arc=1").data))

    assert "arc.json" not in default_zip.namelist()
    assert "records.jsonl" not in default_zip.namelist()
    assert "arc_snap/1.json" not in default_zip.namelist()
    assert {"arc.json", "records.jsonl", "arc_snap/1.json"} <= set(full_zip.namelist())
