import json
import os
from collections import defaultdict, deque

import dm


def test_zero_style_push_does_not_disable_other_style_controls(monkeypatch):
    dm.init_files()
    monkeypatch.setattr(dm.cfg, "_campaign_vals", {})
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "style_enabled": True,
        "style_preset": "none",
        "style_push": 0,
        "style_words_max": 120,
        "style_notes": "Use short sentences.",
        "ledger_enabled": False,
    })

    system = dm.build_context([{"role": "user", "content": "Go."}])[0]["content"]
    assert "Style push:" not in system
    assert "at most 120 words" in system
    assert "Use short sentences." in system


def test_ledger_delimiter_precedes_guard_and_brief(monkeypatch):
    dm.init_files()
    dm.LEDGER.write_text(json.dumps([
        {"id": "key", "kind": "item", "name": "Key", "state": "lost"}
    ]), encoding="utf-8")
    monkeypatch.setattr(dm.cfg, "_campaign_vals", {})
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals, "style_guard": True, "ledger_enabled": True,
    })
    history = [
        {"role": "user", "content": "Look."},
        {"role": "assistant", "content": "The door opens slowly."},
        {"role": "user", "content": "Enter."},
    ]
    content = dm.build_context(history, {"intent": "act"})[-1]["content"]
    assert content.index("Key: lost") < content.index("end of ledger")
    assert content.index("end of ledger") < content.index("AVOID REPEATING:")
    assert content.index("AVOID REPEATING:") < content.index("DIRECTOR'S BRIEF")
    assert content.endswith("Now narrate the outcome of the player's action above.")


def test_ledger_prompt_keeps_legacy_suffix_without_extra_instructions(monkeypatch):
    dm.init_files()
    dm.LEDGER.write_text(json.dumps([
        {"id": "key", "kind": "item", "name": "Key", "state": "lost"}
    ]), encoding="utf-8")
    monkeypatch.setattr(dm.cfg, "_campaign_vals", {})
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals, "style_guard": False, "ledger_enabled": True,
    })
    content = dm.build_context([{"role": "user", "content": "Look."}])[-1]["content"]
    assert content.endswith("\n\n--- end of ledger: now narrate the outcome of the player's action above.")


def test_unknown_ledger_action_is_non_mutating_and_backups_remain_bounded(monkeypatch):
    dm.init_files()
    monkeypatch.setattr(dm, "POST_COUNTS", defaultdict(deque))
    monkeypatch.setattr(dm, "config_locked", lambda: True)
    original = '[{"id": "key", "state": "lost"}]'
    dm.LEDGER.write_text(original, encoding="utf-8")
    for i in range(11):
        backup = dm.ROOT / f"ledger.{i}.bak.json"
        backup.write_text(str(i), encoding="utf-8")
        os.utime(backup, (1000 + i, 1000 + i))
    before = {p.name: p.read_text() for p in dm.ROOT.glob("ledger.*.bak.json")}

    with dm.app.test_client() as client:
        for body in ({"action": "typo"}, {"action": []}, {}, ["invalid"]):
            response = client.post("/api/ledger", json=body)
            assert response.status_code == 400
            assert response.get_json()["error"] == "unknown action"
        assert dm.LEDGER.read_text(encoding="utf-8") == original
        assert {p.name: p.read_text() for p in dm.ROOT.glob("ledger.*.bak.json")} == before

        monkeypatch.setattr(dm.time, "time", lambda: 2000000000)
        response = client.post("/api/ledger", json={"action": "pin", "id": "key"})
        assert response.status_code == 200

    backups = list(dm.ROOT.glob("ledger.*.bak.json"))
    assert len(backups) == 10
    assert (dm.ROOT / "ledger.2000000000.bak.json").read_text(encoding="utf-8") == original
    assert not (dm.ROOT / "ledger.0.bak.json").exists()
    assert not (dm.ROOT / "ledger.1.bak.json").exists()
