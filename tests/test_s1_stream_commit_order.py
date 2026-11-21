"""Sprint 1 / 06 - the UI hides Stop at the 'recording' stage; that is only safe if the
exchange is already on disk when the stage event is emitted."""
import json

import dm


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


PLAN = json.dumps({
    "version": 1, "central_question": "Q?",
    "acts": [{"n": 1, "status": "active", "goal": "g", "beats": [], "outcomes": []}],
    "pressures": [],
})


def test_exchange_committed_before_recording_stage(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test",
        "model": "test-model",
        "scribe_model": "test-scribe",
        "arc_enabled": True,
        "analyst_enabled": False,
        "ledger_enabled": False,
    })

    class FakeStream:
        status_code = 200

        def iter_lines(self):
            for c in ("Hello ", "there."):
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": c}}]})).encode("utf-8")
            yield b"data: [DONE]"

        def close(self):
            pass

    monkeypatch.setattr(dm, "_raw_post", lambda payload, stream=False: FakeStream())

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        sys_msg = messages[0]["content"]
        if "master story planner" in sys_msg:
            return PLAN
        if "continuity recorder" in sys_msg:
            return json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False})
        return "unused"

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    seen_recording = False
    committed_at_recording = None
    with dm.app.test_client() as client:
        res = client.post("/api/stream", json={"text": "I enter."})
        for raw in res.response:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if '"recording"' in text:
                seen_recording = True
                committed_at_recording = len(dm.load_history())

    assert seen_recording
    assert committed_at_recording == 2
