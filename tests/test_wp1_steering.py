"""WP1 - steering state: single writer, rollback, injection, UI fields."""
import json

import dm
import steering


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


ARC = {
    "version": 1, "revision": 0, "central_question": "SECRET_QUESTION_8812", "act": 1,
    "clock": {"minutes": 0},
    "acts": [{"n": 1, "status": "active", "goal": "SECRET_GOAL_4411",
              "beats": [{"id": "a1b1", "text": "SECRET_BEAT_7733", "status": "pending"}],
              "outcomes": []}],
    "pressures": [{"id": "p1", "antagonist": "SECRET_ANTAGONIST_2299", "goal": "g",
                   "status": "active", "steps": [], "deadline_minutes": 900,
                   "effective_deadline_minutes": 900}],
}

RECORD = json.dumps({
    "elapsed_minutes": 10, "beats_done": [], "pressures": [], "act_complete": False,
    "objective": "Find the witness before dawn",
    "open_question": "Do you take the alley or the main road?",
    "progress": True,
})


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-narrator", "scribe_model": "test-scribe",
        "ledger_enabled": False, "analyst_enabled": False,
        "arc_enabled": False, "steering_enabled": True,
        **over,
    })


def _fake(record=RECORD, calls=None):
    def fake(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if calls is not None:
            calls.append(messages)
        if model == "test-scribe":
            return record
        return "Narration."
    return fake


def test_record_pass_writes_steering_when_arc_is_off(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    monkeypatch.setattr(dm, "chat_complete", _fake())

    dm.player_turn("I search the alley.")

    state = steering.load_steering(root)
    assert state["objective"] == "Find the witness before dawn"
    assert state["open_question"] == "Do you take the alley or the main road?"
    assert state["source"] == "record"
    assert state["turns_since_progress"] == 0
    assert not (root / "arc.json").exists()


def test_one_record_call_covers_arc_and_steering(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, json.loads(json.dumps(ARC)))
    _cfg(monkeypatch, arc_enabled=True)
    calls = []
    monkeypatch.setattr(dm, "chat_complete", _fake(calls=calls))

    dm.player_turn("I search the alley.")

    recorder = [c for c in calls if c[0]["content"].startswith("You are the continuity recorder")]
    assert len(recorder) == 1
    assert dm.arc.load_arc(root)["clock"]["minutes"] == 10
    assert steering.load_steering(root)["objective"] == "Find the witness before dawn"


def test_steering_file_never_contains_arc_content(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, json.loads(json.dumps(ARC)))
    _cfg(monkeypatch, arc_enabled=True)
    monkeypatch.setattr(dm, "chat_complete", _fake())

    dm.player_turn("I search the alley.")

    text = (root / "steering.json").read_text(encoding="utf-8")
    for sentinel in ("SECRET_QUESTION_8812", "SECRET_GOAL_4411", "SECRET_BEAT_7733",
                     "SECRET_ANTAGONIST_2299", "a1b1", "p1"):
        assert sentinel not in text


def test_failed_record_keeps_state_and_bumps_counter(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    steering.save_steering(root, {
        "objective": "Keep the witness alive", "open_question": "Where do you hide her?",
        "active_hook": None, "turns_since_progress": 2, "updated_msg": 1, "source": "record",
    })

    def boom(messages, model=None, **kw):
        if model == "test-scribe":
            raise RuntimeError("provider down")
        return "Narration."

    monkeypatch.setattr(dm, "chat_complete", boom)

    assert dm.player_turn("I wait.") == "Narration."

    state = steering.load_steering(root)
    assert state["objective"] == "Keep the witness alive"
    assert state["open_question"] == "Where do you hide her?"
    assert state["turns_since_progress"] == 3
    assert state["source"] == "fallback"


def test_undo_restores_previous_steering(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    monkeypatch.setattr(dm, "chat_complete", _fake())

    dm.player_turn("I search the alley.")
    first = steering.load_steering(root)
    assert first["objective"] == "Find the witness before dawn"

    dm.undo_turn()
    assert steering.load_steering(root) is None

    dm.player_turn("I search the alley.")
    assert steering.load_steering(root)["objective"] == first["objective"]


def test_ask_turn_leaves_open_question_untouched(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, analyst_enabled=True, analyst_model="test-analyst")
    steering.save_steering(root, {
        "objective": "Find the witness", "open_question": "Alley or main road?",
        "active_hook": None, "turns_since_progress": 1, "updated_msg": 1, "source": "record",
    })

    def fake(messages, model=None, **kw):
        if model == "test-analyst":
            return json.dumps({"intent": "ask", "assessment": "a", "outcome": "clean",
                               "plot_moves": [], "keep_true": [],
                               "narration": {"words": 100, "push": 0.5, "end": "decision"},
                               "clarify": None, "act_complete": False})
        if model == "test-scribe":
            raise AssertionError("an ask turn must not call the recorder")
        return "The barkeep is called Marn."

    monkeypatch.setattr(dm, "chat_complete", fake)

    dm.player_turn("What is the barkeep's name?")

    state = steering.load_steering(root)
    assert state["open_question"] == "Alley or main road?"
    assert state["objective"] == "Find the witness"


def test_narrator_prompt_has_steering_after_ledger_before_brief(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.LEDGER.write_text(json.dumps([
        {"id": "key", "kind": "item", "name": "Key", "state": "lost"}
    ]), encoding="utf-8")
    _cfg(monkeypatch, ledger_enabled=True)
    steering.save_steering(root, {
        "objective": "Find the witness", "open_question": "Alley or main road?",
        "active_hook": None, "turns_since_progress": 0, "updated_msg": 1, "source": "record",
    })

    content = dm.build_context([{"role": "user", "content": "Go."}],
                               {"intent": "act"})[-1]["content"]
    assert content.index("Key: lost") < content.index("CURRENT STEERING")
    assert content.index("CURRENT STEERING") < content.index("DIRECTOR'S BRIEF")
    assert "Objective: Find the witness" in content


def test_game_state_exposes_player_facing_steering(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    steering.save_steering(root, {
        "objective": "Find the witness", "open_question": "Alley or main road?",
        "active_hook": None, "turns_since_progress": 0, "updated_msg": 1, "source": "record",
    })

    state = dm.game_state()
    assert state["steering"]["objective"] == "Find the witness"
    assert state["steering"]["open_question"] == "Alley or main road?"


def test_steering_absent_when_disabled(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, steering_enabled=False)
    assert dm.game_state()["steering"] is None


def test_clamp_steering_truncates_and_keeps_previous_on_junk():
    out = steering.clamp_steering({"objective": "x" * 400, "open_question": "q", "progress": True})
    assert len(out["objective"]) == steering.MAX_TEXT
    prev = {"objective": "keep me", "open_question": "and me", "active_hook": None,
            "turns_since_progress": 4, "updated_msg": 1, "source": "record"}
    out = steering.clamp_steering({}, prev, 3)
    assert out["objective"] == "keep me"
    assert out["open_question"] == "and me"
    assert out["turns_since_progress"] == 5


def test_render_steering_is_empty_without_state():
    assert steering.render_steering(None) == ""
    assert steering.render_steering({}) == ""
