"""arc.py — Hidden arc, clock, difficulty scheduling, and record pass.

Doctrine:
- I8: The hidden arc never appears in history, premise, chapters, ledger, audit,
  any API/UI response, or default logs.
- D-09: Difficulty bending is runtime state (effective_deadline_minutes), not a plan revision.
- D-10: UI never shows arc or plot moves; only a non-spoiler chip ("Act 1 · active").
- Pure scheduling function allowed_moves() is unit-tested without LLM calls.
"""
import copy
import json
import os
import pathlib
import re
import shutil
import time
from config import cfg

# Difficulty parameters
# Min turns between overt moves
OVERT_COOLDOWN = {
    "story": 3,
    "standard": 2,
    "hard": 1,
}

# Share of remaining time that slips on counter
DEADLINE_SLIP_RATIO = {
    "story": 0.50,
    "standard": 0.25,
    "hard": 0.0,
}

# Counter-slips can never stretch a deadline past original * this factor.
DEADLINE_MAX_STRETCH = {
    "story": 2.0,
    "standard": 1.5,
    "hard": 1.0,
}

# In-game minutes a "delayed" effect pushes back the pressure's pending steps.
DELAY_MINUTES = {
    "story": 45,
    "standard": 30,
    "hard": 0,
}


def arc_path(root):
    return root / "arc.json"


def arc_log_path(root):
    return root / "arc_log.jsonl"


def records_path(root):
    return root / "records.jsonl"


def arc_snap_dir(root):
    d = root / "arc_snap"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_arc(root):
    p = arc_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "version" in data and "acts" in data:
            return data
    except (ValueError, OSError):
        pass
    return None


def arc_is_corrupt(root):
    """True when arc.json exists but is unusable. Callers must not overwrite it."""
    return arc_path(root).exists() and load_arc(root) is None


