"""WP3 - analyst inputs, ledger-backed keep_true, retry, built fallback."""
import json

import analyst
import dm


LEDGER = [
    {"id": "witness", "kind": "person", "name": "Mira", "state": "hidden in the cellar",
     "status": "active", "pinned": True},
    {"id": "key", "kind": "item", "name": "Iron Key", "state": "lost", "status": "active"},
    {"id": "cult", "kind": "person", "name": "The Cult", "state": "hunting", "status": "active"},
]

BRIEF = {
    "intent": "act", "assessment": "a", "outcome": "clean", "plot_moves": [],
    "keep_true": [], "narration": {"words": 100, "push": 0.5, "end": "decision"},
    "clarify": None, "act_complete": False,
}


class Retryable(Exception):
    """Stands in for dm.ApiError with retry=True (429/5xx)."""
    retry = True


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text(json.dumps(LEDGER), encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-narrator", "scribe_model": "test-scribe",
        "analyst_model": "test-analyst", "analyst_enabled": True,
        "ledger_enabled": True, "arc_enabled": False, "steering_enabled": False,
        **over,
    })


# ---------------------------------------------------------------------------
# C2 - keep_true comes from the ledger
# ---------------------------------------------------------------------------

def test_unknown_ledger_id_in_keep_true_is_dropped():
    out = analyst.clamp_brief({"keep_true": [{"id": "ghost", "why": "not real"}]}, ledger=LEDGER)
    assert out["keep_true"] == []


def test_known_ledger_id_expands_to_name_and_state():
    out = analyst.clamp_brief({"keep_true": [{"id": "key", "why": "the player holds it"}]}, ledger=LEDGER)
    assert out["keep_true"] == ["Iron Key: lost"]


def test_pinned_entry_named_in_the_action_is_always_present():
    out = analyst.clamp_brief({"keep_true": []}, ledger=LEDGER,
                              player_action="I ask Mira where the cult went.")
    assert "Mira: hidden in the cellar" in out["keep_true"]


def test_pinned_entry_named_in_recent_play_is_present():
    out = analyst.clamp_brief({"keep_true": []}, ledger=LEDGER,
                              recent_text="DM: Mira refuses to speak.")
    assert "Mira: hidden in the cellar" in out["keep_true"]


def test_unpinned_entry_is_not_forced_in():
    out = analyst.clamp_brief({"keep_true": []}, ledger=LEDGER, player_action="I look at the Iron Key.")
    assert out["keep_true"] == []


def test_free_text_keep_true_is_capped_at_three():
    raw = {"keep_true": ["one", "two", "three", "four", "five"]}
    out = analyst.clamp_brief(raw, ledger=LEDGER)
    assert out["keep_true"] == ["one", "two", "three"]


def test_keep_true_is_capped_at_eight():
    ledger = [{"id": f"e{i}", "name": f"N{i}", "state": "s", "status": "active"} for i in range(12)]
    raw = {"keep_true": [{"id": f"e{i}", "why": "w"} for i in range(12)]}
    out = analyst.clamp_brief(raw, ledger=ledger)
    assert len(out["keep_true"]) == 8


def test_pinned_entries_come_first():
    out = analyst.clamp_brief({"keep_true": [{"id": "key", "why": "w"}]}, ledger=LEDGER,
                              player_action="Mira waits.")
    assert out["keep_true"][0] == "Mira: hidden in the cellar"
    assert "Iron Key: lost" in out["keep_true"]


# ---------------------------------------------------------------------------
# C3 - the built fallback
# ---------------------------------------------------------------------------

def test_fallback_brief_builds_keep_true_from_pinned():
    brief = analyst.make_fallback_brief("timeout", ledger=LEDGER, player_action="Mira is here.")
    assert "Mira: hidden in the cellar" in brief["keep_true"]
    assert brief.diagnostics == {"fallback_reason": "timeout"}


def test_fallback_brief_plays_due_moves_and_keeps_keep_true():
    sched = [{"id": "beat.a1b1", "how": "overt", "note": "A torn page"}]
    brief = analyst.make_fallback_brief("timeout", allowed_moves=sched, ledger=LEDGER,
                                        player_action="Mira is here.")
    assert [m["id"] for m in brief["plot_moves"]] == ["beat.a1b1"]
    assert "Mira: hidden in the cellar" in brief["keep_true"]


# ---------------------------------------------------------------------------
# C4 - one retry inside the timeout budget
# ---------------------------------------------------------------------------

