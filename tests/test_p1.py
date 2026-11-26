"""tests/test_p1.py — Settings extensions + Writing style + Style guard tests."""
import json
import pytest
import dm
import config as cfg_module


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. New config fields have sensible defaults
# ---------------------------------------------------------------------------

def test_new_config_fields_have_defaults(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    c = cfg_module.Config(settings_file)

    assert c.difficulty == "standard"
    assert c.style_enabled is False
    assert c.style_preset == "none"
    assert c.style_words_min == 0
    assert c.style_words_max == 0
    assert c.style_push == 60
    assert c.style_notes == ""
    assert c.style_guard is False
    assert c.analyst_enabled is False
    assert c.arc_enabled is False
    # D2: notes routing is on by default for every campaign.
    assert c.notes_routing is True
    assert c.analyst_model == ""
    assert c.planner_model == ""
    assert c.analyst_timeout_sec == 25
    assert c.analyst_context_turns == 10
    assert c.web_password == ""


def test_obsolete_record_wait_setting_is_ignored_on_save(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"record_wait_sec": 9, "model": "existing"}), encoding="utf-8")
    c = cfg_module.Config(settings_file)
    assert c.model == "existing"
    assert "record_wait_sec" not in c.public()["values"]
    c.save()
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["model"] == "existing"
    assert "record_wait_sec" not in saved


# ---------------------------------------------------------------------------
# 2. Select type coercion
# ---------------------------------------------------------------------------

def test_select_type_coercion(tmp_path):
    settings_file = tmp_path / "settings.json"
    c = cfg_module.Config(settings_file)

    # Valid select value
    errs = c.update({"difficulty": "hard"})
    assert not errs
    assert c.difficulty == "hard"

    # Invalid select value
    errs = c.update({"difficulty": "impossible"})
    assert "difficulty" in errs
    assert "must be one of" in errs["difficulty"]


# ---------------------------------------------------------------------------
# 3. Campaign scope overlay
# ---------------------------------------------------------------------------

def test_campaign_scope_overlay(tmp_path):
    settings_file = tmp_path / "settings.json"
    c = cfg_module.Config(settings_file)
    c.update({"difficulty": "standard"})

    camp_dir = tmp_path / "campaigns" / "test_camp"
    camp_dir.mkdir(parents=True)
    camp_json = camp_dir / "campaign.json"
    camp_json.write_text(json.dumps({"difficulty": "hard"}), encoding="utf-8")

    c.set_campaign_path(camp_json)
    # The campaign override takes precedence
    assert c.difficulty == "hard"
    assert c._vals["difficulty"] == "standard"

    # Updating campaign scope writes to campaign.json
    c.update({"difficulty": "story"}, scope="campaign")
    assert c.difficulty == "story"
    saved = json.loads(camp_json.read_text(encoding="utf-8"))
    assert saved["difficulty"] == "story"

    # Cannot update global setting at campaign scope
    errs = c.update({"model": "some-model"}, scope="campaign")
    assert "model" in errs


# ---------------------------------------------------------------------------
# 4. Style block added when enabled
# ---------------------------------------------------------------------------

def test_style_block_added_when_enabled(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "premise.md").write_text("Premise text.", encoding="utf-8")

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "style_enabled": True,
        "style_preset": "noir",
        "style_words_min": 100,
        "style_words_max": 200,
        "style_push": 80,
        "style_notes": "Use cynical metaphors.",
        "ledger_enabled": False,
    })

    ctx = dm.build_context([{"role": "user", "content": "I walk down the dark alley."}])
    system_msg = ctx[0]["content"]

    assert "STYLE:" in system_msg
    assert "Noir Style Preset" in system_msg
    assert "Target length: 100–200 words" in system_msg
    assert "Style push: 80/100" in system_msg
    assert "Use cynical metaphors" in system_msg


# ---------------------------------------------------------------------------
# 5. Golden: No style change when style_enabled is False (I11)
# ---------------------------------------------------------------------------

def test_golden_no_style_no_change(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "premise.md").write_text("Premise text.", encoding="utf-8")

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "style_enabled": False,
        "style_guard": False,
        "ledger_enabled": False,
    })

    history = [
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "Greetings traveler."},
        {"role": "user", "content": "Who goes there?"},
    ]

    ctx = dm.build_context(history)
    system_msg = ctx[0]["content"]

    assert "STYLE:" not in system_msg
    assert "AVOID REPEATING:" not in ctx[-1]["content"]


# ---------------------------------------------------------------------------
# 6. Style guard is deterministic
# ---------------------------------------------------------------------------

def test_style_guard_deterministic():
    history = [
        {"role": "user", "content": "Move 1"},
        {"role": "assistant", "content": "The shadows stretch across the stone floor of the hall."},
        {"role": "user", "content": "Move 2"},
        {"role": "assistant", "content": "The shadows stretch across the room as evening falls quietly."},
        {"role": "user", "content": "Move 3"},
        {"role": "assistant", "content": "The shadows stretch across the wall while candles flicker softly."},
    ]

    g1 = dm.compute_style_guard(history)
    g2 = dm.compute_style_guard(history)
    assert g1 == g2


# ---------------------------------------------------------------------------
# 7. Style guard openers: 5-word openers from last 8 DM replies
# ---------------------------------------------------------------------------

def test_style_guard_openers():
    history = []
    for i in range(10):
        history.append({"role": "user", "content": f"Turn {i}"})
        history.append({"role": "assistant", "content": f"Opener number {i} continues here with more detail."})

    guard = dm.compute_style_guard(history)
    # Must capture openers from the last 8 replies (i=2..9)
    assert "Opener number 9 continues here" in guard
    assert "Opener number 8 continues here" in guard
    assert "Opener number 0 continues here" not in guard


# ---------------------------------------------------------------------------
# 8. Style guard trigrams threshold: >= 3 occurrences
# ---------------------------------------------------------------------------

def test_style_guard_trigrams_threshold():
    history = [
        {"role": "assistant", "content": "First response: the cold wind blows."},
        {"role": "assistant", "content": "Second response: the cold wind howling."},
        {"role": "assistant", "content": "Third response: the cold wind dies."},
    ]
    guard = dm.compute_style_guard(history)
    assert "the cold wind" in guard


# ---------------------------------------------------------------------------
# 9. Overlong reply tracking
# ---------------------------------------------------------------------------

def test_overlong_flag_set(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "style_words_max": 20,
        "api_key": "test",
        "model": "test-model",
    })

    long_reply = "word " * 35  # 35 words > 1.5 * 20 = 30 words

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        return long_reply

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("Test action")

    st = dm.game_state()
    assert st["overlong"] is True
    assert st["last_words"] >= 35
