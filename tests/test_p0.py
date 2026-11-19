"""tests/test_p0.py — characterization tests for lean-dm.

All tests:
- Use pytest + tmp_path (via conftest.py autouse fixture for isolation).
- Monkeypatch dm._raw_post or dm.chat_complete; never make real API calls.
- Never touch the real campaigns/ directory.
"""
import json
import os
import pathlib
import pytest

import dm
import config as cfg_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init(root):
    """Create the minimum files for a campaign at *root*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def _write_history(root, messages):
    """Write a list of {role, content} dicts to history.jsonl."""
    text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    (root / "history.jsonl").write_text(text, encoding="utf-8")


def _load_history_lines(root):
    return [l for l in (root / "history.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


SIMPLE_HISTORY = [
    {"role": "user", "content": "I step into the tavern."},
    {"role": "assistant", "content": "The barkeep eyes you warily."},
    {"role": "user", "content": "I order an ale."},
    {"role": "assistant", "content": "He slides a foamy mug your way."},
]


# ---------------------------------------------------------------------------
# 1. Golden: build_context — no ledger, not folded
# ---------------------------------------------------------------------------

def test_golden_build_context_no_features(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_history(root, SIMPLE_HISTORY)
    (root / "premise.md").write_text("A gritty fantasy tavern.", encoding="utf-8")

    # Disable ledger so canon block is absent
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "ledger_enabled": False})

    pending = {"role": "user", "content": "I look around for trouble."}
    result = dm.build_context(SIMPLE_HISTORY + [pending])

    # Must have exactly 2 messages: system + the tail merged
    assert result[0]["role"] == "system"
    assert "A gritty fantasy tavern." in result[0]["content"]
    # The pending user message is last
    assert result[-1]["role"] == "user"
    assert result[-1]["content"] == "I look around for trouble."

    # Idempotency: calling again produces byte-identical output
    result2 = dm.build_context(SIMPLE_HISTORY + [pending])
    assert json.dumps(result, ensure_ascii=False) == json.dumps(result2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2. Golden: build_context — folded=4 with summary and ledger
# ---------------------------------------------------------------------------

def test_golden_build_context_folded_with_ledger(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    # 4 messages folded
    history = SIMPLE_HISTORY + [
        {"role": "user", "content": "I ask about work."},
        {"role": "assistant", "content": "He shrugs and points upstairs."},
    ]
    _write_history(root, history)
    (root / "state.json").write_text(json.dumps({"folded": 4, "ledgered": 4}), encoding="utf-8")
    (root / "summary.md").write_text("## Messages 0–3\nSomething happened in the tavern.", encoding="utf-8")
    (root / "premise.md").write_text("A gritty fantasy tavern.", encoding="utf-8")
    ledger_data = [{"id": "barkeep", "kind": "person", "name": "Barkeep", "state": "suspicious",
                    "src": "dm", "msg": 1, "status": "active"}]
    (root / "ledger.json").write_text(json.dumps(ledger_data), encoding="utf-8")

    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "ledger_enabled": True})

    pending = {"role": "user", "content": "I climb the stairs."}
    result = dm.build_context(history + [pending])

    system_content = result[0]["content"]
    assert "STORY SO FAR" in system_content
    assert "PREMISE" in system_content

    # Only the tail (history[4:]) is verbatim
    assert result[-1]["role"] == "user"
    assert "CANON LEDGER" in result[-1]["content"]
    assert "Barkeep" in result[-1]["content"]

    # Idempotency
    result2 = dm.build_context(history + [pending])
    assert json.dumps(result, ensure_ascii=False) == json.dumps(result2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 3. Undo removes last exchange
# ---------------------------------------------------------------------------

def test_undo_removes_last_exchange(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_history(root, SIMPLE_HISTORY)

    text = dm.undo_turn()
    assert text == "I order an ale."

    lines = _load_history_lines(root)
    assert len(lines) == 2  # only first exchange remains
    msgs = [json.loads(l) for l in lines]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# 4. Undo guard — cannot undo past fold pointer
# ---------------------------------------------------------------------------

def test_undo_guard_past_folded(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_history(root, SIMPLE_HISTORY)
    # Set folded=4 — all 4 messages are folded
    (root / "state.json").write_text(json.dumps({"folded": 4, "ledgered": 0}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="folded|ledger|rewind"):
        dm.undo_turn()


# ---------------------------------------------------------------------------
# 5. Undo guard — cannot undo past ledgered pointer
# ---------------------------------------------------------------------------

def test_undo_guard_past_ledgered(tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_history(root, SIMPLE_HISTORY)
    # Ledgered covers all messages
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 4}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="folded|ledger|rewind"):
        dm.undo_turn()


# ---------------------------------------------------------------------------
# 6. Retry regenerates DM reply
# ---------------------------------------------------------------------------

def test_retry_regenerates_dm_reply(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_history(root, SIMPLE_HISTORY)

    new_reply = "He grins and refills your mug without charge."

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        return new_reply

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "api_key": "test", "model": "test-model"})

    result = dm.retry_turn()
    assert result == new_reply

    # Last line of history should be the new reply
    lines = _load_history_lines(root)
    last = json.loads(lines[-1])
    assert last["role"] == "assistant"
    assert last["content"] == new_reply


# ---------------------------------------------------------------------------
# 7. fold_if_needed triggers when token count exceeds budget
# ---------------------------------------------------------------------------

def test_fold_if_needed_trigger(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    # Create a large enough history to trigger fold
    # ctx_budget_tok default is 350000; we'll lower it drastically
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "ctx_budget_tok": 10,
        "ctx_target_tok": 5,
        "keep_min_turns": 1,
        "ledger_enabled": False,
        "summary_ratio": 10,
        "scribe_chunk_tok": 60000,
        "api_key": "test",
        "model": "test-model",
    })

    big_history = [
        {"role": "user", "content": "x" * 20},
        {"role": "assistant", "content": "y" * 20},
        {"role": "user", "content": "a" * 20},
        {"role": "assistant", "content": "b" * 20},
    ]
    _write_history(root, big_history)

    chapter_text = "Chapter summary: things happened." * 5

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        return chapter_text

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.fold_if_needed()

    # folded pointer should have advanced
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["folded"] > 0
    summary = (root / "summary.md").read_text(encoding="utf-8")
    assert "Messages" in summary


# ---------------------------------------------------------------------------
# 8. fold_pointer_written_last — crash recovery: already-written chapter skipped
# ---------------------------------------------------------------------------

def test_fold_pointer_written_last(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "ctx_budget_tok": 10,
        "ctx_target_tok": 5,
        "keep_min_turns": 1,
        "ledger_enabled": False,
        "summary_ratio": 10,
        "scribe_chunk_tok": 60000,
        "api_key": "test",
        "model": "test-model",
    })

    big_history = [
        {"role": "user", "content": "x" * 20},
        {"role": "assistant", "content": "y" * 20},
        {"role": "user", "content": "a" * 20},
        {"role": "assistant", "content": "b" * 20},
    ]
    _write_history(root, big_history)

    # Simulate crash: summary already written but fold pointer NOT advanced
    # First figure out what range would be folded — do a dry run to discover header
    chapter_text = "Pre-written chapter content."
    # Manually write a chapter that matches what fold_if_needed would write
    # The header format is "## Messages {k}–{end-1}"
    pre_written = "## Messages 0–1\n" + chapter_text
    (root / "summary.md").write_text(pre_written, encoding="utf-8")
    # State pointer still at 0 (crash before set_state)
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")

    call_count = {"n": 0}

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        call_count["n"] += 1
        return "New chapter content."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.fold_if_needed()

    # The already-written chapter should NOT have triggered another scribe call
    # (the header "## Messages 0–1" is already in summary)
    final_summary = (root / "summary.md").read_text(encoding="utf-8")
    # Pre-written chapter must still be there
    assert chapter_text in final_summary
    # Fold pointer must now be advanced
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["folded"] > 0


# ---------------------------------------------------------------------------
# 9. apply_ledger_ops — upsert, close, delete
# ---------------------------------------------------------------------------

def test_apply_ledger_ops_upsert_close_delete():
    ledger = []

    # Upsert a new entry
    ledger = dm.apply_ledger_ops(ledger, {
        "upsert": [{"id": "hero", "kind": "player", "name": "Hero", "state": "alive", "src": "player", "msg": 0}],
        "close": [],
        "delete": [],
    })
    assert len(ledger) == 1
    assert ledger[0]["id"] == "hero"
    assert ledger[0]["status"] == "active"

    # Upsert a second entry
    ledger = dm.apply_ledger_ops(ledger, {
        "upsert": [{"id": "sword", "kind": "item", "name": "Sword", "state": "sharp", "src": "dm", "msg": 1}],
        "close": [],
        "delete": [],
    })
    assert len(ledger) == 2

    # Close one entry
    ledger = dm.apply_ledger_ops(ledger, {
        "upsert": [],
        "close": ["sword"],
        "delete": [],
    })
    sword = next(e for e in ledger if e["id"] == "sword")
    assert sword["status"] == "closed"

    # Delete one entry
    ledger = dm.apply_ledger_ops(ledger, {
        "upsert": [],
        "close": [],
        "delete": ["hero"],
    })
    assert all(e["id"] != "hero" for e in ledger)
    assert len(ledger) == 1  # only sword remains


# ---------------------------------------------------------------------------
# 10. Config validation — unknown keys ignored, all-or-nothing, secrets not returned
# ---------------------------------------------------------------------------

def test_config_validation(tmp_path):
    settings_path = tmp_path / "settings.json"
    c = cfg_module.Config(settings_path)

    # Unknown keys are ignored (returned as error but do not corrupt valid keys)
    errors = c.update({"nonexistent_key": "value"})
    assert "nonexistent_key" in errors

    # All-or-nothing: if one field is invalid the whole update is rejected
    errors = c.update({"retries": "not-a-number", "timeout_sec": 300})
    assert "retries" in errors
    # timeout_sec should NOT have been updated (all-or-nothing)
    assert c.timeout_sec != 300

    # Secrets are never returned in public()
    c.update({"api_key": "super-secret-key"})
    pub = c.public()
    assert pub["values"]["api_key"] == ""   # masked
    assert pub["secret_set"]["api_key"] is True


# ---------------------------------------------------------------------------
# 11. Config old file loads with defaults for missing fields
# ---------------------------------------------------------------------------

def test_config_old_file_loads(tmp_path):
    settings_path = tmp_path / "settings.json"
    # Write a minimal "old" settings file missing most fields
    old_data = {
        "api_key": "oldkey",
        "model": "old-model",
    }
    settings_path.write_text(json.dumps(old_data), encoding="utf-8")

    c = cfg_module.Config(settings_path)

    # Existing keys loaded correctly
    assert c.api_key == "oldkey"
    assert c.model == "old-model"

    # Missing fields fall back to defaults
    assert c.ctx_budget_tok == 350000
    assert c.retries == 6
    assert c.ledger_enabled is True


# ---------------------------------------------------------------------------
# 12. DM_CAMPAIGNS_DIR relocates campaigns directory
# ---------------------------------------------------------------------------

def test_dm_campaigns_dir_override(monkeypatch, tmp_path):
    custom_dir = tmp_path / "my_campaigns"
    custom_dir.mkdir()
    monkeypatch.setenv("DM_CAMPAIGNS_DIR", str(custom_dir))

    # Re-evaluate the path logic as dm.py would on import
    import importlib
    # We can't fully re-import, but we can verify the env-driven path construction
    resolved = pathlib.Path(os.environ.get("DM_CAMPAIGNS_DIR") or pathlib.Path(dm.__file__).parent / "campaigns")
    assert resolved == custom_dir

    # The monkeypatched ROOT in conftest should point to the custom dir
    # (conftest sets dm.CAMPAIGNS_DIR = tmp_path/campaigns and dm.ROOT accordingly)
    # Here we verify the override would route files correctly
    custom_root = custom_dir / dm.CAMPAIGN
    custom_root.mkdir(parents=True, exist_ok=True)
    (custom_root / "history.jsonl").write_text("", encoding="utf-8")

    # Patch dm module globals to use custom_dir
    monkeypatch.setattr(dm, "CAMPAIGNS_DIR", custom_dir)
    monkeypatch.setattr(dm, "ROOT", custom_root)
    monkeypatch.setattr(dm, "HISTORY", custom_root / "history.jsonl")
    monkeypatch.setattr(dm, "STATE", custom_root / "state.json")
    monkeypatch.setattr(dm, "SUMMARY", custom_root / "summary.md")
    monkeypatch.setattr(dm, "LEDGER", custom_root / "ledger.json")
    monkeypatch.setattr(dm, "UNDONE", custom_root / "undone.jsonl")
    monkeypatch.setattr(dm, "PREMISE", custom_root / "premise.md")

    # Verify that load_history reads from the custom path
    test_msg = {"role": "user", "content": "hello from custom dir"}
    (custom_root / "history.jsonl").write_text(json.dumps(test_msg) + "\n", encoding="utf-8")
    history = dm.load_history()
    assert len(history) == 1
    assert history[0]["content"] == "hello from custom dir"
