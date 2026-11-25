"""Sprint 2 / 12 - persisted scheduling state flows through player_turn."""
import copy
import json

import arc
import dm


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


ARC = {
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


def _cfg(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True, "analyst_enabled": True, "difficulty": "standard",
        "api_key": "test", "model": "test-narrator", "analyst_model": "test-analyst",
        "scribe_model": "test-scribe", "ledger_enabled": False,
    })


def _fake(brief, scribe_calls):
    def fake(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-analyst":
            return json.dumps(brief)
        if model == "test-scribe":
            scribe_calls.append(1)
            return json.dumps({"elapsed_minutes": 15, "beats_done": [], "pressures": [], "act_complete": False})
        return "Narration."
    return fake


def _brief(intent="act", moves=()):
    return {"intent": intent, "assessment": "a", "outcome": "clean", "plot_moves": list(moves),
            "keep_true": [], "narration": {"words": 100, "push": 0.5, "end": "decision"},
            "clarify": None, "act_complete": False}


def test_get_allowed_moves_honours_persisted_cooldown(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    a = copy.deepcopy(ARC)
    a["runtime"] = {"last_overt_turn": 1}
    arc.save_arc(root, a)
    _cfg(monkeypatch)
    moves = dm.get_allowed_moves()
    assert [m["how"] for m in moves] == ["subtle", "subtle"]


def test_played_overt_move_is_consumed_and_starts_cooldown(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, copy.deepcopy(ARC))
    _cfg(monkeypatch)
    brief = _brief(moves=[{"id": "p1.s1", "how": "overt", "note": "n"}])
    brief["deferred"] = [{"id": "p1.s2", "reason": "keep the next pressure for the next scene"}]
    monkeypatch.setattr(dm, "chat_complete", _fake(brief, []))

    dm.player_turn("I search the alley.")

    a = arc.load_arc(root)
    assert a["pressures"][0]["steps"][0]["status"] == "done"
    assert a["runtime"]["last_overt_turn"] == 1
    nxt = dm.get_allowed_moves()
    assert [(m["id"], m["how"]) for m in nxt] == [("p1.s2", "subtle")]


def test_ask_turn_skips_record_pass_and_keeps_clock(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, copy.deepcopy(ARC))
    _cfg(monkeypatch)
    scribe_calls = []
    monkeypatch.setattr(dm, "chat_complete", _fake(_brief(intent="ask"), scribe_calls))

    dm.player_turn("What is the barkeep's name?")

    assert scribe_calls == []
    assert arc.load_arc(root)["clock"]["minutes"] == 100


def test_undo_restores_runtime_state(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, copy.deepcopy(ARC))
    _cfg(monkeypatch)
    monkeypatch.setattr(dm, "chat_complete",
                        _fake(_brief(moves=[{"id": "p1.s1", "how": "overt", "note": "n"}]), []))
    dm.player_turn("I search the alley.")
    dm.undo_turn()
    a = arc.load_arc(root)
    assert a["pressures"][0]["steps"][0]["status"] == "pending"
    assert "last_overt_turn" not in a.get("runtime", {})
