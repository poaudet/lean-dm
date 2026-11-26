"""WP5 prompt contracts: explicit inputs, schemas, limits and narrator semantics."""
import json

import analyst
import arc
import dm

BRIEF = {
    "intent": "act", "assessment": "The lock gives.", "outcome": "clean",
    "plot_moves": [], "deferred": [], "keep_true": [],
    "narration": {"words": 120, "push": 0.5, "end": "question"},
    "clarify": None, "act_complete": False,
}


def _arc():
    return {
        "version": 1, "revision": 0, "central_question": "Will the gate open?",
        "act": 1, "clock": {"minutes": 0},
        "acts": [
            {"n": 1, "status": "active", "goal": "Find the key", "beats": [
                {"id": f"a1b{i}", "text": f"private {i}", "hook": f"A clue {i}", "status": "pending"}
                for i in range(1, 4)
            ], "outcomes": []},
            {"n": 2, "status": "planned", "goal": "Open the gate", "beats": [], "outcomes": []},
            {"n": 3, "status": "planned", "goal": "Escape", "beats": [], "outcomes": []},
        ], "pressures": [],
    }


def test_analyst_prompt_contract_and_authority_order(monkeypatch, tmp_path):
    monkeypatch.setattr(analyst.cfg, "_vals", {
        **analyst.cfg._vals, "api_key": "test", "model": "test", "analyst_enabled": True,
    })
    seen = {}

    def chat(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return json.dumps(BRIEF)

    history = [
        {"role": "user", "content": "recent player action"},
        {"role": "assistant", "content": "recent narrator reply"},
    ]
    analyst.run_analyst(
        tmp_path, history, "I turn the key.", [], [], chat,
        premise="fixed premise", summary="old summary", ledger_text="[pinned] Mira",
        steering_text="Find the witness", ledger=[{"id": "mira", "name": "Mira", "state": "hidden",
                                                      "kind": "person", "status": "active", "pinned": True}],
    )
    for section in ("ROLE:", "INPUTS", "TASK:", "OUTPUT:", "RULES:", "WHEN UNSURE:", "EXAMPLES:"):
        assert section in seen["system"]
    assert "[pinned]" in seen["user"]
    assert seen["user"].index("CANON LEDGER") < seen["user"].index("PREMISE")
    assert seen["user"].index("PREMISE") < seen["user"].index("STORY SO FAR")
    assert seen["user"].index("STORY SO FAR") < seen["user"].index("RECENT PLAY")
    assert "keep_true" in seen["system"] and "alias" in seen["system"]
    assert "deferred" in seen["system"] and "offer" in seen["system"]


def test_record_prompt_declares_all_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr(arc.cfg, "_vals", {
        **arc.cfg._vals, "api_key": "test", "model": "test", "scribe_model": "test",
        "arc_enabled": True, "steering_enabled": False, "difficulty": "standard",
        "drift_turns": 4, "act_max_turns": 0,
    })
    arc.save_arc(tmp_path, _arc())
    seen = {}

    def chat(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [],
                           "act_complete": False, "central_question_resolved": False,
                           "on_arc": True, "matched_beats": [], "objective": "Find the key",
                           "open_question": "Which door?", "progress": False})

    arc.run_record_pass(tmp_path, "I search.", "You find dust.", 1, chat)
    for section in ("ROLE:", "INPUTS:", "TASK:", "OUTPUT:", "RULES:", "WHEN UNSURE:", "EXAMPLES:"):
        assert section in seen["system"]
    for field in ("elapsed_minutes", "on_arc", "matched_beats", "objective", "open_question", "active_hook", "progress"):
        assert field in seen["user"]
    assert "1-120" in seen["user"]
    assert "160 characters" in seen["user"]


def test_planner_prompt_contract_and_empty_future_acts():
    for section in ("ROLE:", "INPUTS:", "TASK:", "OUTPUT:", "RULES:", "WHEN UNSURE:", "EXAMPLES:"):
        assert section in arc.PLANNER_SYSTEM
        assert section in arc.BEAT_PLANNER_SYSTEM
    assert "Acts 2 and 3" in arc.PLANNER_SYSTEM and "beats=[]" in arc.PLANNER_SYSTEM
    assert "unique across the entire arc" in arc.PLANNER_SYSTEM
    assert "200 characters" in arc.PLANNER_SYSTEM