def save_arc(root, arc):
    p = arc_path(root)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(arc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def snapshot_arc(root, msg_idx):
    arc = load_arc(root)
    if not arc:
        return
    snap_p = arc_snap_dir(root) / f"{int(msg_idx)}.json"
    snap_p.write_text(json.dumps(arc, indent=2, ensure_ascii=False), encoding="utf-8")


def restore_arc_snapshot(root, msg_idx):
    """Restore arc state from snapshot for the given msg_idx."""
    snap_p = arc_snap_dir(root) / f"{int(msg_idx)}.json"
    if snap_p.exists():
        try:
            data = json.loads(snap_p.read_text(encoding="utf-8"))
            save_arc(root, data)
            return True
        except Exception:
            pass
    return False


# Files that make up the mutable arc runtime state (used to roll back a failed retry).
_RUNTIME_FILES = ("arc.json", "records.jsonl", "arc_log.jsonl")


def backup_state(root):
    """Raw text of each runtime file (None if absent)."""
    out = {}
    for name in _RUNTIME_FILES:
        p = root / name
        out[name] = p.read_text(encoding="utf-8") if p.exists() else None
    return out


def restore_state(root, backup):
    """Put the files captured by backup_state() back exactly as they were."""
    for name, text in backup.items():
        p = root / name
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(text, encoding="utf-8")


def load_records(root):
    p = records_path(root)
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def append_record(root, record):
    p = records_path(root)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def truncate_records(root, upto_msg_idx):
    p = records_path(root)
    if not p.exists():
        return
    kept = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if int(r.get("msg", -1)) < upto_msg_idx:
                kept.append(line)
        except Exception:
            pass
    p.write_text("".join(l + "\n" for l in kept), encoding="utf-8")


def truncate_arc_log(root, upto_msg_idx):
    p = arc_log_path(root)
    if not p.exists():
        return
    kept = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if int(r.get("at_msg", -1)) < upto_msg_idx:
                kept.append(line)
        except Exception:
            pass
    p.write_text("".join(l + "\n" for l in kept), encoding="utf-8")


def append_arc_log(root, rev, at_msg, act, kind, diff, reason):
    p = arc_log_path(root)
    entry = {
        "rev": rev,
        "at_msg": at_msg,
        "act": act,
        "kind": kind,
        "diff": diff,
        "reason": reason,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def get_arc_chip(arc):
    """Return non-spoiler chip string e.g. 'Act 1 · active' or None."""
    if not arc or not isinstance(arc, dict):
        return None
    act_n = arc.get("act", 1)
    acts = arc.get("acts", [])
    status = "active"
    for a in acts:
        if a.get("n") == act_n:
            status = a.get("status", "active")
            break
    return f"Act {act_n} · {status}"


# ---------------------------------------------------------------------------
# Scheduling: pure function allowed_moves()
# ---------------------------------------------------------------------------

def allowed_moves(arc, difficulty="standard", turn=1, last_overt_turn=None, stalled_turns=0):
    """Calculate due plot moves for the current turn.

    Pure function: no I/O, no LLM calls.
    Returns: list of dicts: [{"id": step_id, "how": "overt|subtle", "note": text, "pressure_id": pid}]
    Hard cap: <= 2 plot moves returned.
    """
    if not arc or not isinstance(arc, dict):
        return []

    diff = difficulty.lower() if difficulty in ("story", "standard", "hard") else "standard"
    clock_minutes = arc.get("clock", {}).get("minutes", 0)
    cooldown = OVERT_COOLDOWN.get(diff, 2)
    can_overt = (last_overt_turn is None) or ((turn - last_overt_turn) >= cooldown)

    due_moves = []
    pressures = arc.get("pressures", [])

    for p in pressures:
        if p.get("status") != "active":
            continue
        pid = p.get("id")
        steps = p.get("steps", [])
        for s in steps:
            if s.get("status") == "pending":
                at_min = s.get("at_minutes", 0)
                if clock_minutes >= at_min:
                    due_moves.append({
                        "id": s.get("id"),
                        "pressure_id": pid,
                        "at_minutes": at_min,
                        "effect": s.get("effect", ""),
                        "can_overt": can_overt,
                    })
        eff_dl = p.get("effective_deadline_minutes", p.get("deadline_minutes"))
        if isinstance(eff_dl, (int, float)) and eff_dl > 0 and clock_minutes >= eff_dl:
            due_moves.append({
                "id": f"{pid}.deadline", "pressure_id": pid, "at_minutes": eff_dl, "deadline": True,
                "effect": (f"Deadline reached: {p.get('antagonist', 'the antagonist')} succeeds, "
                           f"or nearly does ({p.get('goal', '')})."),
                "can_overt": True,
            })

    # Deadlines first, then earliest scheduled first
    due_moves.sort(key=lambda m: (not m.get("deadline"), m["at_minutes"]))

    # Format into plot move items
    results = []
    has_overt = False
    for m in due_moves:
        how = "overt" if (m["can_overt"] and not has_overt) else "subtle"
        if how == "overt":
            has_overt = True
        results.append({
            "id": m["id"],
            "how": how,
            "note": m["effect"],
            "pressure_id": m["pressure_id"],
        })
        if len(results) >= 2:  # Hard cap
            break

    # Rescue opening on story difficulty if stalled for >= 3 turns
    if diff == "story" and stalled_turns >= 3 and len(results) < 2:
        results.append({
            "id": "rescue_opening",
            "how": "subtle",
            "note": "A fortuitous discovery or sympathetic ally creates a clear path forward.",
            "pressure_id": "rescue",
        })

    return results[:2]


def calculate_deadline_slip(difficulty, current_minutes, deadline_minutes):
    """Calculate deadline slip minutes when player counters a pressure."""
    diff = difficulty.lower() if difficulty in ("story", "standard", "hard") else "standard"
    ratio = DEADLINE_SLIP_RATIO.get(diff, 0.25)
    remaining = max(0, deadline_minutes - current_minutes)
    return int(remaining * ratio)


def _norm_diff(difficulty):
    return difficulty if difficulty in ("story", "standard", "hard") else "standard"


def apply_counter_slip(p, difficulty, current_minutes):
    """Slip the effective deadline after a counter, capped at original * DEADLINE_MAX_STRETCH."""
    diff = _norm_diff(difficulty)
    orig = p.get("deadline_minutes", 0)
    cur = p.get("effective_deadline_minutes", orig)
    slip = calculate_deadline_slip(diff, current_minutes, cur)
    cap = max(cur, int(orig * DEADLINE_MAX_STRETCH.get(diff, 1.5)))
    p["effective_deadline_minutes"] = min(cur + slip, cap)


def apply_delay(p, difficulty):
    """Push the pressure's pending steps later (no-op on hard)."""
    delay = DELAY_MINUTES.get(_norm_diff(difficulty), 30)
    if not delay:
        return
    for s in p.get("steps", []):
        if s.get("status") == "pending":
            s["at_minutes"] = int(s.get("at_minutes", 0)) + delay


def apply_played_moves(arc, played_moves, turn):
    """Apply the moves the brief played this turn to runtime state.

    Marks played steps done, expires a pressure whose deadline move was played, and records
    last_overt_turn. Returns (played {id: how}, touched pressure ids).
    """
    rt = arc.setdefault("runtime", {})
    played = {}
    for m in played_moves or []:
        if isinstance(m, dict) and m.get("id"):
            played[str(m["id"])] = str(m.get("how", "subtle"))
    touched = set()
    for p in arc.get("pressures", []):
        pid = p.get("id")
        for s in p.get("steps", []):
            if s.get("id") in played and s.get("status") == "pending":
                s["status"] = "done"
                touched.add(pid)
        if f"{pid}.deadline" in played:
            p["status"] = "expired"
            touched.add(pid)
    if "overt" in played.values():
        rt["last_overt_turn"] = int(turn)
    return played, touched


# ---------------------------------------------------------------------------
# Planner Validation & Act Boundary
# ---------------------------------------------------------------------------

def validate_planner_revision(old_arc, new_arc):
    """Validate planner output diff against invariants (A6).

    Reject any change to done beats, recorded outcomes, or central_question
    unless explicitly resolved.
    """
    if not isinstance(new_arc, dict):
        return False, "Planner output is not a JSON object"

    # Central question check
    if old_arc.get("central_question") != new_arc.get("central_question"):
        if not new_arc.get("central_question_resolved"):
            return False, "central_question altered without central_question_resolved: true"

    # Verify done beats in old acts were not altered or dropped
    old_acts_by_n = {a["n"]: a for a in old_arc.get("acts", [])}
    new_acts_by_n = {a["n"]: a for a in new_arc.get("acts", [])}

    for n, old_act in old_acts_by_n.items():
        if n not in new_acts_by_n:
            return False, f"Act {n} was removed in new plan"
        new_act = new_acts_by_n[n]

        # Outcomes check: existing outcomes cannot be changed
        old_outcomes = old_act.get("outcomes", [])
        new_outcomes = new_act.get("outcomes", [])
        if len(new_outcomes) < len(old_outcomes):
            return False, f"Outcomes for Act {n} were deleted"
        for i, o in enumerate(old_outcomes):
            if new_outcomes[i] != o:
                return False, f"Outcome {i} for Act {n} was modified"

        # Done beats check: done beats must remain done and untouched
        old_done_beats = {b["id"]: b for b in old_act.get("beats", []) if b.get("status") == "done"}
        new_beats_by_id = {b["id"]: b for b in new_act.get("beats", [])}
        for bid, ob in old_done_beats.items():
            if bid not in new_beats_by_id:
                return False, f"Done beat {bid} was deleted in Act {n}"
            nb = new_beats_by_id[bid]
            if nb.get("status") != "done":
                return False, f"Done beat {bid} changed status to {nb.get('status')}"

    return True, "Valid"


# ---------------------------------------------------------------------------
# Planning: Initial Arc & Act Planning Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "You are the master story planner and narrative architect of a text RPG.\n"
    "Create a dramatic arc structure in JSON following this exact schema:\n"
    "{\n"
    '  "version": 1,\n'
    '  "revision": 0,\n'
    '  "central_question": "Will the protagonist achieve their driving goal despite the odds?",\n'
    '  "act": 1,\n'
    '  "clock": {"minutes": 0},\n'
    '  "acts": [\n'
    '    {"n": 1, "status": "active", "goal": "Establish stakes and confront immediate obstacle",\n'
    '     "beats": [\n'
    '       {"id": "a1b1", "text": "Opening discovery", "status": "pending"},\n'
    '       {"id": "a1b2", "text": "Escalation", "status": "pending"},\n'
    '       {"id": "a1b3", "text": "Act climax", "status": "pending"}\n'
    '     ], "outcomes": []},\n'
    '    {"n": 2, "status": "planned", "goal": "Rising action and deepening stakes", "beats": [], "outcomes": []},\n'
    '    {"n": 3, "status": "planned", "goal": "Climax and resolution", "beats": [], "outcomes": []}\n'
    '  ],\n'
    '  "pressures": [\n'
    '    {"id": "p1", "antagonist": "Primary Antagonist", "goal": "Their agenda", "status": "active",\n'
    '     "steps": [\n'
    '       {"id": "p1.s1", "at_minutes": 60, "effect": "First overt move", "status": "pending"},\n'
    '       {"id": "p1.s2", "at_minutes": 180, "effect": "Second move", "status": "pending"}\n'
    '     ],\n'
    '     "deadline_minutes": 720, "effective_deadline_minutes": 720}\n'
    '  ]\n'
    "}\n"
    "Rules:\n"
    "- Act 1 has 3-5 concrete dramatic beats.\n"
    "- Acts 2 and 3 have goals outlined, but empty beats (D-04: planned later based on outcomes).\n"
    "- 1-2 pressures with in-game minute timings.\n"
    "- Return ONLY the valid JSON object."
)


def plan_initial_arc(premise, summary, ledger_str, history, chat_fn):
    """Generate initial arc.json via planner model."""
    model = cfg.planner_model or cfg.scribe_model or cfg.model
    h_tail = history[-20:] if len(history) > 20 else history
    play_str = "\n".join(f"{'PLAYER' if m['role'] == 'user' else 'DM'}: {m['content']}" for m in h_tail)

    prompt = (
        f"PREMISE:\n{premise}\n\n"
        f"STORY SO FAR:\n{summary or '(new story)'}\n\n"
        f"CANON LEDGER:\n{ledger_str or '(empty)'}\n\n"
        f"RECENT PLAY:\n{play_str or '(opening)'}\n\n"
        "Draft the initial arc structure."
    )

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    raw = chat_fn(messages, model=model, track=False, retries=1, fallback=True)
    # Parse tolerantly
    start = raw.index("{")
    end = raw.rindex("}") + 1
    arc = json.loads(raw[start:end])
    # Validate minimal fields
    if not arc.get("central_question") or not arc.get("acts"):
        raise ValueError("Invalid arc structure generated by planner")
    arc["version"] = 1
    arc["revision"] = 0
    arc["act"] = 1
    arc["clock"] = {"minutes": 0}
    return arc


# ---------------------------------------------------------------------------
# Record Pass: Background evaluation after turn
# ---------------------------------------------------------------------------

RECORD_PROMPT = (
    "Evaluate the in-game consequences of the latest exchange.\n"
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "elapsed_minutes": <int: estimated in-game minutes elapsed, 1-120>,\n'
    '  "beats_done": [<list of beat id strings from the active act that were accomplished or resolved>],\n'
    '  "pressures": [\n'
    '    {"id": "<pressure_id>", "effect": "advanced|delayed|countered|cancelled|none"}\n'
    '  ],\n'
    '  "act_complete": <bool: true if the active act goal is fully achieved or ready for transition>\n'
    "}"
)


def _clamp_elapsed(value, default=5, lo=1, hi=120):
    """Coerce the recorder's elapsed_minutes to an int in [lo, hi]; junk -> default."""
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def run_record_pass(root, player_action, dm_reply, msg_idx, chat_fn, played_moves=None, intent="act"):
    """Execute record pass over the newest turn, updating clock, beats, pressures.

    played_moves / intent come from the analyst brief (empty / "act" when it is off).
    """
    arc = load_arc(root)
    if not arc:
        return

    # Snapshot BEFORE updating (I4)
    snapshot_arc(root, msg_idx)

    if intent == "ask":
        append_record(root, {"msg": int(msg_idx), "elapsed_minutes": 0, "beats_done": [],
                             "pressures": [], "act_complete": False,
                             "words": len(dm_reply.split())})
        return

    model = cfg.scribe_model or cfg.model
    act_n = arc.get("act", 1)
    active_act = next((a for a in arc.get("acts", []) if a.get("n") == act_n), {})

    prompt = (
        f"CURRENT ARC STATUS:\n"
        f"Act {act_n}: {active_act.get('goal', '')}\n"
        f"Pending beats: {json.dumps([b for b in active_act.get('beats', []) if b.get('status') == 'pending'], ensure_ascii=False)}\n"
        f"Active pressures: {json.dumps(arc.get('pressures', []), ensure_ascii=False)}\n\n"
        f"LATEST EXCHANGE:\n"
        f"PLAYER: {player_action}\n"
        f"DM: {dm_reply}\n\n"
        f"{RECORD_PROMPT}"
    )

    messages = [
        {"role": "system", "content": "You are the continuity recorder for a text RPG. Output only JSON."},
        {"role": "user", "content": prompt},
    ]

    try:
        raw = chat_fn(messages, model=model, track=False, retries=1, fallback=True)
        start = raw.index("{")
        end = raw.rindex("}") + 1
        res = json.loads(raw[start:end])
    except Exception:
        # Fallback record
        res = {
            "elapsed_minutes": 5,
            "beats_done": [],
            "pressures": [],
            "act_complete": False,
        }

    # Update clock
    elapsed = _clamp_elapsed(res.get("elapsed_minutes"))
    arc.setdefault("clock", {})["minutes"] = arc["clock"].get("minutes", 0) + elapsed

    # Update beats
    beats_raw = res.get("beats_done")
    beats_done = [str(x) for x in beats_raw] if isinstance(beats_raw, list) else []
    for b in active_act.get("beats", []):
        if b.get("id") in beats_done and b.get("status") == "pending":
            b["status"] = "done"

    # Moves the brief played this turn + stall counter (runtime state; arc_snap restores it on undo)
    turn = (int(msg_idx) - 1) // 2 + 1
    played, touched = apply_played_moves(arc, played_moves, turn)
    rt = arc.setdefault("runtime", {})
    if beats_done or played:
        rt["stalled_turns"] = 0
    elif intent in ("act", "steer"):
        rt["stalled_turns"] = int(rt.get("stalled_turns", 0)) + 1

    # Update pressures & deadline bending (D-09)
    diff = cfg.difficulty
    pressure_effects = res.get("pressures", [])
    if isinstance(pressure_effects, list):
        for pe in pressure_effects:
            if not isinstance(pe, dict):
                continue
            pid = pe.get("id")
            effect = str(pe.get("effect", "none")).lower()
            p = next((p for p in arc.get("pressures", []) if p.get("id") == pid), None)
            if not p:
                continue

            if effect == "countered":
                apply_counter_slip(p, diff, arc["clock"]["minutes"])
            elif effect == "delayed":
                apply_delay(p, diff)
            elif effect == "advanced":
                # A step played this turn already is the advance; don't skip a second one.
                if pid not in touched:
                    for s in p.get("steps", []):
                        if s.get("status") == "pending":
                            s["status"] = "done"
                            break
            elif effect == "cancelled":
                p["status"] = "cancelled"

    act_complete = bool(res.get("act_complete", False))
    # Check if all beats in active act are done
    pending_beats = [b for b in active_act.get("beats", []) if b.get("status") == "pending"]
    if not pending_beats and active_act.get("beats"):
        act_complete = True

    words = len(dm_reply.split())

    # Save updated arc
    save_arc(root, arc)

    # Append record
    record = {
        "msg": int(msg_idx),
        "elapsed_minutes": elapsed,
        "beats_done": beats_done,
        "pressures": pressure_effects,
        "act_complete": act_complete,
        "words": words,
    }
    append_record(root, record)

    # If act complete, trigger act transition
    if act_complete and active_act.get("status") == "active":
        transition_act(root, act_n, chat_fn, msg_idx=msg_idx)


def transition_act(root, act_n, chat_fn, msg_idx=None):
    """Handle act boundary transition and revision logging.

    msg_idx is the history index of the turn that closed the act. arc_log entries are
    keyed by it so undo/retry truncate the log in the same unit as records.
    """
    arc = load_arc(root)
    if not arc:
        return

    active_act = next((a for a in arc.get("acts", []) if a.get("n") == act_n), None)
    if not active_act:
        return

    active_act["status"] = "resolved"

    # Extract 3-6 factual outcomes from records or scribe
    records = load_records(root)
    all_beats = [b.get("text", b.get("id")) for b in active_act.get("beats", []) if b.get("status") == "done"]
    outcomes = all_beats[:6] if all_beats else ["Act objectives resolved."]
    active_act["outcomes"] = outcomes

    next_n = act_n + 1
    next_act = next((a for a in arc.get("acts", []) if a.get("n") == next_n), None)
    if next_act:
        arc["act"] = next_n
        next_act["status"] = "active"

        # Generate beats for next act if empty
        if not next_act.get("beats"):
            model = cfg.planner_model or cfg.scribe_model or cfg.model
            prompt = (
                f"ACT {act_n} RESOLVED OUTCOMES:\n{json.dumps(outcomes, ensure_ascii=False)}\n\n"
                f"CENTRAL QUESTION: {arc.get('central_question')}\n"
                f"ACT {next_n} GOAL: {next_act.get('goal', '')}\n\n"
                "Provide 3-5 sequential dramatic beats for Act " + str(next_n) + " as a JSON list:\n"
                '[{"id": "a' + str(next_n) + 'b1", "text": "...", "status": "pending"}, ...]'
            )
            try:
                raw = chat_fn([
                    {"role": "system", "content": "You are the story planner. Return only a JSON array of beats."},
                    {"role": "user", "content": prompt},
                ], model=model, track=False, retries=1, fallback=True)
                start = raw.index("[")
                end = raw.rindex("]") + 1
                beats = json.loads(raw[start:end])
                if isinstance(beats, list) and beats:
                    next_act["beats"] = beats
            except Exception:
                next_act["beats"] = [{"id": f"a{next_n}b1", "text": "Navigate emerging complications", "status": "pending"}]

    arc["revision"] = arc.get("revision", 0) + 1
    save_arc(root, arc)

    append_arc_log(
        root,
        rev=arc["revision"],
        at_msg=int(msg_idx) if msg_idx is not None else len(records),
        act=act_n,
        kind="act_boundary",
        diff={"resolved_act": act_n, "new_act": arc.get("act")},
        reason=f"Act {act_n} resolved and transitioned to Act {arc.get('act')}",
    )
