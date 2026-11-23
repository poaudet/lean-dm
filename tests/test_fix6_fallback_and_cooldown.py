"""FIX6 - the fallback brief carries no steering key; only pressure moves start the cooldown.

Both corrections are recorded in decisions.md as D-28.
"""
import copy
import json

import analyst
import arc


LEDGER = [
    {"id": "witness", "kind": "person", "name": "Mira", "state": "hidden in the cellar",
     "status": "active", "pinned": True},
]

BASE_ARC = {
    "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
    "clock": {"minutes": 100},
    "acts": [{"n": 1, "status": "active", "goal": "g",
              "beats": [{"id": "a1b1", "text": "x", "hook": "A lead", "status": "pending"}],
              "outcomes": []}],
    "pressures": [{"id": "p1", "status": "active",
                   "steps": [{"id": "p1.s1", "at_minutes": 60, "effect": "Scout", "status": "pending"}],
                   "deadline_minutes": 300, "effective_deadline_minutes": 300}],
}


# ---------------------------------------------------------------------------
# FIX6a - the fallback brief has no steering key
# ---------------------------------------------------------------------------

def test_fallback_brief_has_no_steering_key():
    brief = analyst.make_fallback_brief("timeout", steering_text="Objective: Find the witness")
    assert "steering" not in brief


def test_fallback_brief_matches_the_schema_exactly():
    brief = analyst.make_fallback_brief("timeout", steering_text="Objective: Find the witness")
    assert set(brief) == set(analyst.BRIEF_SCHEMA)


def test_fallback_brief_still_accepts_steering_text():
    """Call sites in dm.py pass steering_text; the signature must not change."""
    brief = analyst.make_fallback_brief("timeout", steering_text="anything")
    assert brief["intent"] == "act"


def test_fallback_brief_keeps_keep_true_and_played_moves():
    """C3 still holds without the steering key."""
    brief = analyst.make_fallback_brief(
        "timeout",
        allowed_moves=[{"id": "beat.a1b1", "how": "overt", "note": "A torn page"}],
        ledger=LEDGER, player_action="Mira is here.",
    )
    assert "Mira: hidden in the cellar" in brief["keep_true"]
    assert [m["id"] for m in brief["plot_moves"]] == ["beat.a1b1"]
    assert "steering" not in brief


def test_fallback_brief_diagnostics_unchanged():
    brief = analyst.make_fallback_brief("timeout")
    assert brief.diagnostics == {"fallback_reason": "timeout"}


# ---------------------------------------------------------------------------
# FIX6b - only pressure moves set last_overt_turn
# ---------------------------------------------------------------------------

def test_overt_beat_move_does_not_set_last_overt_turn():
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(a, [{"id": "beat.a1b1", "how": "overt"}], 4)
    assert "last_overt_turn" not in a.get("runtime", {})


def test_overt_pressure_move_sets_last_overt_turn():
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(a, [{"id": "p1.s1", "how": "overt"}], 4)
    assert a["runtime"]["last_overt_turn"] == 4


def test_overt_deadline_move_sets_last_overt_turn():
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(a, [{"id": "p1.deadline", "how": "overt"}], 9)
    assert a["runtime"]["last_overt_turn"] == 9


def test_subtle_pressure_move_does_not_set_last_overt_turn():
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(a, [{"id": "p1.s1", "how": "subtle"}], 4)
    assert "last_overt_turn" not in a.get("runtime", {})


def test_beat_and_pressure_overt_together_sets_last_overt_turn():
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(
        a, [{"id": "beat.a1b1", "how": "overt"}, {"id": "p1.s1", "how": "overt"}], 7)
    assert a["runtime"]["last_overt_turn"] == 7


def test_beat_move_still_does_not_complete_the_beat():
    """B6 is unaffected by the cooldown change."""
    a = copy.deepcopy(BASE_ARC)
    arc.apply_played_moves(a, [{"id": "beat.a1b1", "how": "overt"}], 4)
    assert a["acts"][0]["beats"][0]["status"] == "pending"


def test_played_pressure_step_is_still_marked_done():
    a = copy.deepcopy(BASE_ARC)
    played, touched = arc.apply_played_moves(a, [{"id": "p1.s1", "how": "overt"}], 4)
    assert a["pressures"][0]["steps"][0]["status"] == "done"
    assert touched == {"p1"}


def test_beat_hook_escalation_still_overrides_the_cooldown():
    """D-23: a beat move ignores the cooldown even when one is running.

    stalled_turns=3 reaches the standard stall threshold (3), so the hook is pushed
    alongside the due pressure step; hook_level(3) is "overt" even though
    last_overt_turn=1 would otherwise force "subtle".
    """
    moves = arc.allowed_moves(copy.deepcopy(BASE_ARC), "standard", turn=2,
                              last_overt_turn=1, stalled_turns=3)
    beat = [m for m in moves if m["id"].startswith("beat.")]
    assert beat, f"beat hook was not pushed: {moves}"
    assert beat[0]["how"] == "overt", f"cooldown leaked onto the beat move: {beat[0]}"
    pressure = [m for m in moves if not m["id"].startswith("beat.")]
    assert pressure and pressure[0]["how"] == "subtle", f"pressure move ignored the cooldown: {pressure}"
