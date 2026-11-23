"""WP4 - narrator contract: reply shape, field meanings, word budget, post-check."""
import json

import dm
import narrator_context


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-narrator", "ledger_enabled": False,
        "style_enabled": False, "style_guard": False, "style_words_max": 0,
        "analyst_enabled": False, "arc_enabled": False, "steering_enabled": False,
        **over,
    })


def _brief(**narr):
    base = {"words": 120, "push": 0.5, "end": "decision"}
    base.update(narr)
    return {"intent": "act", "assessment": "a", "outcome": "clean", "plot_moves": [],
            "keep_true": [], "narration": base, "clarify": None, "act_complete": False}


def test_reply_shape_rule_is_in_the_system_prompt(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    system = dm.build_context([{"role": "user", "content": "Go."}])[0]["content"]
    assert "OUTPUT: Three parts" in system
    assert "closing line stating the live decision" in system


def test_field_definitions_appear_with_style_off(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    content = dm.build_context([{"role": "user", "content": "Go."}], _brief())[-1]["content"]
    assert "What the brief fields mean for this reply:" in content
    assert "end=decision" in content
    assert "push=0.5" in content


def test_end_question_and_beat_have_distinct_meanings(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    q = dm.build_context([{"role": "user", "content": "Go."}], _brief(end="question"))[-1]["content"]
    b = dm.build_context([{"role": "user", "content": "Go."}], _brief(end="beat"))[-1]["content"]
    assert "direct question from an NPC or the world" in q
    assert "with the decision implied" in b


def test_push_states_the_atmosphere_percentage(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    content = dm.build_context([{"role": "user", "content": "Go."}], _brief(push=0.75))[-1]["content"]
    assert "at most 25 percent" in content


def test_clarify_is_defined_when_present(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    brief = _brief()
    brief["clarify"] = "Do you mean the cellar door?"
    content = dm.build_context([{"role": "user", "content": "Go."}], brief)[-1]["content"]
    assert "advance nothing" in content
    assert "Do you mean the cellar door?" in content


def test_ask_intent_is_defined(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    brief = _brief()
    brief["intent"] = "ask"
    content = dm.build_context([{"role": "user", "content": "Go."}], brief)[-1]["content"]
    assert "at most 80 words" in content


def test_default_system_states_no_word_count(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    system = dm.build_context([{"role": "user", "content": "Go."}])[0]["content"]
    assert "120-220" not in system
    assert "120–220" not in system


def test_exactly_one_word_target_with_a_brief(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, style_enabled=True, style_words_min=100, style_words_max=200)
    system = dm.build_context([{"role": "user", "content": "Go."}], _brief(words=95))[0]["content"]
    assert system.count("Word target:") == 1
    assert "Word target: 95 words" in system
    assert "Target length:" not in system


def test_word_target_falls_back_to_style_max_without_a_brief(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, style_words_max=180)
    assert narrator_context.word_target(None) == 180


def test_word_target_default_when_nothing_configured(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    assert narrator_context.word_target(None) == narrator_context.DEFAULT_WORDS


def test_shorter_words_reduces_by_the_ratio():
    assert narrator_context.shorter_words(100) == 70
    assert narrator_context.shorter_words(50) == 50


def test_shape_ok_false_for_a_question_end_without_a_question_mark():
    assert dm.compute_shape_ok("The door opens. You step through.", "question") is False


def test_shape_ok_true_for_a_question_end_with_a_question_mark():
    assert dm.compute_shape_ok("The door opens. Do you step through?", "question") is True


def test_shape_ok_true_for_a_decision_end_with_a_closing_line():
    assert dm.compute_shape_ok("The door opens. The hall waits on your choice.", "decision") is True


def test_shape_ok_false_for_an_empty_reply():
    assert dm.compute_shape_ok("", "decision") is False
    assert dm.compute_shape_ok("   ", "decision") is False


def test_overlong_uses_the_brief_budget_not_style_words_max(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, style_words_max=0, overlong_ratio=1.5)
    monkeypatch.setattr(dm, "chat_complete", lambda *a, **k: "word " * 200)
    dm.player_turn("Go.")
    st = dm.game_state()
    assert st["overlong"] is True
    assert st["word_budget"] == 120


def test_not_overlong_below_the_ratio(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, overlong_ratio=1.5)
    monkeypatch.setattr(dm, "chat_complete", lambda *a, **k: "word " * 100)
    dm.player_turn("Go.")
    assert dm.game_state()["overlong"] is False


def test_shape_ok_is_recorded_in_state(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    monkeypatch.setattr(dm, "chat_complete", lambda *a, **k: "No question here.")
    dm.player_turn("Go.")
    assert dm.game_state()["shape_ok"] is False


def test_retry_shorter_uses_the_reduced_budget(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, analyst_enabled=True, analyst_model="test-analyst")

    def chat(messages, model=None, **kw):
        if model == "test-analyst":
            return json.dumps(_brief(words=100))
        return "A reply."

    monkeypatch.setattr(dm, "chat_complete", chat)
    dm.player_turn("Go.")
    dm.retry_turn(shorter=True)
    assert dm.game_state()["word_budget"] == 70


def test_player_knowledge_and_canonical_name_rules_are_present(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch)
    system = dm.build_context([{"role": "user", "content": "Go."}])[0]["content"]
    assert "canonical names" in system
    assert "already been shown" in system