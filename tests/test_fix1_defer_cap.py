"""FIX1 (B4) - the defer cap is enforced in play, not just inside clamp_brief.

`clamp_brief` already forced a move back in after MAX_DEFERS, but nothing ever
passed `defer_counts`, so the count was always None and a move could be deferred
forever. These tests cover the counter and the wiring.
"""
import json

import analyst
import dm


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


def _arc():
    return {
        "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
        "clock": {"minutes": 0},
        "acts": [{"n": 1, "status": "active", "goal": "g",
                  "beats": [{"id": "a1b1", "text": "SECRET_BEAT_TEXT",
                             "hook": "A ledger with a torn page", "status": "pending"}],
                  "outcomes": []}],
        "pressures": [],
    }


def _brief(*deferred_ids):
    return {"brief": {"deferred": [{"id": i, "reason": "mid-scene"} for i in deferred_ids]}}


def _write_briefs(root, entries):
    """Write briefs.jsonl the way save_brief does: {msg, source, model, ms, brief}."""
    (root / "briefs.jsonl").write_text(
        "".join(json.dumps({"msg": m, "source": "analyst", "model": "m", "ms": 1,
                            "brief": e["brief"]}) + "\n"
                for m, e in entries.items()),
        encoding="utf-8")


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-narrator", "scribe_model": "test-scribe",
        "analyst_model": "test-analyst", "analyst_enabled": True,
        "ledger_enabled": False, "arc_enabled": True, "steering_enabled": False,
        **over,
    })


# ---------------------------------------------------------------------------
# the counter
# ---------------------------------------------------------------------------

def test_no_briefs_means_no_counts(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    assert dm._defer_counts() == {}


def test_a_single_defer_counts_once(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_briefs(root, {0: _brief("beat.a1b1")})
    assert dm._defer_counts() == {"beat.a1b1": 1}


def test_two_consecutive_defers_count_twice(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_briefs(root, {0: _brief("beat.a1b1"), 2: _brief("beat.a1b1")})
    assert dm._defer_counts() == {"beat.a1b1": 2}


def test_a_brief_that_does_not_defer_ends_the_streak(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_briefs(root, {0: _brief("beat.a1b1"), 2: _brief("beat.a1b1"), 4: _brief()})
    assert dm._defer_counts() == {}


def test_only_the_newest_run_counts(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    _write_briefs(root, {0: _brief("a"), 2: _brief("a"), 4: _brief(), 6: _brief("a")})
    assert dm._defer_counts() == {"a": 1}


def test_malformed_briefs_are_tolerated(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    (root / "briefs.jsonl").write_text(
        json.dumps({"msg": 0, "brief": None}) + "\n" +
        json.dumps({"msg": 2, "brief": {"deferred": ["junk", {"id": ""}]}}) + "\n",
        encoding="utf-8")
    assert dm._defer_counts() == {}


# ---------------------------------------------------------------------------
# the wiring - the accept criterion
# ---------------------------------------------------------------------------

def test_run_turn_passes_defer_counts_to_the_analyst(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, _arc())
    _cfg(monkeypatch)
    _write_briefs(root, {0: _brief("beat.a1b1"), 2: _brief("beat.a1b1")})

    captured = {}

    def fake_run_analyst(*args, **kwargs):
        captured.update(kwargs)
        return dict(analyst.BRIEF_SCHEMA), "analyst", "test-analyst", 1

    monkeypatch.setattr(dm.analyst, "run_analyst", fake_run_analyst)
    monkeypatch.setattr(dm, "chat_complete", lambda *a, **k: "Narration.")

    dm.player_turn("I search the alley.")

    assert captured.get("defer_counts") == {"beat.a1b1": 2}


def test_two_stored_defers_force_the_move_into_plot_moves(monkeypatch, tmp_path):
    """The accept criterion: a model reply that defers again still yields the move."""
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, _arc())
    _cfg(monkeypatch)
    _write_briefs(root, {0: _brief("beat.a1b1"), 2: _brief("beat.a1b1")})
    (root / "history.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
        ]), encoding="utf-8")

    def fake_chat(messages, model=None, **kw):
        if model == "test-analyst":
            return json.dumps({
                "intent": "act", "assessment": "a", "outcome": "clean",
                "plot_moves": [],
                "deferred": [{"id": "beat.a1b1", "reason": "still mid-scene"}],
                "keep_true": [],
                "narration": {"words": 100, "push": 0.5, "end": "decision"},
                "clarify": None, "act_complete": False,
            })
        return "Narration."

    monkeypatch.setattr(dm, "chat_complete", fake_chat)

    dm.player_turn("I search the alley.")

    # Four stored messages precede the new player message at index 4.
    saved = analyst.load_brief_for_msg(root, 4)
    assert saved is not None, sorted(analyst.load_briefs(root))
    assert [m["id"] for m in saved["plot_moves"]] == ["beat.a1b1"]
    assert saved["deferred"] == []


def test_a_first_defer_is_still_honoured(monkeypatch, tmp_path):
    """One defer is allowed; only the third consecutive one is forced."""
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, _arc())
    _cfg(monkeypatch)
    _write_briefs(root, {0: _brief("beat.a1b1")})
    (root / "history.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
        ]), encoding="utf-8")

    def fake_chat(messages, model=None, **kw):
        if model == "test-analyst":
            return json.dumps({
                "intent": "act", "assessment": "a", "outcome": "clean",
                "plot_moves": [],
                "deferred": [{"id": "beat.a1b1", "reason": "mid-scene"}],
                "keep_true": [],
                "narration": {"words": 100, "push": 0.5, "end": "decision"},
                "clarify": None, "act_complete": False,
            })
        return "Narration."

    monkeypatch.setattr(dm, "chat_complete", fake_chat)

    dm.player_turn("I search the alley.")

    # Two stored messages precede the new player message at index 2.
    saved = analyst.load_brief_for_msg(root, 2)
    assert saved is not None, sorted(analyst.load_briefs(root))
    assert saved["plot_moves"] == []
    assert [d["id"] for d in saved["deferred"]] == ["beat.a1b1"]


def test_undo_rolls_the_defer_count_back(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    dm.arc.save_arc(root, _arc())
    _cfg(monkeypatch)
    _write_briefs(root, {0: _brief("beat.a1b1"), 2: _brief("beat.a1b1")})
    (root / "history.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
        ]), encoding="utf-8")

    assert dm._defer_counts() == {"beat.a1b1": 2}

    dm.undo_turn()

    # truncate_briefs keeps entries strictly before len(h) - 2 == 2, so msg 0 survives.
    assert dm._defer_counts() == {"beat.a1b1": 1}
