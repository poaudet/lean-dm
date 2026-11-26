"""Sprint 2 / 10 - runtime scheduling state, played moves, deadlines, delays, ask turns."""
import copy
import json

import arc

BASE = {
    "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
    "clock": {"minutes": 100},
    "acts": [{"n": 1, "status": "active", "goal": "g",
              "beats": [{"id": "a1b1", "text": "x", "status": "pending"}], "outcomes": []}],
    "pressures": [{
        "id": "p1", "antagonist": "Cult", "goal": "Kill the witness", "status": "active",
        "steps": [
            {"id": "p1.s1", "at_minutes": 60, "effect": "Scout seen", "status": "pending"},
            {"id": "p1.s2", "at_minutes": 90, "effect": "Poisoner arrives", "status": "pending"},
            {"id": "p1.s3", "at_minutes": 150, "effect": "Strike", "status": "pending"},
        ],
        "deadline_minutes": 300, "effective_deadline_minutes": 300,
    }],
}


def _rec(**kw):
    base = {"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False}
    base.update(kw)
    return json.dumps(base)


def _statuses(root):
    return [s["status"] for s in arc.load_arc(root)["pressures"][0]["steps"]]


def test_overt_cooldown_respected_by_allowed_moves():
    moves = arc.allowed_moves(BASE, "standard", turn=2, last_overt_turn=1)
    assert [m["how"] for m in moves] == ["subtle", "subtle"]
    assert arc.allowed_moves(BASE, "standard", turn=3, last_overt_turn=1)[0]["how"] == "overt"


def test_apply_played_moves_marks_done_and_sets_last_overt():
    a = copy.deepcopy(BASE)
    played, touched = arc.apply_played_moves(a, [{"id": "p1.s1", "how": "overt"}], 4)
    assert a["pressures"][0]["steps"][0]["status"] == "done"
    assert a["runtime"]["last_overt_turn"] == 4
    assert touched == {"p1"}
    b = copy.deepcopy(BASE)
    arc.apply_played_moves(b, [{"id": "p1.s1", "how": "subtle"}], 4)
    assert "last_overt_turn" not in b["runtime"]


def test_deadline_move_is_first_overt_then_pressure_expires():
    a = copy.deepcopy(BASE)
    a["clock"]["minutes"] = 300
    moves = arc.allowed_moves(a, "standard", turn=1)
    assert moves[0]["id"] == "p1.deadline" and moves[0]["how"] == "overt"
    arc.apply_played_moves(a, [moves[0]], 1)
    assert a["pressures"][0]["status"] == "expired"
    assert arc.allowed_moves(a, "standard", turn=2) == []


def test_counter_slip_is_capped():
    for diff, cap in (("story", 600), ("standard", 450), ("hard", 300)):
        p = {"deadline_minutes": 300, "effective_deadline_minutes": 300}
        for _ in range(10):
            arc.apply_counter_slip(p, diff, 100)
        assert p["effective_deadline_minutes"] == cap


def test_delayed_effect_pushes_pending_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(arc.cfg, "_vals", {**arc.cfg._vals, "difficulty": "standard"})
    arc.save_arc(tmp_path, copy.deepcopy(BASE))
    rec = _rec(pressures=[{"id": "p1", "effect": "delayed"}])
    arc.run_record_pass(tmp_path, "x", "y", 1, lambda m, **k: rec)
    steps = arc.load_arc(tmp_path)["pressures"][0]["steps"]
    assert [s["at_minutes"] for s in steps] == [90, 120, 180]


def test_played_step_not_double_advanced(tmp_path):
    arc.save_arc(tmp_path, copy.deepcopy(BASE))
    rec = _rec(pressures=[{"id": "p1", "effect": "advanced"}])
    arc.run_record_pass(tmp_path, "x", "y", 1, lambda m, **k: rec,
                        played_moves=[{"id": "p1.s2", "how": "subtle"}])
    assert _statuses(tmp_path) == ["pending", "done", "pending"]


def test_ask_turn_costs_no_time_and_skips_recorder(tmp_path):
    arc.save_arc(tmp_path, copy.deepcopy(BASE))
    called = []

    def boom(messages, **kw):
        called.append(1)
        return _rec()

    arc.run_record_pass(tmp_path, "what is his name?", "Bob.", 1, boom, intent="ask")
    assert called == []
    assert arc.load_arc(tmp_path)["clock"]["minutes"] == 100
    assert arc.load_records(tmp_path)[0]["elapsed_minutes"] == 0


def test_stalled_turns_count_and_reset(tmp_path):
    arc.save_arc(tmp_path, copy.deepcopy(BASE))
    for i in range(3):
        arc.run_record_pass(tmp_path, "x", "y", 1 + 2 * i, lambda m, **k: _rec())
    assert arc.load_arc(tmp_path)["runtime"]["stalled_turns"] == 3
    arc.run_record_pass(tmp_path, "x", "y", 7, lambda m, **k: _rec(),
                        played_moves=[{"id": "rescue_opening", "how": "subtle"}])
    # A played rescue opening does not complete a beat; only beats_done resets the stall count.
    assert arc.load_arc(tmp_path)["runtime"]["stalled_turns"] == 4
