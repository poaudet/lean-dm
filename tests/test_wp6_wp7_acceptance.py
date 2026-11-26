"""Acceptance coverage for WP6 arc corrections (A1-A6) and WP7 canon naming (B1-B4)."""
import copy
import json

import analyst
import arc
import dm
import ledger


def _arc():
    return {
        "version": 1, "revision": 1, "central_question": "Will the gate open?", "act": 1,
        "clock": {"minutes": 0},
        "acts": [
            {"n": 1, "status": "active", "goal": "Find the key", "started_msg": 0,
             "beats": [{"id": f"a1b{i}", "text": f"private {i}", "hook": f"Visible lead {i}",
                        "status": "pending"} for i in range(1, 4)], "outcomes": []},
            {"n": 2, "status": "planned", "goal": "Open the gate", "beats": [], "outcomes": []},
            {"n": 3, "status": "planned", "goal": "Escape", "beats": [], "outcomes": []},
        ], "pressures": [], "runtime": {"off_arc_turns": 1},
    }


def _record(on_arc=False):
    return json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [],
                       "act_complete": False, "central_question_resolved": False,
                       "on_arc": on_arc, "matched_beats": [], "objective": "Find the key",
                       "open_question": "Which door?", "progress": False})


def _valid_beats(act=1):
    return json.dumps([
        {"id": f"a{act}r2b{i}", "text": f"private new {i}", "hook": f"A new lead {i}", "status": "pending"}
        for i in range(1, 4)
    ])


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(arc.cfg, "_vals", {
        **arc.cfg._vals, "api_key": "test", "model": "test", "scribe_model": "test",
        "planner_model": "test", "difficulty": "standard", "arc_enabled": True,
        "steering_enabled": False, "drift_turns": 2, "act_max_turns": 0, **over,
    })


def test_a1_planned_empty_acts_validate_but_short_active_act_does_not():
    state = _arc()
    assert arc.validate_arc(state) == (True, "")
    state["acts"][0]["beats"] = state["acts"][0]["beats"][:2]
    assert arc.validate_arc(state)[0] is False


def test_a1_beat_requires_hook():
    state = _arc()
    state["acts"][0]["beats"][0]["hook"] = " "
    ok, reason = arc.validate_arc(state)
    assert not ok and "empty hook" in reason


