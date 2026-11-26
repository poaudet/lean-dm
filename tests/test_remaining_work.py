"""Regression tests for the remaining steering/arc review findings."""
import copy
import json

import pytest

import analyst
import arc
import dm
import steering


def _arc_state():
    return {
        "version": 1,
        "revision": 0,
        "central_question": "Will the gate open?",
        "act": 1,
        "clock": {"minutes": 100},
        "acts": [
            {"n": 1, "status": "active", "goal": "Find the key", "beats": [
                {"id": f"a1b{i}", "text": f"private beat {i}", "hook": f"A clue {i}", "status": "pending"}
                for i in range(1, 4)
            ], "outcomes": []},
            {"n": 2, "status": "planned", "goal": "Open the gate", "beats": [], "outcomes": []},
            {"n": 3, "status": "planned", "goal": "Escape", "beats": [], "outcomes": []},
        ],
        "pressures": [{
            "id": "p1", "antagonist": "The Ash King", "goal": "Capture the witness", "status": "active",
            "steps": [
                {"id": "p1.s1", "at_minutes": 60, "effect": "A scout arrives", "status": "pending"},
                {"id": "p1.s2", "at_minutes": 300, "effect": "The trap closes", "status": "pending"},
            ],
            "deadline_minutes": 600, "effective_deadline_minutes": 600,
        }],
    }


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def _cfg(monkeypatch, **overrides):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-narrator", "scribe_model": "test-scribe",
        "analyst_model": "test-analyst", "planner_model": "test-planner",
        "ledger_enabled": False, "notes_routing": False,
        "arc_enabled": True, "analyst_enabled": False, "steering_enabled": False,
        "difficulty": "standard", "drift_turns": 4, "act_max_turns": 0,
        **overrides,
    })


def test_open_question_omission_preserves_but_explicit_empty_clears():
    previous = {"objective": "Find the key", "open_question": "Which door?", "active_hook": None,
                "turns_since_progress": 2, "updated_msg": 1, "source": "record"}
    omitted = steering.clamp_steering({"objective": "Find the key"}, previous, 2)
    cleared = steering.clamp_steering({"open_question": ""}, previous, 2)
    nulled = steering.clamp_steering({"open_question": None}, previous, 2)
    assert omitted["open_question"] == "Which door?"
    assert cleared["open_question"] == ""
    assert nulled["open_question"] == ""


def test_record_rejects_string_booleans_and_foreign_or_unknown_ids(tmp_path, monkeypatch):
    _cfg(monkeypatch, steering_enabled=False)
    state = _arc_state()
    state["runtime"] = {"stalled_turns": 3, "off_arc_turns": 2}
    arc.save_arc(tmp_path, state)
    raw = {
        "elapsed_minutes": 5,
        "beats_done": ["missing", "a2b1", "a2b1"],
        "pressures": [{"id": "unknown", "effect": "cancelled"}],
        "act_complete": "true",
        "central_question_resolved": "true",
        "on_arc": "false",
        "matched_beats": ["a2b1", "a1b2", "a1b2"],
    }
    arc.run_record_pass(tmp_path, "I look around", "Dust falls.", 1,
                        lambda *args, **kwargs: json.dumps(raw))
    saved = arc.load_arc(tmp_path)
    active = saved["acts"][0]
    assert [b["status"] for b in active["beats"]] == ["pending", "pending", "pending"]
    assert saved["act"] == 1
    assert saved["pressures"][0]["status"] == "active"
    assert saved["runtime"]["stalled_turns"] == 4
    assert saved["runtime"]["off_arc_turns"] == 0  # malformed on_arc defaults safely to true
    assert saved["runtime"]["matched_beats"] == ["a1b2"]