def test_429_once_then_success_yields_a_normal_brief(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_retries=1)
    calls = []

    def chat(messages, model=None, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise Retryable("HTTP 429")
        return json.dumps(BRIEF)

    brief, source, model, ms = analyst.run_analyst(
        tmp_path, [], "I move.", [], [], chat, ledger=LEDGER)
    assert source == "analyst"
    assert len(calls) == 2
    assert brief["intent"] == "act"


def test_two_429s_yield_the_built_fallback(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_retries=1)
    calls = []

    def chat(messages, model=None, **kw):
        calls.append(1)
        raise Retryable("HTTP 503")

    brief, source, model, ms = analyst.run_analyst(
        tmp_path, [], "I move.", [], [], chat, ledger=LEDGER)
    assert source == "fallback"
    assert len(calls) == 2
    assert brief.diagnostics["fallback_reason"] == "analyst_error"


def test_non_retryable_error_does_not_retry(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_retries=1)
    calls = []

    def chat(messages, model=None, **kw):
        calls.append(1)
        raise RuntimeError("private response")

    brief, source, model, ms = analyst.run_analyst(tmp_path, [], "I move.", [], [], chat)
    assert source == "fallback"
    assert len(calls) == 1
    assert brief.diagnostics["fallback_reason"] == "analyst_error"


def test_zero_retries_means_one_attempt(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_retries=0)
    calls = []

    def chat(messages, model=None, **kw):
        calls.append(1)
        raise Retryable("HTTP 429")

    analyst.run_analyst(tmp_path, [], "I move.", [], [], chat)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# C1 - the analyst sees the campaign state
# ---------------------------------------------------------------------------

def test_ledger_and_steering_appear_in_the_analyst_prompt(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    seen = {}

    def chat(messages, model=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps(BRIEF)

    analyst.run_analyst(
        tmp_path, [], "I move.", [], [], chat,
        ledger_text="[PERSON]\n- Mira: hidden in the cellar",
        steering_text="Objective: Find the witness",
        ledger=LEDGER,
    )
    assert "Mira: hidden in the cellar" in seen["prompt"]
    assert "Objective: Find the witness" in seen["prompt"]


def test_premise_summary_hooks_and_previous_brief_reach_the_prompt(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    seen = {}

    def chat(messages, model=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps(BRIEF)

    analyst.run_analyst(
        tmp_path, [], "I move.", [], [], chat,
        premise="A rain-soaked port town.",
        summary="## Messages 0-3\nMira went into hiding.",
        hooks=["A ledger with a torn page"],
        previous_brief={"intent": "act", "assessment": "prior"},
        ledger=LEDGER,
    )
    prompt = seen["prompt"]
    assert "A rain-soaked port town." in prompt
    assert "Mira went into hiding." in prompt
    assert "A ledger with a torn page" in prompt
    assert "prior" in prompt


def test_summary_is_capped_oldest_first(monkeypatch, tmp_path):
    _cfg(monkeypatch, analyst_summary_tok=200)
    seen = {}

    def chat(messages, model=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps(BRIEF)

    summary = "OLDEST_MARKER " + ("x" * 5000) + " NEWEST_MARKER"
    analyst.run_analyst(tmp_path, [], "I move.", [], [], chat, summary=summary)
    assert "NEWEST_MARKER" in seen["prompt"]
    assert "OLDEST_MARKER" not in seen["prompt"]


# ---------------------------------------------------------------------------
# C1 - the retry path passes the same inputs as a normal turn
# ---------------------------------------------------------------------------

def test_retry_brief_passes_ledger_and_act_goal(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _cfg(monkeypatch, arc_enabled=True)
    dm.arc.save_arc(root, {
        "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
        "clock": {"minutes": 0},
        "acts": [{"n": 1, "status": "active", "goal": "Find the witness",
                  "beats": [], "outcomes": []}],
        "pressures": [],
    })
    (root / "history.jsonl").write_text(
        json.dumps({"role": "user", "content": "I search."}) + "\n" +
        json.dumps({"role": "assistant", "content": "You find a door."}) + "\n",
        encoding="utf-8")

    captured = {}

    def fake_run_analyst(*args, **kwargs):
        captured.update(kwargs)
        return dict(BRIEF), "analyst", "test-analyst", 1

    monkeypatch.setattr(dm.analyst, "run_analyst", fake_run_analyst)
    dm._retry_brief(dm.load_history(), rerun_analyst=True)

    assert captured.get("ledger_text")
    assert captured.get("act_goal") == "Find the witness"
    assert captured.get("ledger") is not None
