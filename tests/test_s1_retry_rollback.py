"""Sprint 1 / 02 - a failed retry must leave arc state untouched."""
import json

import pytest

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
    "clock": {"minutes": 50},
    "acts": [
        {"n": 1, "status": "active", "goal": "g",
         "beats": [{"id": "a1b1", "text": "x", "status": "pending"}], "outcomes": []},
        {"n": 2, "status": "planned", "goal": "g2", "beats": [], "outcomes": []},
    ],
    "pressures": [],
}


def test_failed_retry_keeps_arc_state(monkeypatch, tmp_path):
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

    def ok(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-scribe":
            return json.dumps({"elapsed_minutes": 25, "beats_done": [], "pressures": [], "act_complete": False})
        return "First reply."

    monkeypatch.setattr(dm, "chat_complete", ok)
    dm.player_turn("Turn 1.")
    assert arc.load_arc(root)["clock"]["minutes"] == 75
    records_before = arc.load_records(root)

    def boom(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        raise RuntimeError("provider down")

    monkeypatch.setattr(dm, "chat_complete", boom)
    with pytest.raises(RuntimeError):
        dm.retry_turn()

    # The old reply is still the committed one, so clock and records must match it.
    assert dm.load_history()[-1]["content"] == "First reply."
    assert arc.load_arc(root)["clock"]["minutes"] == 75
    assert arc.load_records(root) == records_before