def test_only_a_new_valid_beat_resets_stall_counter(tmp_path, monkeypatch):
    _cfg(monkeypatch, steering_enabled=False)
    state = _arc_state()
    state["runtime"] = {"stalled_turns": 3}
    arc.save_arc(tmp_path, state)
    raw = {"elapsed_minutes": 5, "beats_done": ["a1b1"], "pressures": [], "act_complete": False}
    arc.run_record_pass(tmp_path, "I find the key", "The lock clicks.", 1,
                        lambda *args, **kwargs: json.dumps(raw))
    saved = arc.load_arc(tmp_path)
    assert saved["acts"][0]["beats"][0]["status"] == "done"
    assert saved["runtime"]["stalled_turns"] == 0


def test_clarify_suppresses_due_moves_and_does_not_advance_arc(tmp_path, monkeypatch):
    _cfg(monkeypatch, steering_enabled=False)
    state = _arc_state()
    state["runtime"] = {"stalled_turns": 2}
    arc.save_arc(tmp_path, state)
    due = {"p1.s1": {"id": "p1.s1", "how": "overt", "note": "A scout appears"}}
    brief = analyst.clamp_brief({
        "intent": "act", "clarify": "Which key do you mean?",
        "plot_moves": [{"id": "p1.s1"}], "deferred": [{"id": "p1.s1", "reason": "wait"}],
        "act_complete": True,
    }, due)
    assert brief["plot_moves"] == []
    assert brief["deferred"] == []
    assert brief["act_complete"] is False
    assert dm._record_kwargs(brief)["clarify"] is True

    called = []
    arc.run_record_pass(tmp_path, "Use the key", "Which key?", 1,
                        lambda *args, **kwargs: called.append(True),
                        played_moves=brief["plot_moves"], clarify=True)
    saved = arc.load_arc(tmp_path)
    assert called == []
    assert saved["clock"]["minutes"] == 100
    assert saved["runtime"]["stalled_turns"] == 2
    assert saved["pressures"][0]["steps"][0]["status"] == "pending"
    assert arc.load_records(tmp_path)[-1]["elapsed_minutes"] == 0


def test_clarify_turn_does_not_autofill_pressure_steps(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, _arc_state())
    _cfg(monkeypatch)
    fill_calls = []
    monkeypatch.setattr(dm.arc, "run_record_pass", lambda *a, **k: None)
    monkeypatch.setattr(dm.arc, "fill_empty_pressures", lambda *a, **k: fill_calls.append(True))
    brief = {"intent": "act", "clarify": "Which door?", "plot_moves": []}
    user = {"role": "user", "content": "Which door?"}
    dm._arc_step("Which door?", "The north door.", 1, [], user, [], brief, False)
    assert fill_calls == []


def test_pressure_autofill_stays_before_deadline_with_spacing(tmp_path):
    state = _arc_state()
    state["clock"]["minutes"] = 160
    state["pressures"][0]["deadline_minutes"] = 300
    state["pressures"][0]["effective_deadline_minutes"] = 300
    state["pressures"][0]["steps"] = [
        {"id": "p1.s1", "at_minutes": 150, "effect": "done", "status": "done"}
    ]
    arc.save_arc(tmp_path, state)
    arc.fill_empty_pressures(tmp_path, arc.load_arc(tmp_path))
    steps = arc.load_arc(tmp_path)["pressures"][0]["steps"]
    added = [s["at_minutes"] for s in steps if s["status"] == "pending"]
    assert added == [220]
    assert all(at <= 300 - arc.MIN_DEADLINE_GAP for at in added)
    assert all(b - a >= 60 for a, b in zip([160] + added, added))


