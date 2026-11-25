"""tests/test_p3.py — Hidden arc, clock, difficulty scheduling, and record pass tests."""
import json
import pytest
import dm
import arc


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text("", encoding="utf-8")
    (root / "undone.jsonl").write_text("", encoding="utf-8")
    (root / "summary.md").write_text("", encoding="utf-8")
    (root / "ledger.json").write_text("[]", encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"folded": 0, "ledgered": 0}), encoding="utf-8")


SAMPLE_ARC = {
    "version": 1,
    "revision": 0,
    "central_question": "Will the hero uncover the conspiracy before the city falls?",
    "act": 1,
    "clock": {"minutes": 100},
    "acts": [
        {
            "n": 1,
            "status": "active",
            "goal": "Uncover the first clue",
            "beats": [
                {"id": "a1b1", "text": "Find the dead messenger", "status": "pending"},
                {"id": "a1b2", "text": "Decipher the seal", "status": "pending"},
                {"id": "a1b3", "text": "Confront the magistrate", "status": "pending"},
            ],
            "outcomes": [],
        },
        {"n": 2, "status": "planned", "goal": "Pursue the cabal", "beats": [], "outcomes": []},
        {"n": 3, "status": "planned", "goal": "Prevent the catastrophe", "beats": [], "outcomes": []},
    ],
    "pressures": [
        {
            "id": "p1",
            "antagonist": "Cult of the Shadow",
            "goal": "Assassinate witness",
            "status": "active",
            "steps": [
                {"id": "p1.s1", "at_minutes": 60, "effect": "Cult scout spotted", "status": "pending"},
                {"id": "p1.s2", "at_minutes": 90, "effect": "Poisoner arrives at inn", "status": "pending"},
                {"id": "p1.s3", "at_minutes": 150, "effect": "Strike ordered", "status": "pending"},
            ],
            "deadline_minutes": 300,
            "effective_deadline_minutes": 300,
        }
    ],
}


# ---------------------------------------------------------------------------
# 1. Allowed moves: story, standard, hard & move caps (A5)
# ---------------------------------------------------------------------------

def test_plot_moves_capped_at_2():
    # Clock is at 100 min: p1.s1 (60 min) and p1.s2 (90 min) are due
    moves = arc.allowed_moves(SAMPLE_ARC, difficulty="standard", turn=1)
    assert len(moves) <= 2
    assert moves[0]["id"] == "p1.s1"
    assert moves[1]["id"] == "p1.s2"


def test_allowed_moves_story_rescue_after_3_stalls():
    # Story difficulty: if stalled for 3 turns, rescue opening is generated
    arc_copy = json.loads(json.dumps(SAMPLE_ARC))
    arc_copy["clock"]["minutes"] = 10  # No pressure steps due (earliest is 60)
    moves = arc.allowed_moves(arc_copy, difficulty="story", turn=5, stalled_turns=3)
    assert len(moves) == 1
    assert moves[0]["id"] == "rescue_opening"

    # On standard or hard, no rescue opening
    moves_std = arc.allowed_moves(arc_copy, difficulty="standard", turn=5, stalled_turns=3)
    assert len(moves_std) == 0
    moves_hard = arc.allowed_moves(arc_copy, difficulty="hard", turn=5, stalled_turns=3)
    assert len(moves_hard) == 0


def test_deadline_slip_table():
    # 200 minutes remaining (300 deadline - 100 current)
    # Story: <= 50% slip -> 100 min
    assert arc.calculate_deadline_slip("story", 100, 300) == 100
    # Standard: <= 25% slip -> 50 min
    assert arc.calculate_deadline_slip("standard", 100, 300) == 50
    # Hard: 0 slip (hard never bends)
    assert arc.calculate_deadline_slip("hard", 100, 300) == 0


# ---------------------------------------------------------------------------
# 3. Act boundary accepted writes arc_log.jsonl (A6)
# ---------------------------------------------------------------------------

def test_act_boundary_accepted_writes_arc_log(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)
    arc.save_arc(root, SAMPLE_ARC)

    # Transition Act 1
    arc.transition_act(root, act_n=1, chat_fn=lambda msgs, **kw: '[{"id": "a2b1", "text": "Investigate magistrate", "status": "pending"}]')

    updated = arc.load_arc(root)
    assert updated["acts"][0]["status"] == "resolved"
    assert updated["act"] == 2
    assert updated["acts"][1]["status"] == "active"
    assert len(updated["acts"][1]["beats"]) > 0

    # arc_log.jsonl must have an entry
    log_file = root / "arc_log.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    entries = [json.loads(line) for line in lines]
    entry = next(item for item in entries if item["kind"] == "act_boundary")
    assert entry["act"] == 1
    assert any(item["kind"] == "next_act_fallback" for item in entries)


# ---------------------------------------------------------------------------
# 4. Arc leak test: Sentinel strings never leak into history, premise, chapters, ledger, audit, or API (A9)
# ---------------------------------------------------------------------------

