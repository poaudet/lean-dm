import arc
import dm

ARC = {
    "version": 1,
    "revision": 0,
    "central_question": "Q?",
    "act": 1,
    "clock": {"minutes": 0},
    "acts": [{"n": 1, "status": "active", "goal": "g", "beats": [], "outcomes": []}],
    "pressures": [],
}


class FakeStream:
    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"content":"A reply."}}]}'
        yield b"data: [DONE]"

    def close(self):
        pass


def test_stream_turn_runs_analyst_arc_record_and_commits(monkeypatch):
    dm.ROOT.mkdir(parents=True, exist_ok=True)
    dm.HISTORY.write_text("", encoding="utf-8")
    arc.save_arc(dm.ROOT, ARC)
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test",
        "model": "narrator",
        "analyst_model": "analyst",
        "scribe_model": "scribe",
        "analyst_enabled": True,
        "arc_enabled": True,
        "ledger_enabled": False,
    })

    def chat(messages, model=None, **kwargs):
        if model == "analyst":
            return '{"intent":"act","plot_moves":[]}'
        if model == "scribe":
            return '{"elapsed_minutes":5,"beats_done":[],"pressures":[],"act_complete":false}'
        raise AssertionError(f"unexpected model: {model}")

    monkeypatch.setattr(dm, "chat_complete", chat)
    monkeypatch.setattr(dm, "_raw_post", lambda *args, **kwargs: FakeStream())

    with dm.app.test_client() as client:
        response = client.post("/api/stream", json={"text": "I investigate."})
        body = response.get_data(as_text=True)

    assert '"stage": "analyzing"' in body
    assert '"stage": "recording"' in body
    assert dm.load_history()[-1]["content"] == "A reply."
    assert arc.load_records(dm.ROOT)[0]["elapsed_minutes"] == 5


def test_turn_and_retry_return_conflict_when_lock_is_busy():
    dm.TURN_LOCK.acquire()
    try:
        with dm.app.test_client() as client:
            assert client.post("/api/turn", json={"text": "wait"}).status_code == 409
            assert client.post("/api/retry").status_code == 409
            assert client.post("/api/undo").status_code == 409
    finally:
        dm.TURN_LOCK.release()


def test_act_transition_summarizes_play_and_records(tmp_path):
    arc_state = {
        **ARC,
        "acts": [
            {"n": 1, "status": "active", "goal": "Save the witness",
             "beats": [{"id": "a1b1", "text": "Planned beat text", "status": "done"}],
             "outcomes": []},
            {"n": 2, "status": "planned", "goal": "Expose the conspiracy",
             "beats": [{"id": "a2b1", "text": "Next", "status": "pending"}], "outcomes": []},
        ],
    }
    arc.save_arc(tmp_path, arc_state)
    arc.append_record(tmp_path, {
        "msg": 3,
        "elapsed_minutes": 20,
        "beats_done": ["a1b1"],
        "pressures": [{"id": "p1", "effect": "countered"}],
        "act_complete": True,
    })
    history = [
        {"role": "user", "content": "I find the witness."},
        {"role": "assistant", "content": "The witness is in the cellar."},
        {"role": "user", "content": "I lead her to safety."},
        {"role": "assistant", "content": "She escapes the raid."},
    ]
    prompts = []

    def scribe(messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return '["The witness escaped the raid."]'

    arc.transition_act(tmp_path, 1, scribe, msg_idx=3, history=history)

    assert "I lead her to safety." in prompts[0]
    assert '"elapsed_minutes": 20' in prompts[0]
    assert arc.load_arc(tmp_path)["acts"][0]["outcomes"] == ["The witness escaped the raid."]
    assert "Planned beat text" not in arc.load_arc(tmp_path)["acts"][0]["outcomes"]
