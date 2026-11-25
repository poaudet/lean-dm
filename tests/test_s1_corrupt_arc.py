"""Sprint 1 / 04 - an unreadable arc.json is never silently replaced."""
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


PLAN = json.dumps({
    "version": 1, "central_question": "Q?",
    "acts": [{"n": 1, "status": "active", "goal": "g", "beats": [
        {"id": "a1b1", "text": "Private beat 1", "hook": "A visible lead 1", "status": "pending"},
        {"id": "a1b2", "text": "Private beat 2", "hook": "A visible lead 2", "status": "pending"},
        {"id": "a1b3", "text": "Private beat 3", "hook": "A visible lead 3", "status": "pending"},
    ], "outcomes": []}],
    "pressures": [],
})


def _cfg(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True,
        "analyst_enabled": False,
        "api_key": "test",
        "model": "test-model",
        "ledger_enabled": False,
    })


def _fake(planner_calls):
    def fake(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        sys_msg = messages[0]["content"]
        if "hidden story-arc planner" in sys_msg:
            planner_calls.append(1)
            return PLAN
        if "continuity recorder" in sys_msg:
            return json.dumps({"elapsed_minutes": 5, "beats_done": [], "pressures": [], "act_complete": False})
        return "Narration."
    return fake


def test_arc_is_corrupt_states(tmp_path):
    assert arc.arc_is_corrupt(tmp_path) is False            # missing
    arc.save_arc(tmp_path, json.loads(PLAN))
    assert arc.arc_is_corrupt(tmp_path) is False            # valid
    (tmp_path / "arc.json").write_text("{ not json", encoding="utf-8")
    assert arc.arc_is_corrupt(tmp_path) is True


def test_corrupt_arc_not_replaced_by_autoplan(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "arc.json").write_text("{ hand-edited, half finished", encoding="utf-8")
    _cfg(monkeypatch)
    planner_calls = []
    monkeypatch.setattr(dm, "chat_complete", _fake(planner_calls))

    dm.player_turn("I look around.")

    assert planner_calls == []
    assert (root / "arc.json").read_text(encoding="utf-8") == "{ hand-edited, half finished"


def test_missing_arc_still_autoplans(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    planner_calls = []
    monkeypatch.setattr(dm, "chat_complete", _fake(planner_calls))

    dm.player_turn("I look around.")

    assert planner_calls == [1]
    assert arc.load_arc(root) is not None
