"""Sprint 1 / 01 - the analyst timeout must bound turn latency (D-14)."""
import json
import time

import analyst
import dm


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def test_analyst_timeout_does_not_block_the_turn(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": True,
        "analyst_timeout_sec": 1,
        "api_key": "test",
        "model": "test-model",
        "ledger_enabled": False,
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if any("narrative director" in m.get("content", "") for m in messages):
            time.sleep(3)  # far longer than the 1 s timeout
            return "{}"
        return "DM reply."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    t0 = time.time()
    reply = dm.player_turn("I wait.")
    elapsed = time.time() - t0

    assert reply == "DM reply."
    assert elapsed < 2.0, f"turn took {elapsed:.1f}s: analyst timeout is not bounding latency"
    assert analyst.load_briefs(root)[0]["source"] == "fallback"
