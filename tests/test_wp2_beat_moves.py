"""WP2 - beat hooks reach the narrator as mandatory, escalating moves."""
import json

import analyst
import arc


def _arc(**over):
    base = {
        "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
        "clock": {"minutes": 0},
        "acts": [{"n": 1, "status": "active", "goal": "g",
                  "beats": [{"id": "a1b1", "text": "SECRET_BEAT_TEXT",
                             "hook": "A ledger with a torn page", "status": "pending"}],
                  "outcomes": []}],
        "pressures": [],
    }
    base.update(over)
    return base


def _pressure():
    return [{"id": "p1", "status": "active",
             "steps": [{"id": "p1.s1", "status": "pending", "at_minutes": 0, "effect": "Scout"}],
             "deadline_minutes": 900, "effective_deadline_minutes": 900}]


def test_beat_without_hook_never_produces_a_move():
    a = _arc()
    a["acts"][0]["beats"][0]["hook"] = ""
    assert arc.allowed_moves(a, "standard", 1) == []


def test_normalization_preserves_hook_and_superseded_status():
    beats = arc.normalize_beats([{"id": "a1b1", "text": "old", "hook": "lead",
                                  "status": "superseded"}])
    assert beats[0]["hook"] == "lead"
    assert beats[0]["status"] == "superseded"
    assert arc.allowed_moves(_arc(acts=[{"n": 1, "beats": beats}]), "standard", 1) == []


def test_fill_empty_pressure_persists_revision(tmp_path):
    a = _arc(pressures=[{"id": "p1", "status": "active", "steps": [],
                         "deadline_minutes": 900, "effective_deadline_minutes": 900}])
    arc.save_arc(tmp_path, a)
    arc.fill_empty_pressures(tmp_path, a)
    saved = arc.load_arc(tmp_path)
    assert len(saved["pressures"][0]["steps"]) == 2
    assert saved["revision"] == 1
    assert (tmp_path / "arc_log.jsonl").exists()


def test_beat_move_appears_when_no_pressure_is_due():
    moves = arc.allowed_moves(_arc(), "standard", 1)
    assert [m["id"] for m in moves] == ["beat.a1b1"]
    assert moves[0]["note"] == "A ledger with a torn page"
    assert moves[0]["how"] == "subtle"


def test_beat_move_waits_for_the_stall_threshold_when_pressure_is_due():
    a = _arc(pressures=_pressure())
    assert [m["id"] for m in arc.allowed_moves(a, "standard", 1, stalled_turns=0)] == ["p1.s1"]
    assert [m["id"] for m in arc.allowed_moves(a, "standard", 1, stalled_turns=3)] == ["p1.s1", "beat.a1b1"]


def test_two_move_cap_is_kept():
    a = _arc(pressures=[{"id": "p1", "status": "active",
                         "steps": [{"id": "p1.s1", "status": "pending", "at_minutes": 0, "effect": "A"},
                                   {"id": "p1.s2", "status": "pending", "at_minutes": 0, "effect": "B"}],
                         "deadline_minutes": 900, "effective_deadline_minutes": 900}])
    moves = arc.allowed_moves(a, "standard", 1, stalled_turns=9)
    assert len(moves) == 2
    assert all(m["id"].startswith("p1.") for m in moves)


def test_hook_escalates_subtle_overt_offer():
    a = _arc()
    assert arc.allowed_moves(a, "standard", 1, stalled_turns=0)[0]["how"] == "subtle"
    assert arc.allowed_moves(a, "standard", 1, stalled_turns=2)[0]["how"] == "overt"
    assert arc.allowed_moves(a, "standard", 1, stalled_turns=4)[0]["how"] == "offer"


def test_beat_move_ignores_the_overt_cooldown():
    moves = arc.allowed_moves(_arc(), "standard", turn=2, last_overt_turn=1, stalled_turns=2)
    assert moves[0]["how"] == "overt"


def test_allowed_moves_is_pure():
    a = _arc()
    before = json.dumps(a, sort_keys=True)
    arc.allowed_moves(a, "standard", 1, stalled_turns=4)
    assert json.dumps(a, sort_keys=True) == before


def test_analyst_empty_plot_moves_still_yields_due_moves():
    sched = {"beat.a1b1": {"id": "beat.a1b1", "how": "subtle", "note": "A ledger with a torn page"}}
    out = analyst.clamp_brief({"plot_moves": []}, sched)
    assert out["plot_moves"] == [{"id": "beat.a1b1", "how": "subtle", "note": "A ledger with a torn page"}]


def test_deferred_move_is_not_forced_in():
    sched = {"beat.a1b1": {"id": "beat.a1b1", "how": "subtle", "note": "hook"}}
    out = analyst.clamp_brief({"plot_moves": [], "deferred": [{"id": "beat.a1b1", "reason": "mid-scene"}]}, sched)
    assert out["plot_moves"] == []
    assert out["deferred"] == [{"id": "beat.a1b1", "reason": "mid-scene"}]


def test_defer_cap_forces_the_move_to_be_played():
    sched = {"beat.a1b1": {"id": "beat.a1b1", "how": "subtle", "note": "hook"}}
    out = analyst.clamp_brief({"plot_moves": [], "deferred": [{"id": "beat.a1b1", "reason": "again"}]},
                              sched, defer_counts={"beat.a1b1": 2})
    assert out["deferred"] == []
    assert [m["id"] for m in out["plot_moves"]] == ["beat.a1b1"]


def test_fallback_brief_plays_due_moves():
    sched = [{"id": "beat.a1b1", "how": "overt", "note": "A ledger with a torn page"}]
    brief = analyst.make_fallback_brief("timeout", allowed_moves=sched)
    assert brief["plot_moves"] == [{"id": "beat.a1b1", "how": "overt", "note": "A ledger with a torn page"}]
    assert brief.diagnostics == {"fallback_reason": "timeout"}


def test_offer_is_a_valid_how():
    sched = {"beat.a1b1": {"id": "beat.a1b1", "how": "offer", "note": "hook"}}
    out = analyst.clamp_brief({"plot_moves": [{"id": "beat.a1b1"}]}, sched)
    assert out["plot_moves"][0]["how"] == "offer"


def test_playing_a_beat_hook_does_not_complete_the_beat(tmp_path):
    arc.save_arc(tmp_path, _arc())
    rec = json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False})
    arc.run_record_pass(tmp_path, "x", "y", 1, lambda m, **k: rec,
                        played_moves=[{"id": "beat.a1b1", "how": "subtle"}])
    state = arc.load_arc(tmp_path)
    assert state["acts"][0]["beats"][0]["status"] == "pending"
    assert state["runtime"]["stalled_turns"] == 1


def test_beat_text_never_reaches_a_player_facing_payload(tmp_path):
    arc.save_arc(tmp_path, _arc())
    rec = json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False})
    arc.run_record_pass(tmp_path, "x", "y", 1, lambda m, **k: rec)
    for name in ("records.jsonl", "steering.json"):
        p = tmp_path / name
        if p.exists():
            assert "SECRET_BEAT_TEXT" not in p.read_text(encoding="utf-8")
