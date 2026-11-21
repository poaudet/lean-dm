"""Sprint 1 / 03 - arc_log is keyed by message index, so undo can truncate it."""
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
    "clock": {"minutes": 0},
    "acts": [
        {"n": 1, "status": "active", "goal": "g",
         "beats": [{"id": "a1b1", "text": "x", "status": "pending"}], "outcomes": []},
        {"n": 2, "status": "planned", "goal": "g2", "beats": [], "outcomes": []},
        {"n": 3, "status": "planned", "goal": "g3", "beats": [], "outcomes": []},
    ],
    "pressures": [],
}


def test_act_boundary_logged_by_msg_idx_and_removed_by_undo(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, ARC)
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "scribe_model": "test-scribe",
        "ledger_enabled": False,
    })

    calls = {"rec": 0}

    def fake(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        sys_msg = messages[0]["content"]
        if "story planner" in sys_msg:
            return '[{"id": "a2b1", "text": "Next", "status": "pending"}]'
        if "continuity recorder" in sys_msg:
            calls["rec"] += 1
            return json.dumps({"elapsed_minutes": 10, "beats_done": [], "pressures": [],
                               "act_complete": calls["rec"] == 3})
        return "Narration."

    monkeypatch.setattr(dm, "chat_complete", fake)

    for i in range(3):
        dm.player_turn(f"Action {i}.")

    entries = [json.loads(l) for l in arc.arc_log_path(root).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    # 3rd exchange = history indices 4 (player) and 5 (DM); the old code logged len(records) == 3
    assert entries[0]["at_msg"] == 5

    dm.undo_turn()

    remaining = [l for l in arc.arc_log_path(root).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert remaining == []
    assert arc.load_arc(root)["act"] == 1
