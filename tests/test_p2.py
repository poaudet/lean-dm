"""tests/test_p2.py — Analyst brief + Notes routing tests."""
import json
import pytest
import dm
import analyst


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


SAMPLE_BRIEF_JSON = json.dumps({
    "intent": "act",
    "assessment": "The player searches the chest.",
    "outcome": "partial",
    "plot_moves": [],
    "keep_true": ["The tavern door is locked"],
    "narration": {"words": 130, "push": 0.7, "end": "decision"},
    "clarify": None,
    "act_complete": False,
})


# ---------------------------------------------------------------------------
# 1. Analyst called before narrator
# ---------------------------------------------------------------------------

def test_analyst_called_before_narrator(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "analyst_model": "test-analyst",
        "ledger_enabled": False,
    })

    call_order = []

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-analyst":
            call_order.append("analyst")
            return SAMPLE_BRIEF_JSON
        call_order.append("narrator")
        return "You open the chest and find an iron key."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("I open the chest.")

    assert call_order == ["analyst", "narrator"]


# ---------------------------------------------------------------------------
# 2. Brief appears in the last user message of build_context
# ---------------------------------------------------------------------------

def test_analyst_brief_in_last_message():
    brief = {
        "intent": "act",
        "assessment": "Search test",
        "outcome": "clean",
        "plot_moves": [],
        "keep_true": [],
        "narration": {"words": 120, "push": 0.5, "end": "beat"},
        "clarify": None,
        "act_complete": False,
    }
    history = [{"role": "user", "content": "I look around."}]
    ctx = dm.build_context(history, brief=brief)
    last_msg = ctx[-1]
    assert last_msg["role"] == "user"
    assert "DIRECTOR'S BRIEF" in last_msg["content"]
    assert "Search test" in last_msg["content"]


# ---------------------------------------------------------------------------
# 3. Fallbacks: timeout, bad JSON, empty output
# ---------------------------------------------------------------------------

def test_analyst_fallback_on_bad_json(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": True,
        "api_key": "test",
        "model": "test-model",
        "ledger_enabled": False,
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if any("narrative director" in m.get("content", "") for m in messages):
            return "This is not JSON at all! Just random prose."
        return "The DM narrates despite analyst failure."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    reply = dm.player_turn("I do something risky.")
    assert reply == "The DM narrates despite analyst failure."

    # Verify fallback was recorded
    briefs = analyst.load_briefs(root)
    assert 0 in briefs
    assert briefs[0]["source"] == "fallback"


def test_analyst_fallback_on_timeout(monkeypatch, tmp_path):
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
            import time
            time.sleep(1.5)  # exceeds timeout
            return SAMPLE_BRIEF_JSON
        return "DM reply after analyst timeout."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    reply = dm.player_turn("I wait for timeout.")
    assert reply == "DM reply after analyst timeout."

    briefs = analyst.load_briefs(root)
    assert briefs[0]["source"] == "fallback"


# ---------------------------------------------------------------------------
# 4. briefs.jsonl written and history stays clean (I2, I3)
# ---------------------------------------------------------------------------

def test_briefs_jsonl_written_and_history_clean(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "analyst_model": "test-analyst",
        "ledger_enabled": False,
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-analyst":
            return SAMPLE_BRIEF_JSON
        return "Narrator text."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("First player move.")

    # Check briefs.jsonl
    briefs = analyst.load_briefs(root)
    assert len(briefs) == 1
    assert briefs[0]["brief"]["outcome"] == "partial"

    # Check history.jsonl: lines must strictly have ONLY role and content
    for line in (root / "history.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        assert set(d.keys()) == {"role", "content"}
        assert "DIRECTOR'S BRIEF" not in d["content"]


# ---------------------------------------------------------------------------
# 5. OOC notes never enter history (I2) & saved to notes.jsonl
# ---------------------------------------------------------------------------

def test_ooc_note_not_in_history(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "notes_routing": True,
        "api_key": "test",
        "model": "test-model",
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        return "Sure, that character's name is Lord Roderick."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    # Send note with // prefix
    resp = dm.player_turn("// what was the lord's name again?")
    assert "Lord Roderick" in resp

    # History must be completely empty!
    history = dm.load_history()
    assert len(history) == 0

    # notes.jsonl must have the note
    notes = analyst.load_notes(root)
    assert len(notes) == 1
    assert notes[0]["q"] == "// what was the lord's name again?"
    assert notes[0]["a"] == "Sure, that character's name is Lord Roderick."
    assert notes[0]["canon"] is False


# ---------------------------------------------------------------------------
# 6. Canon promotion applies to ledger or premise
# ---------------------------------------------------------------------------

def test_ooc_canon_applies_ledger(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "notes_routing": True,
        "api_key": "test",
        "model": "test-model",
    })

    ooc_reply = (
        "Got it, the tavern is called The Broken Wheel.\n"
        '```json\n{"canon_proposal": {"id": "the-broken-wheel", "kind": "place", "name": "The Broken Wheel", "state": "tavern in the slums"}}\n```'
    )

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        return ooc_reply

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("ooc: what is this tavern called?")
    notes = analyst.load_notes(root)
    note_id = notes[0]["id"]
    assert notes[0]["proposal"]["id"] == "the-broken-wheel"

    # Call the canon promotion endpoint using Flask test client
    with dm.app.test_client() as client:
        res = client.post(f"/api/notes/{note_id}/canon")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True

    # Ledger must now have the entry!
    ledger = dm.load_ledger()
    wheel = next((e for e in ledger if e["id"] == "the-broken-wheel"), None)
    assert wheel is not None
    assert wheel["name"] == "The Broken Wheel"


# ---------------------------------------------------------------------------
# 7. Golden: Analyst disabled produces byte-identical context to v3 (I11)
# ---------------------------------------------------------------------------

def test_golden_analyst_off_no_change(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": False,
        "style_enabled": False,
        "style_guard": False,
        "ledger_enabled": False,
    })

    history = [
        {"role": "user", "content": "Action 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Action 2"},
    ]

    ctx = dm.build_context(history)
    assert "DIRECTOR'S BRIEF" not in ctx[-1]["content"]


# ---------------------------------------------------------------------------
# 8. Retry reuses stored brief, retry! re-runs analyst (D-11)
# ---------------------------------------------------------------------------

def test_retry_reuses_brief_and_retry_bang_reruns(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "analyst_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "analyst_model": "test-analyst",
        "ledger_enabled": False,
    })

    analyst_calls = []

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-analyst":
            analyst_calls.append(len(analyst_calls) + 1)
            return json.dumps({
                "intent": "act",
                "assessment": f"Call #{len(analyst_calls)}",
                "outcome": "clean",
                "plot_moves": [],
                "keep_true": [],
                "narration": {"words": 100, "push": 0.5, "end": "decision"},
                "clarify": None,
                "act_complete": False,
            })
        return "Narrator reply."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("Action.")
    assert len(analyst_calls) == 1

    # Standard /retry should NOT re-run analyst (D-11)
    dm.retry_turn(rerun_analyst=False)
    assert len(analyst_calls) == 1  # Reused stored brief!

    # retry! (rerun_analyst=True) should re-run analyst (D-11)
    dm.retry_turn(rerun_analyst=True)
    assert len(analyst_calls) == 2