def test_arc_leak_no_arc_in_history(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    # Seed arc with unique sentinels
    SENTINEL_QUESTION = "SECRET_SENTINEL_CENTRAL_QUESTION_9981"
    SENTINEL_ANTAGONIST = "SECRET_SENTINEL_ANTAGONIST_7742"
    SENTINEL_BEAT = "SECRET_SENTINEL_BEAT_3321"

    seeded_arc = json.loads(json.dumps(SAMPLE_ARC))
    seeded_arc["central_question"] = SENTINEL_QUESTION
    seeded_arc["pressures"][0]["antagonist"] = SENTINEL_ANTAGONIST
    seeded_arc["acts"][0]["beats"][0]["text"] = SENTINEL_BEAT
    arc.save_arc(root, seeded_arc)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True,
        "analyst_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "analyst_model": "test-analyst",
        "scribe_model": "test-scribe",
        "ledger_enabled": True,
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-analyst":
            return json.dumps({
                "intent": "act",
                "assessment": "Player investigates the alley.",
                "outcome": "clean",
                "plot_moves": [{"id": "p1.s1", "how": "subtle", "note": "A cloaked figure watches."}],
                "keep_true": [],
                "narration": {"words": 100, "push": 0.5, "end": "decision"},
                "clarify": None,
                "act_complete": False,
            })
        if model == "test-scribe":
            # Record pass or ledger pass
            return json.dumps({
                "elapsed_minutes": 15,
                "beats_done": ["a1b1"],
                "pressures": [{"id": "p1", "effect": "none"}],
                "act_complete": False,
            })
        return "The alley is cold and quiet. A faint shadow slips past."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    # Run 3 turns
    for i in range(3):
        dm.player_turn(f"I search the alley carefully turn {i}.")

    sentinels = [SENTINEL_QUESTION, SENTINEL_ANTAGONIST, SENTINEL_BEAT]

    # Check history.jsonl
    history_text = (root / "history.jsonl").read_text(encoding="utf-8")
    for s in sentinels:
        assert s not in history_text

    # Check premise.md
    premise_text = (root / "premise.md").read_text(encoding="utf-8")
    for s in sentinels:
        assert s not in premise_text

    # Check summary.md
    summary_text = (root / "summary.md").read_text(encoding="utf-8")
    for s in sentinels:
        assert s not in summary_text

    # Check ledger.json
    ledger_text = (root / "ledger.json").read_text(encoding="utf-8")
    for s in sentinels:
        assert s not in ledger_text

    # Check /api/state response via test client
    with dm.app.test_client() as client:
        res = client.get("/api/state")
        state_json = json.dumps(res.get_json())
        for s in sentinels:
            assert s not in state_json
        # Only non-spoiler arc_chip should be in state
        data = res.get_json()
        assert "Act 1" in data.get("arc_chip", "")


# ---------------------------------------------------------------------------
# 5. Undo / Retry restores arc state snapshots (A4)
# ---------------------------------------------------------------------------

def test_undo_restores_arc_snap(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    arc_state = json.loads(json.dumps(SAMPLE_ARC))
    arc_state["clock"]["minutes"] = 50
    arc.save_arc(root, arc_state)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "scribe_model": "test-scribe",
        "ledger_enabled": False,
    })

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-scribe":
            return json.dumps({
                "elapsed_minutes": 25,
                "beats_done": ["a1b1"],
                "pressures": [{"id": "p1", "effect": "none"}],
                "act_complete": False,
            })
        return "You inspect the surroundings."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("Turn 1 action.")
    # Clock advanced to 50 + 25 = 75
    post_turn_arc = arc.load_arc(root)
    assert post_turn_arc["clock"]["minutes"] == 75

    # Now undo!
    dm.undo_turn()

    # Clock must be restored to pre-turn snapshot of 50 minutes!
    restored_arc = arc.load_arc(root)
    assert restored_arc["clock"]["minutes"] == 50
    assert restored_arc["acts"][0]["beats"][0]["status"] == "pending"


def test_retry_restores_and_re_records(monkeypatch, tmp_path):
    root = tmp_path / "campaigns" / dm.CAMPAIGN
    _init(root)

    arc_state = json.loads(json.dumps(SAMPLE_ARC))
    arc_state["clock"]["minutes"] = 50
    arc.save_arc(root, arc_state)

    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "arc_enabled": True,
        "api_key": "test",
        "model": "test-narrator",
        "scribe_model": "test-scribe",
        "ledger_enabled": False,
    })

    call_count = {"scribe": 0}

    def fake_chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
        if model == "test-scribe":
            call_count["scribe"] += 1
            elapsed = 20 if call_count["scribe"] == 1 else 30
            return json.dumps({
                "elapsed_minutes": elapsed,
                "beats_done": [],
                "pressures": [],
                "act_complete": False,
            })
        return "Narrator text."

    monkeypatch.setattr(dm, "chat_complete", fake_chat_complete)

    dm.player_turn("Turn 1 action.")
    assert arc.load_arc(root)["clock"]["minutes"] == 70  # 50 + 20

    # Retry should restore snapshot (50) and then re-record with elapsed=30 -> 80
    dm.retry_turn()
    assert arc.load_arc(root)["clock"]["minutes"] == 80  # 50 + 30