def test_analyst_prompt_cap_includes_all_sections_and_preserves_action(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_enabled=True, analyst_prompt_cap=1000,
         analyst_context_turns=5, analyst_summary_tok=10000, chars_per_tok=4.0,
         analyst_timeout_sec=2, analyst_retries=0)
    seen = {}

    def chat(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return json.dumps({"intent": "act", "assessment": "ok", "outcome": "clean"})

    history = [{"role": "user", "content": "old play " + "x" * 5000},
               {"role": "assistant", "content": "old reply " + "y" * 5000}]
    analyst.run_analyst(
        tmp_path, history, "UNIQUE_PLAYER_ACTION_SENTINEL " + "z" * 100,
        [{"id": f"move-{i}", "how": "subtle", "note": "move " + "m" * 100} for i in range(10)],
        ["avoid " + "g" * 100 for _ in range(10)], chat,
        premise="P" * 5000, summary="S" * 5000, ledger_text="L" * 5000,
        act_goal="G" * 500, steering_text="T" * 500,
        hooks=["H" * 500 for _ in range(5)],
    )
    assert len(seen["user"]) <= 1000
    assert "UNIQUE_PLAYER_ACTION_SENTINEL" in seen["user"]
    assert "PLAYER ACTION:" in seen["user"]
    assert "RULES:" in seen["system"]


def test_analyst_prompt_cap_handles_minimum_configured_limit(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_enabled=True, analyst_prompt_cap=500,
         analyst_context_turns=5, analyst_summary_tok=10000, chars_per_tok=4.0,
         analyst_timeout_sec=2, analyst_retries=0)
    seen = {}

    def chat(messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return json.dumps({"intent": "act", "assessment": "ok", "outcome": "clean"})

    analyst.run_analyst(tmp_path, [], "A short current action.", [], [], chat,
                        premise="P" * 5000, summary="S" * 5000, ledger_text="L" * 5000)
    assert len(seen["user"]) <= 500
    assert "A short current action." in seen["user"]


def test_planner_limits_are_present_in_initial_replan_and_next_act_prompts(monkeypatch):
    _cfg(monkeypatch, difficulty="standard")
    prompts = []

    def capture(messages, **kwargs):
        prompts.append(messages[0]["content"])
        if "initial arc" in messages[0]["content"].lower() or "three-act" in messages[0]["content"].lower():
            return json.dumps(_arc_state())
        return json.dumps([
            {"id": f"new{i}", "text": f"Private beat {i}", "hook": f"A lead {i}", "status": "pending"}
            for i in range(1, 4)
        ])

    state = _arc_state()
    arc.plan_initial_arc("premise", "", "", [], capture)
    arc.plan_replan_arc(copy.deepcopy(state), [], [], capture)
    arc.plan_next_act_beats(copy.deepcopy(state), 2, [], capture)
    assert len(prompts) == 3
    for prompt in prompts:
        assert "integer >= 0" in prompt
        assert "30 minutes" in prompt
        assert "14400" in prompt
        assert "1.5 for standard difficulty" in prompt


@pytest.mark.parametrize("arc_enabled,analyst_enabled", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_arc_and_analyst_flag_combinations_keep_due_moves(monkeypatch, tmp_path,
                                                          arc_enabled, analyst_enabled):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    state = _arc_state()
    state["clock"]["minutes"] = 100
    arc.save_arc(root, state)
    _cfg(monkeypatch, arc_enabled=arc_enabled, analyst_enabled=analyst_enabled)
    seen = {}

    def fake(messages, model=None, **kwargs):
        if model == "test-analyst":
            return json.dumps({
                "intent": "act", "assessment": "act", "outcome": "clean",
                "plot_moves": [{"id": "p1.s1", "how": "subtle", "note": "A scout appears"}],
                "keep_true": [], "narration": {"words": 100, "push": 0.5, "end": "decision"},
                "clarify": None, "act_complete": False,
            })
        if model == "test-scribe":
            return json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False})
        seen["prompt"] = messages[-1]["content"]
        return "A scout moves in the dark."

    monkeypatch.setattr(dm, "chat_complete", fake)
    dm.player_turn("I watch the road.")
    if arc_enabled:
        saved = arc.load_arc(root)
        assert saved["pressures"][0]["steps"][0]["status"] == "done"
        assert "p1.s1" in seen["prompt"]
    else:
        assert "p1.s1" not in seen["prompt"]


def test_arc_debug_is_exposed_only_when_arc_is_enabled(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    state = _arc_state()
    arc.save_arc(root, state)
    _cfg(monkeypatch, arc_enabled=True, arc_debug=True)
    exposed = dm.game_state()["arc_debug"]
    assert exposed["central_question"] == "Will the gate open?"
    assert exposed["acts"][0]["beats"][0]["text"] == "private beat 1"
    assert exposed["pressures"][0]["antagonist"] == "The Ash King"

    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "arc_enabled": False})
    assert dm.game_state()["arc_debug"] is None


