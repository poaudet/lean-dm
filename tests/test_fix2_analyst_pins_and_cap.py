"""FIX2 (C1) - pin markers in the analyst ledger copy, and a total prompt cap.

The narrator-facing render must stay byte-identical: the player never sees a pin marker.
"""
import json

import analyst
import ledger


LEDGER = [
    {"id": "witness", "kind": "person", "name": "Mira", "state": "hidden in the cellar",
     "status": "active", "pinned": True},
    {"id": "key", "kind": "item", "name": "Iron Key", "state": "lost", "status": "active"},
    {"id": "cult", "kind": "person", "name": "The Cult", "state": "hunting", "status": "active"},
    {"id": "old", "kind": "thread", "name": "Old Thread", "state": "done",
     "status": "closed", "msg": 3},
]

BRIEF = {"intent": "act", "assessment": "a", "outcome": "clean", "plot_moves": [],
         "keep_true": [], "narration": {"words": 100, "push": 0.5, "end": "decision"},
         "clarify": None, "act_complete": False}


def _capture(**kwargs):
    seen = {}

    def chat(messages, model=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps(BRIEF)

    analyst.run_analyst(None, [], "I move.", [], [], chat, **kwargs)
    return seen["prompt"]


# ---------------------------------------------------------------------------
# C1 - pin markers, analyst copy only
# ---------------------------------------------------------------------------

def test_pinned_entry_is_marked_in_the_analyst_copy():
    assert "- Mira: hidden in the cellar [pinned]" in analyst.analyst_ledger_text(LEDGER)


def test_unpinned_entries_are_not_marked():
    text = analyst.analyst_ledger_text(LEDGER)
    assert "- Iron Key: lost" in text
    assert "- Iron Key: lost [pinned]" not in text


def test_narrator_render_has_no_pin_marker():
    """The narrator-facing render must stay byte-identical, so golden tests keep passing."""
    assert "[pinned]" not in ledger.render_ledger(LEDGER)


def test_narrator_render_is_unchanged():
    assert ledger.render_ledger(LEDGER) == "\n".join([
        "[PERSON]", "- Mira: hidden in the cellar", "- The Cult: hunting",
        "[ITEM]", "- Iron Key: lost",
        "[PAST / RESOLVED \u2014 do not revive]", "- Old Thread: done",
    ])


def test_analyst_copy_keeps_the_narrator_grouping():
    a = [l for l in analyst.analyst_ledger_text(LEDGER).splitlines() if l.startswith("[")]
    b = [l for l in ledger.render_ledger(LEDGER).splitlines() if l.startswith("[")]
    assert a == b


def test_empty_and_malformed_ledgers_are_safe():
    assert analyst.analyst_ledger_text([]) == ""
    assert analyst.analyst_ledger_text(None) == ""
    assert analyst.analyst_ledger_text("nonsense") == ""


def test_pin_marker_reaches_the_analyst_prompt():
    prompt = _capture(ledger=LEDGER)
    assert "[pinned]" in prompt
    assert "Mira: hidden in the cellar" in prompt


# ---------------------------------------------------------------------------
# C1 - total prompt cap, oldest-first
# ---------------------------------------------------------------------------

def test_oversized_premise_and_summary_stay_under_the_cap():
    premise = "PREMISE_OLDEST_MARKER " + ("p" * 200000) + " PREMISE_NEWEST_MARKER"
    summary = "SUMMARY_OLDEST_MARKER " + ("s" * 200000) + " SUMMARY_NEWEST_MARKER"
    prompt = _capture(premise=premise, summary=summary, ledger=LEDGER)
    cap_chars = analyst.ANALYST_PROMPT_TOK * 4.0
    assert len(prompt) <= cap_chars * 1.05


def test_the_newest_text_survives_the_cap():
    premise = "PREMISE_OLDEST_MARKER " + ("p" * 200000) + " PREMISE_NEWEST_MARKER"
    summary = "SUMMARY_OLDEST_MARKER " + ("s" * 200000) + " SUMMARY_NEWEST_MARKER"
    prompt = _capture(premise=premise, summary=summary, ledger=LEDGER)
    assert "PREMISE_NEWEST_MARKER" in prompt
    assert "SUMMARY_NEWEST_MARKER" in prompt


def test_the_oldest_text_is_cut_first():
    premise = "PREMISE_OLDEST_MARKER " + ("p" * 200000) + " PREMISE_NEWEST_MARKER"
    summary = "SUMMARY_OLDEST_MARKER " + ("s" * 200000) + " SUMMARY_NEWEST_MARKER"
    prompt = _capture(premise=premise, summary=summary, ledger=LEDGER)
    assert "PREMISE_OLDEST_MARKER" not in prompt
    assert "SUMMARY_OLDEST_MARKER" not in prompt


def test_the_ledger_and_action_survive_the_cap():
    premise = "PREMISE_OLDEST_MARKER " + ("p" * 200000)
    prompt = _capture(premise=premise, ledger=LEDGER)
    assert "Mira: hidden in the cellar" in prompt
    assert "I move." in prompt


def test_a_normal_prompt_is_left_alone():
    prompt = _capture(
        premise="A rain-soaked port town.",
        summary="## Messages 0-3\nMira went into hiding.",
        hooks=["A ledger with a torn page"],
        previous_brief={"intent": "act", "assessment": "prior"},
        ledger=LEDGER,
    )
    assert "A rain-soaked port town." in prompt
    assert "Mira went into hiding." in prompt
    assert "A ledger with a torn page" in prompt
    assert '"assessment": "prior"' in prompt


def test_a_truncated_previous_brief_is_dropped_whole():
    """Half a JSON object would be unparseable, so it is dropped rather than cut."""
    prompt = _capture(
        premise="PREMISE_OLDEST_MARKER " + ("p" * 200000),
        previous_brief={"intent": "act", "assessment": "x" * 300000, "outcome": "clean"},
        ledger=LEDGER,
    )
    if "PREVIOUS BRIEF:" in prompt:
        frag = prompt.split("PREVIOUS BRIEF: ", 1)[1].split("\nCANON LEDGER", 1)[0]
        assert frag.rstrip().endswith("}")