def test_next_act_beats_are_validated_and_ids_are_arc_unique(monkeypatch):
    monkeypatch.setattr(arc.cfg, "_vals", {
        **arc.cfg._vals, "api_key": "test", "model": "test", "planner_model": "test",
        "difficulty": "standard",
    })
    state = _arc()
    prompts = []

    def chat(messages, **kwargs):
        prompts.append(messages[1]["content"])
        return json.dumps([
            {"id": f"a2b{i}", "text": f"private {i}", "hook": f"A visible lead {i}", "status": "pending"}
            for i in range(1, 4)
        ])

    beats = arc.plan_next_act_beats(state, 2, ["the key was found"], chat)
    ids = [beat["id"] for beat in beats]
    assert len(ids) == len(set(ids)) == 3
    assert not set(ids).intersection({"a1b1", "a1b2", "a1b3"})
    assert "EXISTING BEAT IDS" in prompts[0]


def test_next_act_retries_invalid_count_once_before_accepting_valid_beats(monkeypatch):
    monkeypatch.setattr(arc.cfg, "_vals", {
        **arc.cfg._vals, "api_key": "test", "model": "test", "planner_model": "test",
        "difficulty": "standard",
    })
    calls = []

    def chat(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps([{"id": "bad", "text": "private", "hook": "Only one", "status": "pending"}])
        return json.dumps([
            {"id": f"a2b{i}", "text": f"private {i}", "hook": f"A lead {i}", "status": "pending"}
            for i in range(1, 4)
        ])

    beats = arc.plan_next_act_beats(_arc(), 2, [], chat)
    assert len(calls) == 2
    assert len(beats) == 3


def test_next_act_invalid_count_uses_single_fallback(monkeypatch):
    monkeypatch.setattr(arc.cfg, "_vals", {
        **arc.cfg._vals, "api_key": "test", "model": "test", "planner_model": "test",
        "difficulty": "standard",
    })
    beats = arc.plan_next_act_beats(_arc(), 2, [], lambda *a, **k: json.dumps([
        {"id": "a2b1", "text": "private", "hook": "A lead", "status": "pending"}
    ]))
    assert len(beats) == 1
    assert beats[0]["hook"] == "An opening the player can act on"


def test_audit_prompt_includes_alias_collisions(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(dm, "PREMISE", tmp_path / "premise.md")
    monkeypatch.setattr(dm, "AUDIT", tmp_path / "audit.md")
    monkeypatch.setattr(dm, "STATE", tmp_path / "state.json")
    dm.HISTORY.write_text(
        json.dumps({"role": "user", "content": "Mira meets the smith."}) + "\n" +
        json.dumps({"role": "assistant", "content": "They exchange a key."}) + "\n",
        encoding="utf-8",
    )
    dm.PREMISE.write_text("A town with a forge.", encoding="utf-8")
    seen = {}

    def chat(messages, **kwargs):
        seen["prompt"] = messages[-1]["content"]
        return "No issues."

    monkeypatch.setattr(dm, "chat_complete", chat)
    monkeypatch.setattr(dm, "load_ledger", lambda: [])
    monkeypatch.setattr(dm, "render_ledger", lambda ledger: "")
    monkeypatch.setattr(dm, "set_state", lambda **kwargs: None)
    dm._audit()
    assert "ALIAS COLLISIONS" in seen["prompt"]
    assert '"from": id' in seen["prompt"]


def test_ledger_prompt_has_alias_merge_and_pin_rules():
    for section in ("ROLE:", "INPUTS:", "TASK:", "OUTPUT:", "RULES:", "WHEN UNSURE:", "EXAMPLES:"):
        assert section in dm.LEDGER_PROMPT
    assert "aliases" in dm.LEDGER_PROMPT
    assert "six aliases" in dm.LEDGER_PROMPT
    assert "merge" in dm.LEDGER_PROMPT
    assert "pinned" in dm.LEDGER_PROMPT


def test_narrator_field_definitions_and_shape_rule_appear_once(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "PREMISE", tmp_path / "premise.md")
    monkeypatch.setattr(dm, "SUMMARY", tmp_path / "summary.md")
    monkeypatch.setattr(dm, "STATE", tmp_path / "state.json")
    dm.PREMISE.write_text("A fixed premise.", encoding="utf-8")
    dm.SUMMARY.write_text("", encoding="utf-8")
    dm.STATE.write_text('{"folded": 0, "ledgered": 0}', encoding="utf-8")
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals, "style_enabled": False, "style_guard": False,
        "ledger_enabled": False, "analyst_enabled": False,
        "steering_enabled": False, "notes_routing": False,
    })
    brief = {**BRIEF, "intent": "ask", "clarify": "Which key?"}
    prompt = dm.build_context([{"role": "user", "content": "Which key?"}], brief)
    system_text = prompt[0]["content"]
    user_text = prompt[-1]["content"]
    assert system_text.count("OUTPUT: Three parts") == 1
    assert user_text.count("end=question") == 1
    assert user_text.count("push=0.5") == 1
    assert user_text.count("clarify: ask this one short in-character question") == 1
    assert user_text.count("intent=ask") == 1
    assert system_text.count("Word target:") == 1
    assert "Word target: 120 words" in system_text