def test_arc_debug_needs_debug_mode(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, _arc_state())
    _cfg(monkeypatch, arc_enabled=True)             # arc on, debug mode off (default)
    state = dm.game_state()
    assert state["arc_debug"] is None
    assert state["arc_chip"]                        # the non-spoiler chip stays
    assert "private beat 1" not in json.dumps(state)
    assert "The Ash King" not in json.dumps(state)
    _cfg(monkeypatch, arc_enabled=True, arc_debug=True)
    assert dm.game_state()["arc_debug"]["pressures"][0]["antagonist"] == "The Ash King"
    assert dm.cfg.arc_debug is True
    _cfg(monkeypatch, arc_enabled=False, arc_debug=True)
    assert dm.game_state()["arc_debug"] is None     # debug mode never exposes a disabled arc


def test_ooc_prompt_has_examples_section():
    assert "EXAMPLES:" in analyst.OOC_SYSTEM


def test_active_hook_is_populated_and_explicit_null_clears():
    previous = {"objective": "Find the key", "open_question": "Which door?",
                "active_hook": "An old lead", "turns_since_progress": 0,
                "updated_msg": 1, "source": "record"}
    updated = steering.clamp_steering({"active_hook": "A torn map points north"}, previous, 2)
    cleared = steering.clamp_steering({"active_hook": None}, updated, 3)
    assert updated["active_hook"] == "A torn map points north"
    assert "Live lead: A torn map points north" in steering.render_steering(updated)
    assert cleared["active_hook"] is None


def test_planner_rejects_float_and_too_close_pressure_steps(monkeypatch):
    _cfg(monkeypatch)
    state = _arc_state()
    state["pressures"][0]["steps"][1]["at_minutes"] = 80
    ok, reason = arc.validate_arc(state)
    assert not ok and "30 minutes apart" in reason
    state = _arc_state()
    state["pressures"][0]["steps"][0]["at_minutes"] = 60.5
    ok, reason = arc._validate_planner_time_schema(state)
    assert not ok and "JSON integer" in reason


def test_clarify_at_pressure_deadline_leaves_deadline_move_due(tmp_path, monkeypatch):
    _cfg(monkeypatch, steering_enabled=False)
    state = _arc_state()
    state["clock"]["minutes"] = 600
    pressure = state["pressures"][0]
    pressure["deadline_minutes"] = 600
    pressure["effective_deadline_minutes"] = 600
    for step in pressure["steps"]:
        step["status"] = "done"
    arc.save_arc(tmp_path, state)
    due = arc.allowed_moves(arc.load_arc(tmp_path), "standard", turn=1)
    assert [move["id"] for move in due] == ["p1.deadline"]

    brief = analyst.clamp_brief({"clarify": "Which door do you mean?",
                                 "plot_moves": [{"id": "p1.deadline"}],
                                 "deferred": [{"id": "p1.deadline", "reason": "wait"}]},
                                {"p1.deadline": due[0]})
    assert brief["plot_moves"] == [] and brief["deferred"] == []
    arc.run_record_pass(tmp_path, "Which door?", "Which door do you mean?", 1,
                        lambda *args, **kwargs: pytest.fail("clarify must not call record"),
                        clarify=True)
    saved = arc.load_arc(tmp_path)
    assert saved["clock"]["minutes"] == 600
    assert saved["pressures"][0]["status"] == "active"
    assert arc.allowed_moves(saved, "standard", turn=2)[0]["id"] == "p1.deadline"