def test_a2_successful_replan_resets_saved_drift_counter(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    arc.save_arc(tmp_path, state)
    calls = []

    def chat(messages, **kwargs):
        calls.append(messages[0]["content"])
        return _record(False) if "continuity recorder" in messages[0]["content"] else _valid_beats()

    arc.run_record_pass(tmp_path, "side quest", "A side scene.", 3, chat, history=[])
    saved = arc.load_arc(tmp_path)
    assert saved["runtime"]["off_arc_turns"] == 0
    assert len(calls) == 2
    assert saved["acts"][0]["beats"][0]["status"] == "superseded"


def test_a2_failed_replan_persists_reset_and_logs_threshold(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    arc.save_arc(tmp_path, state)

    def chat(messages, **kwargs):
        if "continuity recorder" in messages[0]["content"]:
            return _record(False)
        return json.dumps([{"id": "bad", "text": "private", "hook": "Only one", "status": "pending"}])

    arc.run_record_pass(tmp_path, "side quest", "A side scene.", 3, chat, history=[])
    saved = arc.load_arc(tmp_path)
    assert saved["runtime"]["off_arc_turns"] == 0
    assert [b["status"] for b in saved["acts"][0]["beats"]] == ["pending"] * 3
    log = [json.loads(line) for line in arc.arc_log_path(tmp_path).read_text().splitlines()]
    failed = [entry for entry in log if entry["kind"] == "replan_failed"]
    assert len(failed) == 1
    assert "2 consecutive" in failed[0]["reason"]


def test_a3_replan_retains_superseded_beats_and_unique_revision_ids(monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    updated = arc.plan_replan_arc(state, ["outcome"], [], lambda *a, **k: _valid_beats())
    assert updated is not None
    beats = updated["acts"][0]["beats"]
    assert len(beats) == 6
    assert [b["status"] for b in beats[:3]] == ["superseded"] * 3
    ids = [b["id"] for a in updated["acts"] for b in a["beats"]]
    assert len(ids) == len(set(ids))


def test_a3_failed_replan_restores_exact_prior_beats(monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    before = copy.deepcopy(state["acts"][0]["beats"])
    bad = json.dumps([{"id": "bad", "text": "private", "hook": "Only one", "status": "pending"}])
    assert arc.plan_replan_arc(state, [], [], lambda *a, **k: bad) is None
    assert state["acts"][0]["beats"] == before


def test_a4_no_requested_end_is_not_a_shape_failure():
    assert dm.compute_shape_ok("A complete reply.", None) is True
    assert dm.compute_shape_ok("A complete reply.", "question") is False


def test_a5_pressure_revision_uses_message_index_for_undo(tmp_path):
    state = _arc()
    state["pressures"] = [{"id": "p1", "status": "active", "steps": [],
                           "deadline_minutes": 900, "effective_deadline_minutes": 900}]
    arc.save_arc(tmp_path, state)
    arc.fill_empty_pressures(tmp_path, state, msg_idx=6)
    entry = json.loads(arc.arc_log_path(tmp_path).read_text().splitlines()[-1])
    assert entry["kind"] == "pressure_revision"
    assert entry["at_msg"] == 6
    arc.truncate_arc_log(tmp_path, 5)
    assert arc.arc_log_path(tmp_path).read_text() == ""


def test_a6_initial_planner_retries_invalid_arc_once(monkeypatch):
    _cfg(monkeypatch)
    valid = _arc()
    invalid = copy.deepcopy(valid)
    invalid["acts"][0]["beats"] = invalid["acts"][0]["beats"][:2]
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        return json.dumps(invalid if len(calls) == 1 else valid)

    result = arc.plan_initial_arc("premise", "", "", [], chat)
    assert len(calls) == 2
    assert result["act"] == 1


def test_a6_replan_retries_invalid_output_once(monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        return json.dumps([{"id": "bad", "text": "private", "hook": "One", "status": "pending"}]) \
            if len(calls) == 1 else _valid_beats()

    result = arc.plan_replan_arc(state, [], [], chat)
    assert result is not None
    assert len(calls) == 2


def test_a6_next_act_fallback_is_logged(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    state = _arc()
    arc.save_arc(tmp_path, state)

    def chat(messages, **kwargs):
        if "Summarize factual play outcomes" in messages[0]["content"]:
            return json.dumps(["The key was found."])
        return json.dumps([{"id": "bad", "text": "private", "hook": "Only one", "status": "pending"}])

    arc.transition_act(tmp_path, 1, chat, msg_idx=5, history=[])
    log = [json.loads(line) for line in arc.arc_log_path(tmp_path).read_text().splitlines()]
    fallback = [entry for entry in log if entry["kind"] == "next_act_fallback"]
    assert len(fallback) == 1
    assert fallback[0]["diff"] == {"fallback_beats": 1}


def test_a6_normalize_arc_clamps_effective_deadline(monkeypatch):
    _cfg(monkeypatch, difficulty="standard")
    state = _arc()
    state["pressures"] = [{"id": "p1", "status": "active", "steps": [],
                           "deadline_minutes": 900, "effective_deadline_minutes": 5000}]
    normalized = arc.normalize_arc(state)
    assert normalized["pressures"][0]["effective_deadline_minutes"] <= 900 * arc.DEADLINE_MAX_STRETCH["standard"]


def test_b1_merge_adds_source_name_as_alias_and_skips_pinned():
    original = [
        {"id": "bran", "name": "Bran", "state": "alive", "aliases": [], "status": "active"},
        {"id": "the-smith", "name": "The Smith", "state": "at forge", "aliases": ["blacksmith"], "status": "active"},
    ]
    merged = ledger.apply_ledger_ops(copy.deepcopy(original), {"merge": [{"from": "the-smith", "into": "bran"}]})
    bran = next(entry for entry in merged if entry["id"] == "bran")
    assert bran["aliases"] == ["blacksmith", "the smith"]
    assert len(merged) == 1
    pinned = copy.deepcopy(original)
    pinned[0]["pinned"] = True
    unchanged = ledger.apply_ledger_ops(pinned, {"merge": [{"from": "the-smith", "into": "bran"}]})
    assert len(unchanged) == 2


def test_b2_analyst_resolves_aliases_for_keep_true_and_mentions():
    entries = [{"id": "smith", "name": "Tomas", "state": "at forge", "aliases": ["the smith"],
                "kind": "person", "status": "active", "pinned": True}]
    assert analyst._ledger_entry(entries, "the smith") is entries[0]
    assert analyst._mentioned(entries[0], "I ask the smith about the key.") is True
    result = analyst.clamp_brief({"keep_true": [{"id": "the smith", "why": "named"}]}, ledger=entries)
    assert result["keep_true"] == ["Tomas: at forge"]


def test_b3_ledger_prompt_and_merge_key_are_supported():
    assert '"merge"' in dm.LEDGER_PROMPT
    entries = [
        {"id": "bran", "name": "Bran", "state": "alive", "aliases": [], "status": "active"},
        {"id": "the-smith", "name": "The Smith", "state": "at forge", "aliases": [], "status": "active"},
    ]
    result = ledger.apply_ledger_ops(entries, {"merge": [{"from": "the-smith", "into": "bran"}]})
    assert len(result) == 1
