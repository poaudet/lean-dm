"""arc.py — Arc planning, clock, difficulty scheduling, and record pass.

Doctrine:
- The full arc is inspectable through the explicit debug state payload.
- D-09: Difficulty bending is runtime state (effective_deadline_minutes), not a plan revision.
- Pure scheduling function allowed_moves() is unit-tested without LLM calls.
"""
import json
import logging

import steering
from config import cfg
from storage import append_jsonl, atomic_write, parse_json_response, replace_lines

logger = logging.getLogger(__name__)

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

# Max consecutive off-arc turns before a replan is triggered (overridden by
# cfg.drift_turns, default 4).
DRIFT_TURNS_DEFAULT = 4

# Max turns in an act before forced transition (0 = off; overridden by
# cfg.act_max_turns).
ACT_MAX_TURNS_DEFAULT = 0

# A pressure with no pending steps and a deadline more than this many minutes
# away may get up to this many future steps appended.
EMPTY_PRESSURE_MINUTES_AWAY = 120
EMPTY_PRESSURE_MAX_STEPS = 2

# Planner validation limits
MIN_BEATS = 3
MAX_BEATS = 5
MAX_PRESSURES = 2
MIN_STEP_GAP = 30  # minimum spacing between pressure steps
MIN_DEADLINE_GAP = 30  # minimum time from the final step to the deadline
MAX_DEADLINE_MINUTES = 14400  # 10 days

# B2: turns without progress before a beat hook is pushed even while a pressure is due.
# Initial values, to be tuned after playtest.
STALL_THRESHOLD = {
    "story": 2,
    "standard": 3,
    "hard": 5,
}

# B3: turns a hook stays at one level before escalating (subtle -> overt -> offer).
HOOK_ESCALATION = 2

# Beat moves carry this prefix so they never collide with a pressure step id.
BEAT_MOVE_PREFIX = "beat."


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
            return normalize_arc(data)
    except (ValueError, OSError):
        pass
    return None


def arc_is_corrupt(root):
    """True when arc.json exists but is unusable. Callers must not overwrite it."""
    return arc_path(root).exists() and load_arc(root) is None


def save_arc(root, arc):
    p = arc_path(root)
    atomic_write(p, json.dumps(arc, indent=2, ensure_ascii=False))


def snapshot_arc(root, msg_idx):
    arc = load_arc(root)
    if not arc:
        return
    snap_p = arc_snap_dir(root) / f"{int(msg_idx)}.json"
    atomic_write(snap_p, json.dumps(arc, indent=2, ensure_ascii=False))


def restore_arc_snapshot(root, msg_idx):
    """Restore arc state from snapshot for the given msg_idx."""
    snap_p = arc_snap_dir(root) / f"{int(msg_idx)}.json"
    if not snap_p.exists():
        logger.warning("arc snapshot %s is missing; the arc was not rewound", snap_p.name)
        return False
    try:
        data = json.loads(snap_p.read_text(encoding="utf-8"))
        save_arc(root, data)
        return True
    except Exception as e:
        logger.warning("arc snapshot %s is unreadable: %s", snap_p.name, e)
    return False


def prune_arc_snapshots(root, below):
    """Delete snapshots for msg indexes < below (undo can never reach them)."""
    d = root / "arc_snap"
    if not d.is_dir():
        return
    for f in d.glob("*.json"):
        if f.stem.isdigit() and int(f.stem) < below:
            f.unlink(missing_ok=True)


# Files that make up the mutable runtime state (used to roll back a failed retry).
_RUNTIME_FILES = ("arc.json", "records.jsonl", "arc_log.jsonl", "steering.json")


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
            atomic_write(p, text)


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
    append_jsonl(p, [record])


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
    replace_lines(p, kept)


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
    replace_lines(p, kept)


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
    append_jsonl(p, [entry])


def get_arc_chip(arc):
    """Return non-spoiler chip string e.g. 'Act 1 · active' or None."""
    if not arc or not isinstance(arc, dict):
        return None
    act_n = arc.get("act", 1)
    acts = arc.get("acts", [])
    status = "active"
    for a in acts if isinstance(acts, list) else []:
        if isinstance(a, dict) and a.get("n") == act_n:
            status = a.get("status", "active")
            break
    return f"Act {act_n} · {status}"


# ---------------------------------------------------------------------------
# Scheduling: pure function allowed_moves()
# ---------------------------------------------------------------------------

def pending_beat(arc):
    """The next pending beat of the active act, or None. Pure, no I/O (B2)."""
    if not arc or not isinstance(arc, dict):
        return None
    act_n = arc.get("act", 1)
    for a in arc.get("acts") or []:
        if not isinstance(a, dict) or a.get("n") != act_n:
            continue
        for b in a.get("beats") or []:
            if isinstance(b, dict) and b.get("status") == "pending":
                return b
    return None


def hook_level(stalled_turns):
    """B3: a hook rises subtle -> overt -> offer while it goes unanswered."""
    try:
        n = int(stalled_turns)
    except (TypeError, ValueError):
        n = 0
    if n >= 2 * HOOK_ESCALATION:
        return "offer"
    if n >= HOOK_ESCALATION:
        return "overt"
    return "subtle"


def allowed_moves(arc, difficulty="standard", turn=1, last_overt_turn=None, stalled_turns=0):
    """Calculate due plot moves for the current turn.

    Pure function: no I/O, no LLM calls.
    Returns: list of dicts: [{"id": step_id, "how": "overt|subtle|offer", "note": text, "pressure_id": pid}]
    Hard cap: <= 2 plot moves returned. Deadlines first, then the beat hook (B2).
    Superseded and blocked beats never produce moves.
    """
    if not arc or not isinstance(arc, dict) or arc.get("terminal"):
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

    # B2: the next pending beat's hook, once the stall threshold is reached (or at once
    # when no pressure move is due). B3: its level rises while it goes unanswered, and
    # that escalation overrides the overt cooldown.
    beat = pending_beat(arc)
    if beat and beat.get("hook") and len(results) < 2:
        threshold = STALL_THRESHOLD.get(diff, 3)
        if not results or stalled_turns >= threshold:
            results.append({
                "id": f"{BEAT_MOVE_PREFIX}{beat.get('id')}",
                "how": hook_level(stalled_turns),
                "note": str(beat["hook"])[:200],
                "pressure_id": "beat",
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

    Marks played pressure steps done, expires a pressure whose deadline move was played, and
    records last_overt_turn. A played beat hook is NOT marked done (B6): only the record pass
    completes a beat. Returns (played {id: how}, touched pressure ids).
    Only pressure moves set last_overt_turn (D-23: hook escalation overrides overt cooldown).
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
    # D-23: hook escalation overrides the overt cooldown, so a beat move must not start
    # the cooldown for pressure moves. Only pressure moves set last_overt_turn (D-28).
    if any(how == "overt" and not mid.startswith(BEAT_MOVE_PREFIX)
           for mid, how in played.items()):
        rt["last_overt_turn"] = int(turn)
    return played, touched


# ---------------------------------------------------------------------------
# Planning: Initial Arc & Act Planning Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "ROLE: You are the hidden story-arc planner for a text RPG.\n"
    "INPUTS: PREMISE, STORY SO FAR, CANON LEDGER and recent play are authoritative story context. "
    "For a replan or next act, use the supplied outcomes and existing beat ids.\n"
    "TASK: Build or revise a three-act dramatic plan that can adapt to player choices.\n"
    "OUTPUT: Return only a JSON object with version (integer), revision (integer), central_question (string), "
    "act (integer), clock ({minutes: integer}), acts (array), and pressures (array). Each act has n, status, "
    "goal, beats and outcomes. Each beat has id, text, hook and status. Each pressure has id, antagonist, goal, "
    "status, steps, deadline_minutes and effective_deadline_minutes.\n"
    "RULES:\n"
    "1. The initial active Act 1 has 3-5 beats. Planned Acts 2 and 3 have goals and beats=[]; they are generated at transition.\n"
    "2. Replans and next-act plans contain 3-5 beats. Every beat has a non-empty hook of at most 200 characters.\n"
    "3. Beat ids must be unique across the entire arc, including superseded beats. Use act and revision in ids, e.g. a2r3b1. "
    "Never reuse an id listed in EXISTING BEAT IDS.\n"
    "4. Hook is a player-perceivable lead, never the antagonist's name or the solution. "
    "The full beat text remains available in Arc debug, not as a narrated hook.\n"
    "5. Use 1-2 pressures with ascending in-game minute timings. A deadline follows the final step and respects the supplied limits.\n"
    "6. Preserve outcomes already resolved; plan forward rather than rewriting past events.\n"
    "WHEN UNSURE: Prefer a simple, playable hook and preserve the established canon.\n"
    "EXAMPLES: Good id: a2r3b1 with hook 'A courier leaves a marked parcel'. Bad hook: naming the hidden antagonist or revealing who sent it."
)

def _planner_constraints():
    """Shared numeric/schema constraints supplied to initial, replan and next-act planners."""
    difficulty = _norm_diff(cfg.difficulty)
    stretch = DEADLINE_MAX_STRETCH.get(difficulty, 1.5)
    return (
        "EFFECTIVE PLANNER CONSTRAINTS (validation is authoritative):\n"
        f"- Every step at_minutes is a JSON integer >= 0; steps are ascending and at least {MIN_STEP_GAP} minutes apart.\n"
        f"- deadline_minutes is an integer >= 0, at least {MIN_DEADLINE_GAP} minutes after the final step, "
        f"and no greater than {MAX_DEADLINE_MINUTES}.\n"
        f"- effective_deadline_minutes is an integer >= 0 and no greater than the original deadline "
        f"times {stretch} for {difficulty} difficulty.\n"
    )


BEAT_PLANNER_SYSTEM = (
    "ROLE: You are the hidden beat planner for a text RPG.\n"
    "INPUTS: The user message supplies resolved outcomes, the act goal, central question and every existing beat id.\n"
    "TASK: Plan 3-5 sequential beats for the requested act without rewriting resolved events.\n"
    "OUTPUT: Return only a JSON array of objects with id (string), text (full beat detail), hook (string) and status "
    "(pending).\n"
    "RULES: Every id is unique across the whole arc, including superseded beats; include act and revision, e.g. "
    "a2r3b1, and never reuse an id in the supplied list. Every hook is non-empty and at most 200 characters. "
    "Hooks are player-perceivable leads: never name the antagonist or reveal the solution. Full beat text is "
    "inspectable in Arc debug, but must never be copied into a hook.\n"
    "WHEN UNSURE: Choose a modest, concrete lead and preserve established outcomes.\n"
    "EXAMPLES: Good hook: 'A courier leaves a marked parcel'. Bad hook: naming the hidden antagonist or revealing who sent it."
)



def _num(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _dicts(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def normalize_beats(beats, act_n=1):
    out = []
    for i, b in enumerate(_dicts(beats), start=1):
        b = dict(b)
        b["id"] = str(b.get("id") or f"a{act_n}b{i}")
        b["text"] = str(b.get("text") or "")
        b["hook"] = str(b.get("hook") or "")[:200]
        b["status"] = b.get("status") if b.get("status") in ("pending", "done", "superseded", "blocked") else "pending"
        out.append(b)
    return out


def normalize_arc(arc):
    """Coerce a planner-produced arc into the shape the scheduler expects; drops malformed acts, beats and steps."""
    if not isinstance(arc, dict):
        raise ValueError("arc is not an object")
    arc = dict(arc)
    acts = []
    for a in _dicts(arc.get("acts")):
        a = dict(a)
        n = _num(a.get("n"), None)
        if n is None:
            continue
        a["n"] = n
        a["beats"] = normalize_beats(a.get("beats"), n)
        acts.append(a)
    arc["acts"] = acts
    pressures = []
    for p in _dicts(arc.get("pressures")):
        p = dict(p)
        steps = []
        for s in _dicts(p.get("steps")):
            s = dict(s)
            if s.get("at_minutes") is None or _num(s.get("at_minutes"), None) is None:
                continue
            s["at_minutes"] = _num(s["at_minutes"])
            steps.append(s)
        p["steps"] = steps
        for key in ("deadline_minutes", "effective_deadline_minutes"):
            if key in p:
                n = _num(p[key], None)
                if n is None:
                    del p[key]
                else:
                    p[key] = n
        # A6: clamp effective_deadline_minutes to original * DEADLINE_MAX_STRETCH
        dl = p.get("deadline_minutes")
        eff_dl = p.get("effective_deadline_minutes")
        if dl is not None and eff_dl is not None:
            max_stretch = DEADLINE_MAX_STRETCH.get(_norm_diff(cfg.difficulty), 1.5)
            cap = int(dl * max_stretch)
            if eff_dl > cap:
                p["effective_deadline_minutes"] = cap
        pressures.append(p)
    arc["pressures"] = pressures
    clock = arc.get("clock")
    arc["clock"] = {**(clock if isinstance(clock, dict) else {}),
                    "minutes": _num((clock or {}).get("minutes") if isinstance(clock, dict) else 0)}
    if "act" in arc:
        arc["act"] = _num(arc["act"], 1)
    return arc


def validate_arc(arc):
    """Validate an arc plan (initial, replan, or next-act beats).

    Returns (ok, reason). On failure the caller should retry once, then keep
    the previous arc (or the single fallback beat for a next act).

    Rules:
    - ids unique across acts
    - at_minutes integers, non-negative, ascending and at least MIN_STEP_GAP apart per pressure
    - at most MAX_PRESSURES pressures
    - MIN_BEATS..MAX_BEATS beats on the active act and on any act that already
      has beats (counting only non-superseded beats); an act with no beats and
      status `planned` is valid
    - every beat needs a non-empty player-perceivable hook; full beat text is available in Arc debug
    - deadline_minutes at least MIN_DEADLINE_GAP above the last step and at most MAX_DEADLINE_MINUTES
    - effective_deadline_minutes at most original * DEADLINE_MAX_STRETCH[difficulty]
    """
    if not isinstance(arc, dict):
        return False, "arc is not an object"

    # Collect all beat ids across acts for uniqueness
    all_beat_ids = set()
    for a in _dicts(arc.get("acts")):
        beats = a.get("beats", [])
        if not isinstance(beats, list):
            return False, f"act {a.get('n')} beats is not a list"
        act_n = a.get("n")
        live_beats = [b for b in _dicts(beats) if b.get("status") != "superseded"]
        is_active = act_n == arc.get("act")
        has_beats = len(live_beats) > 0
        if (is_active or has_beats) and (len(live_beats) < MIN_BEATS or len(live_beats) > MAX_BEATS):
            return False, f"act {act_n} has {len(live_beats)} beats, need {MIN_BEATS}-{MAX_BEATS}"
        for b in _dicts(beats):
            bid = str(b.get("id", "")).strip()
            if not bid:
                return False, f"act {act_n} beat missing id"
            if bid in all_beat_ids:
                return False, f"duplicate beat id {bid}"
            all_beat_ids.add(bid)
            if not str(b.get("hook", "")).strip():
                return False, f"beat {bid} has empty hook"

    pressures = _dicts(arc.get("pressures"))
    if len(pressures) > MAX_PRESSURES:
        return False, f"{len(pressures)} pressures, max {MAX_PRESSURES}"

    diff = _norm_diff(cfg.difficulty)
    max_stretch = DEADLINE_MAX_STRETCH.get(diff, 1.5)

    for p in pressures:
        pid = str(p.get("id", "")).strip()
        steps = _dicts(p.get("steps"))
        prev_at = -1
        for s in steps:
            at = _num(s.get("at_minutes"), None)
            if at is None or at < 0:
                return False, f"pressure {pid} step has invalid at_minutes"
            if prev_at >= 0 and at - prev_at < MIN_STEP_GAP:
                return False, f"pressure {pid} steps must be at least {MIN_STEP_GAP} minutes apart"
            prev_at = at

        dl = p.get("deadline_minutes")
        eff_dl = p.get("effective_deadline_minutes", dl)
        if dl is not None:
            dl = _num(dl, None)
            if dl is None:
                return False, f"pressure {pid} invalid deadline_minutes"
            if prev_at >= 0 and dl < prev_at + MIN_DEADLINE_GAP:
                return False, f"pressure {pid} deadline too close to last step"
            if dl > MAX_DEADLINE_MINUTES:
                return False, f"pressure {pid} deadline exceeds {MAX_DEADLINE_MINUTES}"
        if eff_dl is not None:
            eff_dl = _num(eff_dl, None)
            if eff_dl is None:
                return False, f"pressure {pid} invalid effective_deadline_minutes"
            if dl is not None and eff_dl > dl * max_stretch:
                return False, f"pressure {pid} effective_deadline exceeds stretch cap"

    return True, ""


def _validate_planner_time_schema(raw):
    """Reject non-integer/negative planner timings before normalize_arc coerces them."""
    if not isinstance(raw, dict):
        return False, "planner response is not an object"
    pressures = raw.get("pressures", [])
    if not isinstance(pressures, list):
        return False, "pressures must be an array"
    for pressure in pressures:
        if not isinstance(pressure, dict):
            return False, "pressure entries must be objects"
        steps = pressure.get("steps", [])
        if not isinstance(steps, list):
            return False, "pressure steps must be an array"
        for step in steps:
            if not isinstance(step, dict):
                return False, "pressure steps must be objects"
            at = step.get("at_minutes")
            if type(at) is not int or at < 0:
                return False, "step at_minutes must be a non-negative JSON integer"
        for key in ("deadline_minutes", "effective_deadline_minutes"):
            value = pressure.get(key)
            if value is not None and (type(value) is not int or value < 0):
                return False, f"{key} must be a non-negative JSON integer"
    return True, ""


def plan_initial_arc(premise, summary, ledger_str, history, chat_fn):
    """Generate an initial arc, retrying once when the planner output is invalid."""
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
        {"role": "system", "content": PLANNER_SYSTEM + "\n" + _planner_constraints()},
        {"role": "user", "content": prompt},
    ]
    last_error = None
    for attempt in range(2):
        try:
            raw = chat_fn(messages, model=model, track=False, retries=1, fallback=True)
            arc = parse_json_response(raw)
            if not isinstance(arc, dict):
                raise ValueError("planner response did not contain a JSON object")
            numeric_ok, numeric_reason = _validate_planner_time_schema(arc)
            if not numeric_ok:
                raise ValueError(numeric_reason)
            arc = normalize_arc(arc)
            ok, reason = validate_arc(arc)
            if not ok:
                raise ValueError(reason)
            if not arc.get("central_question") or not arc["acts"]:
                raise ValueError("invalid arc structure generated by planner")
            arc["version"] = 1
            arc["revision"] = 0
            arc["act"] = 1
            arc["clock"] = {"minutes": 0}
            return arc
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("initial planner output invalid; retrying once: %s", exc)
    raise ValueError(f"initial planner failed validation after retry: {last_error}")


def _unique_beat_id(arc, act_n, revision, base):
    """Return a beat id unique across the whole arc (revision included)."""
    candidate = f"a{act_n}r{revision}{base}"
    existing = {str(b.get("id")) for a in _dicts(arc.get("acts")) for b in _dicts(a.get("beats"))}
    i = 1
    while candidate in existing:
        candidate = f"a{act_n}r{revision}{base}{i}"
        i += 1
    return candidate


def _validate_planned_beats(arc, act_n, beats):
    """Validate a planner's beat list without revalidating unrelated live pressures."""
    if not isinstance(beats, list) or not MIN_BEATS <= len(beats) <= MAX_BEATS:
        return False, f"act {act_n} must have {MIN_BEATS}-{MAX_BEATS} beats"
    existing = {
        str(beat.get("id"))
        for act in _dicts(arc.get("acts"))
        for beat in _dicts(act.get("beats"))
    }
    seen = set()
    for beat in beats:
        beat_id = str(beat.get("id", "")).strip()
        if not beat_id:
            return False, "planned beat missing id"
        if beat_id in existing or beat_id in seen:
            return False, f"duplicate beat id {beat_id}"
        if not str(beat.get("hook", "")).strip():
            return False, f"beat {beat_id} has empty hook"
        seen.add(beat_id)
    return True, ""


def plan_replan_arc(arc, outcomes, history, chat_fn):
    """Replan the pending beats of the active act from outcomes so far.

    Old pending beats are kept as `superseded` (never deleted); new beats are
    appended with ids unique across the whole arc (revision included).
    Returns the updated arc dict, or None on failure (caller keeps old arc).
    On failure the previous beat statuses are restored exactly.
    """
    model = cfg.planner_model or cfg.scribe_model or cfg.model
    act_n = arc.get("act", 1)
    active_act = next((a for a in arc.get("acts", []) if a.get("n") == act_n), {})

    old_beats = list(active_act.get("beats", []))
    old_statuses = {str(b.get("id")): b.get("status") for b in old_beats}

    # Mark old pending beats as superseded (tentatively; restored on failure).
    for b in active_act.get("beats", []):
        if b.get("status") == "pending":
            b["status"] = "superseded"

    existing_ids = [str(b.get("id")) for a in _dicts(arc.get("acts")) for b in _dicts(a.get("beats"))]
    prompt = (
        f"ACT: {act_n}\n"
        f"RESOLVED OUTCOMES: {json.dumps(outcomes, ensure_ascii=False)}\n"
        f"CENTRAL QUESTION: {arc.get('central_question', '')}\n"
        f"ACT GOAL: {active_act.get('goal', '')}\n"
        f"EXISTING BEAT IDS: {json.dumps(existing_ids)}\n"
        "Plan replacement beats for the unresolved part of this act."
    )

    last_error = None
    for attempt in range(2):
        try:
            raw = chat_fn([
                {"role": "system", "content": BEAT_PLANNER_SYSTEM + "\n" + _planner_constraints()},
                {"role": "user", "content": prompt},
            ], model=model, track=False, retries=1, fallback=True)
            new_beats = normalize_beats(parse_json_response(raw), act_n)
            if len(new_beats) < MIN_BEATS or len(new_beats) > MAX_BEATS:
                raise ValueError("replan must return 3-5 beats")
            revision = _num(arc.get("revision"), 0) + 1
            for i, beat in enumerate(new_beats, start=1):
                beat["id"] = _unique_beat_id(arc, act_n, revision, f"b{i}")
            ok, reason = _validate_planned_beats(arc, act_n, new_beats)
            if not ok:
                raise ValueError(reason)
            active_act["beats"] = old_beats + new_beats
            arc["revision"] = revision
            return arc
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("replan output invalid; retrying once: %s", exc)
    # Restore previous statuses so a failed replan leaves beats exactly as before.
    for beat in active_act.get("beats", []):
        bid = str(beat.get("id"))
        if bid in old_statuses:
            beat["status"] = old_statuses[bid]
    logger.warning("replan failed after validation retry: %s", last_error)
    return None



def plan_next_act_beats(arc, next_n, outcomes, chat_fn):
    """Generate beats for the next act from outcomes.

    Returns a list of beats, or a single fallback beat on failure.
    """
    model = cfg.planner_model or cfg.scribe_model or cfg.model
    next_act = next((a for a in arc.get("acts", []) if a.get("n") == next_n), {})
    existing_ids = [str(b.get("id")) for a in _dicts(arc.get("acts")) for b in _dicts(a.get("beats"))]
    revision = _num(arc.get("revision"), 0) + 1
    prompt = (
        f"PREVIOUS ACT: {next_n - 1}\n"
        f"RESOLVED OUTCOMES: {json.dumps(outcomes, ensure_ascii=False)}\n"
        f"CENTRAL QUESTION: {arc.get('central_question', '')}\n"
        f"NEXT ACT: {next_n}\n"
        f"ACT GOAL: {next_act.get('goal', '')}\n"
        f"EXISTING BEAT IDS: {json.dumps(existing_ids)}\n"
        "Plan this act from the recorded outcomes."
    )
    last_error = None
    for attempt in range(2):
        try:
            raw = chat_fn([
                {"role": "system", "content": BEAT_PLANNER_SYSTEM + "\n" + _planner_constraints()},
                {"role": "user", "content": prompt},
            ], model=model, track=False, retries=1, fallback=True)
            beats = normalize_beats(parse_json_response(raw), next_n)
            if len(beats) < MIN_BEATS or len(beats) > MAX_BEATS:
                raise ValueError("next-act planner must return 3-5 beats")
            for i, beat in enumerate(beats, start=1):
                beat["id"] = _unique_beat_id(arc, next_n, revision, f"b{i}")
            ok, reason = _validate_planned_beats(arc, next_n, beats)
            if not ok:
                raise ValueError(reason)
            return beats
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("next-act planner output invalid; retrying once: %s", exc)
    logger.warning("next-act planning failed after validation retry: %s", last_error)
    fallback_id = _unique_beat_id(arc, next_n, revision, "b1")
    return [{"id": fallback_id, "text": "Navigate emerging complications",
             "hook": "An opening the player can act on", "status": "pending"}]



# ---------------------------------------------------------------------------
# Record Pass: Background evaluation after turn
# ---------------------------------------------------------------------------

RECORD_SYSTEM = (
    "You are the continuity recorder for a text RPG.\n"
    "ROLE: Evaluate the committed exchange and update only supported state.\n"
    "INPUTS: CURRENT ARC STATUS and CURRENT STEERING describe state before this exchange. LATEST EXCHANGE is the "
    "only new evidence. The full arc is available in the debug state; objective and open_question are steering summaries.\n"
    "TASK: Record only consequences supported by the exchange, without inventing events or changing canon.\n"
    "OUTPUT: Return only the JSON object defined in the user message.\n"
    "RULES: Use only supplied active-act beat and pressure ids. elapsed_minutes is an integer from 1 to 120. "
    "objective and open_question are each at most 160 characters; active_hook is at most 200 characters. "
    "matched_beats contains only active-act ids "
    "addressed by this exchange. progress is true only for a completed beat, changed objective, or resolved open question.\n"
    "WHEN UNSURE: Set booleans false, use empty arrays, and preserve the previous steering text.\n"
    "EXAMPLES: Good: matched_beats lists a supplied id. Bad: inventing a beat id or putting arc names in objective."
)

RECORD_PROMPT = (
    "OUTPUT SCHEMA (types and limits):\n"
    "{\n"
    '  "elapsed_minutes": integer 1-120,\n'
    '  "beats_done": array of active-act beat id strings accomplished or resolved,\n'
    '  "pressures": array of {"id": active pressure id, "effect": "advanced|delayed|countered|cancelled|none"},\n'
    '  "act_complete": boolean,\n'
    '  "central_question_resolved": boolean,\n'
    '  "on_arc": boolean,\n'
    '  "matched_beats": array of active-act beat ids addressed by this exchange,\n'
    '  "objective": string, maximum 160 characters,\n'
    '  "open_question": string, maximum 160 characters,\n'
    '  "active_hook": optional current player-perceivable lead, string or null, maximum 200 characters,\n'
    '  "progress": boolean\n'
    "}\n"
    "LIMITS: Do not include unknown ids. elapsed_minutes must be 1-120. objective/open_question must be concise, "
    "player-facing text and no more than 160 characters each; active_hook is at most 200 characters. "
    "progress is true only if a beat completed, the "
    "objective changed, or the open question was resolved."
)


def _strict_bool(value, default=False):
    """Accept JSON booleans only; truthy strings/numbers are malformed."""
    return value if isinstance(value, bool) else default


def _clamp_elapsed(value, default=5, lo=1, hi=120):
    """Coerce the recorder's elapsed_minutes to an int in [lo, hi]; junk -> default."""
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def _is_last_act(arc, act_n):
    return act_n >= max((_num(a.get("n")) for a in _dicts(arc.get("acts"))), default=act_n)


def _drift_turns():
    return int(cfg.drift_turns) if hasattr(cfg, "drift_turns") else DRIFT_TURNS_DEFAULT


def _act_max_turns():
    return int(cfg.act_max_turns) if hasattr(cfg, "act_max_turns") else ACT_MAX_TURNS_DEFAULT


def run_record_pass(root, player_action, dm_reply, msg_idx, chat_fn, played_moves=None,
                    intent="act", history=None, analyst_act_complete=False, clarify=False):
    """Execute record pass over the newest turn, updating clock, beats, pressures and steering.

    played_moves / intent come from the analyst brief (empty / "act" when it is off).
    WP6 additions:
    - on_arc and matched_beats are read from the record result and stored in arc.runtime.
    - After drift_turns consecutive off-arc turns, a replan is triggered.
    - After act_max_turns turns in an act, a forced transition occurs.
    Runs when the arc or the steering state is enabled; one model call covers both (A2).
    """
    arc = load_arc(root)
    steering_on = bool(cfg.steering_enabled)
    if not arc and not steering_on:
        return

    # Snapshot BEFORE updating (I4)
    if arc:
        snapshot_arc(root, msg_idx)
    if steering_on:
        steering.snapshot_steering(root, msg_idx)

    if intent == "ask" or clarify:
        # Ask/clarify turns leave the clock, arc progress, steering and due moves untouched.
        if arc:
            append_record(root, {"msg": int(msg_idx), "elapsed_minutes": 0, "beats_done": [],
                                 "pressures": [], "act_complete": False,
                                 "on_arc": False, "matched_beats": [],
                                 "words": len(dm_reply.split())})
        return

    model = cfg.scribe_model or cfg.model
    act_n = arc.get("act", 1) if arc else 1
    active_act = next((a for a in arc.get("acts", []) if a.get("n") == act_n), {}) if arc else {}
    previous = steering.load_steering(root) if steering_on else None

    blocks = []
    if arc:
        blocks.append(
            f"CURRENT ARC STATUS:\n"
            f"Act {act_n}: {active_act.get('goal', '')}\n"
            f"Central question: {arc.get('central_question', '')}\n"
            f"Pending beats: {json.dumps([b for b in active_act.get('beats', []) if b.get('status') == 'pending'], ensure_ascii=False)}\n"
            f"Active pressures: {json.dumps(arc.get('pressures', []), ensure_ascii=False)}\n"
        )
    if steering_on:
        prev = previous or {}
        blocks.append(
            "CURRENT STEERING (player-facing):\n"
            f"Objective: {prev.get('objective') or '(none yet)'}\n"
            f"Open question: {prev.get('open_question') or '(none yet)'}\n"
        )
    blocks.append(
        f"LATEST EXCHANGE:\n"
        f"PLAYER: {player_action}\n"
        f"DM: {dm_reply}\n\n"
        f"{RECORD_PROMPT}"
    )
    prompt = "\n".join(blocks)

    messages = [
        {"role": "system", "content": RECORD_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    failed = False
    try:
        raw = chat_fn(messages, model=model, track=False, retries=1, fallback=True)
        res = parse_json_response(raw)
        if not isinstance(res, dict):
            raise ValueError("record response did not contain a JSON object")
    except Exception:
        failed = True
        # Fallback record
        res = {
            "elapsed_minutes": 5,
            "beats_done": [],
            "pressures": [],
            "act_complete": False,
            "on_arc": True,
            "matched_beats": [],
        }

    if steering_on:
        state = (steering.stalled_steering(previous, msg_idx) if failed
                 else steering.clamp_steering(res, previous, msg_idx, source="record"))
        steering.save_steering(root, state)

    if not arc:
        return

    # Update clock
    elapsed = _clamp_elapsed(res.get("elapsed_minutes"))
    arc.setdefault("clock", {})["minutes"] = arc["clock"].get("minutes", 0) + elapsed

    # Update beats
    active_beat_ids = {str(b.get("id")) for b in active_act.get("beats", []) if b.get("id")}
    beats_raw = res.get("beats_done")
    requested_done = beats_raw if isinstance(beats_raw, list) else []
    beats_done = []
    for value in requested_done:
        if not isinstance(value, str) or value not in active_beat_ids or value in beats_done:
            continue
        beat = next((b for b in active_act.get("beats", []) if b.get("id") == value), None)
        if beat and beat.get("status") == "pending":
            beat["status"] = "done"
            beats_done.append(value)

    # Moves the brief played this turn + stall counter (runtime state; arc_snap restores it on undo)
    turn = (int(msg_idx) - 1) // 2 + 1
    played, touched = apply_played_moves(arc, played_moves, turn)
    rt = arc.setdefault("runtime", {})
    # B6: only a completed beat resets the stall counter. Playing a beat hook does not
    # complete the beat, so it must not clear the escalation either (B3).
    if beats_done:
        rt["stalled_turns"] = 0
    elif intent in ("act", "steer"):
        rt["stalled_turns"] = int(rt.get("stalled_turns", 0)) + 1

    # Track on_arc / matched_beats and drift counter (WP6 F2)
    on_arc = _strict_bool(res.get("on_arc"), True)
    matched_raw = res.get("matched_beats", [])
    matched_beats = []
    if isinstance(matched_raw, list):
        for value in matched_raw:
            if isinstance(value, str) and value in active_beat_ids and value not in matched_beats:
                matched_beats.append(value)
    rt["on_arc"] = on_arc
    rt["matched_beats"] = matched_beats

    if on_arc:
        rt["off_arc_turns"] = 0
    else:
        rt["off_arc_turns"] = int(rt.get("off_arc_turns", 0)) + 1

    # Update pressures & deadline bending (D-09)
    diff = cfg.difficulty
    pressure_effects = res.get("pressures", [])
    active_pressure_ids = {str(p.get("id")) for p in arc.get("pressures", [])
                           if p.get("id") and p.get("status") == "active"}
    if isinstance(pressure_effects, list):
        seen_pressure_effects = set()
        for pe in pressure_effects:
            if not isinstance(pe, dict):
                continue
            pid = pe.get("id")
            effect = str(pe.get("effect", "none")).lower()
            if not isinstance(pid, str) or pid not in active_pressure_ids or pid in seen_pressure_effects:
                continue
            if effect not in {"advanced", "delayed", "countered", "cancelled", "none"}:
                continue
            seen_pressure_effects.add(pid)
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

    act_complete = _strict_bool(res.get("act_complete")) or analyst_act_complete is True
    central_question_resolved = _strict_bool(res.get("central_question_resolved"))
    # Check if all beats in active act are done
    pending_beats = [b for b in active_act.get("beats", []) if b.get("status") == "pending"]
    if not pending_beats and active_act.get("beats"):
        act_complete = True
    if _is_last_act(arc, act_n) and not central_question_resolved:
        act_complete = False

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
        "on_arc": on_arc,
        "matched_beats": [str(b) for b in matched_beats],
        "words": words,
    }
    append_record(root, record)

    # WP6 F3: Act cap - forced transition when act_max_turns reached
    act_max = _act_max_turns()
    started_msg = int(active_act.get("started_msg", 0) or 0)
    turns_in_act = turn - (started_msg // 2 + 1) + 1 if started_msg else turn
    if act_max > 0 and turns_in_act >= act_max and active_act.get("status") == "active":
        logger.info("Act %d reached max turns %d; forcing transition", act_n, act_max)
        transition_act(root, act_n, chat_fn, msg_idx=msg_idx,
                       history=history, central_question_resolved=central_question_resolved,
                       forced=True)
        return

    # WP6 F2: Drift and replan
    drift_turns = _drift_turns()
    if drift_turns > 0 and intent != "ask" and int(rt.get("off_arc_turns", 0)) >= drift_turns:
        logger.info("Drift threshold reached (%d off-arc turns); triggering replan", drift_turns)
        # Collect outcomes from this act's records
        records = load_records(root)
        start_msg = max(0, int(active_act.get("started_msg", 0)))
        act_records = [r for r in records if start_msg <= int(r.get("msg", -1)) <= int(msg_idx)]
        outcomes = [
            f"{r.get('elapsed_minutes', 0)} min; beats: {', '.join(r.get('beats_done', [])) or 'none'}; "
            f"pressures: {json.dumps(r.get('pressures', []), ensure_ascii=False)}"
            for r in act_records[-12:]
        ]
        if not outcomes:
            outcomes = ["The act is ongoing; no detailed record available."]
        drift_count = int(rt.get("off_arc_turns", 0))
        new_arc = plan_replan_arc(arc, outcomes, history, chat_fn)
        if new_arc is not None:
            arc = new_arc
            rt["off_arc_turns"] = 0
            save_arc(root, arc)
            append_arc_log(
                root,
                rev=arc.get("revision", 0),
                at_msg=int(msg_idx),
                act=act_n,
                kind="replan",
                diff={"off_arc_turns": 0},
                reason=f"Replan after {drift_count} consecutive off-arc turns",
            )
        else:
            rt["off_arc_turns"] = 0
            save_arc(root, arc)
            logger.warning("Replan failed; keeping previous arc (I6)")
            append_arc_log(
                root,
                rev=arc.get("revision", 0),
                at_msg=int(msg_idx),
                act=act_n,
                kind="replan_failed",
                diff={"off_arc_turns": 0},
                reason=f"Replan failed after {drift_count} consecutive off-arc turns",
            )

    # If act complete, trigger act transition
    if act_complete and active_act.get("status") == "active":
        transition_act(root, act_n, chat_fn, msg_idx=msg_idx,
                       history=history, central_question_resolved=central_question_resolved)


def transition_act(root, act_n, chat_fn, msg_idx=None, history=None,
                   central_question_resolved=False, forced=False):
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
    records = load_records(root)
    start_msg = max(0, int(active_act.get("started_msg", 0)))
    act_records = [r for r in records if start_msg <= int(r.get("msg", -1)) <= int(msg_idx or 0)]
    play = history or []
    played_range = play[start_msg:(int(msg_idx) + 1 if msg_idx is not None else len(play))]
    play_lines = [
        f"[{i}] {'PLAYER' if message.get('role') == 'user' else 'DM'}: {message.get('content', '')}"
        for i, message in enumerate(played_range, start=start_msg)
    ]
    if len(play_lines) > 30:
        play_lines = play_lines[-30:]
    outcomes = [
        f"{record.get('elapsed_minutes', 0)} in-game minutes elapsed; "
        f"beats completed: {', '.join(record.get('beats_done', [])) or 'none'}; "
        f"pressure effects: {json.dumps(record.get('pressures', []), ensure_ascii=False)}"
        for record in act_records[-12:]
    ]
    if not outcomes:
        outcomes = ["The act objective was resolved in play; no detailed record was available."]
    try:
        model = cfg.scribe_model or cfg.model
        raw = chat_fn([
            {"role": "system", "content": "Summarize factual play outcomes for the resolved act. Return only a JSON array of 3-6 short strings."},
            {"role": "user", "content":
                f"ACT {act_n} GOAL:\n{active_act.get('goal', '')}\n\n"
                f"ACT PLAY HISTORY:\n{chr(10).join(play_lines)}\n\n"
                f"ACT RECORDS:\n{json.dumps(act_records, ensure_ascii=False)}"},
        ], model=model, track=False, retries=1, fallback=True)
        parsed = parse_json_response(raw)
        if isinstance(parsed, list):
            factual = [str(x).strip()[:300] for x in parsed if str(x).strip()]
            if factual:
                outcomes = factual[:6]
    except Exception:
        pass
    active_act["outcomes"] = outcomes

    next_n = act_n + 1
    next_act = next((a for a in arc.get("acts", []) if a.get("n") == next_n), None)
    next_act_fallback = False
    if next_act:
        arc["act"] = next_n
        next_act["status"] = "active"
        next_act["started_msg"] = int(msg_idx) + 1 if msg_idx is not None else 0

        # Generate beats for next act if empty
        if not next_act.get("beats"):
            beats = plan_next_act_beats(arc, next_n, outcomes, chat_fn)
            next_act["beats"] = beats
            next_act_fallback = len(beats) == 1
    else:
        arc["terminal"] = bool(central_question_resolved)
        arc["central_question_resolved"] = central_question_resolved

    arc["revision"] = arc.get("revision", 0) + 1
    save_arc(root, arc)

    kind = "act_boundary_forced" if forced else "act_boundary"
    reason = f"Act {act_n} resolved and transitioned to Act {arc.get('act')}"
    if forced:
        reason = f"Act {act_n} hit max-turn cap; forced transition to Act {arc.get('act')}"

    append_arc_log(
        root,
        rev=arc["revision"],
        at_msg=int(msg_idx) if msg_idx is not None else len(records),
        act=act_n,
        kind=kind,
        diff={"resolved_act": act_n, "new_act": arc.get("act"), "forced": forced},
        reason=reason,
    )
    if next_act_fallback:
        append_arc_log(
            root,
            rev=arc["revision"],
            at_msg=int(msg_idx) if msg_idx is not None else len(records),
            act=next_n,
            kind="next_act_fallback",
            diff={"fallback_beats": 1},
            reason="Next-act planner failed validation after one retry; inserted a single safe fallback beat",
        )


def fill_empty_pressures(root, arc, msg_idx=None):
    """WP6 F5: When a pressure has no pending steps and its deadline is more than
    EMPTY_PRESSURE_MINUTES_AWAY minutes away, append up to EMPTY_PRESSURE_MAX_STEPS
    future steps. Logged as a revision.

    Returns the (possibly updated) arc.
    """
    if not arc:
        return arc
    changed = False
    clock_minutes = arc.get("clock", {}).get("minutes", 0)
    for p in arc.get("pressures", []):
        if p.get("status") != "active":
            continue
        pending = [s for s in p.get("steps", []) if s.get("status") == "pending"]
        if pending:
            continue
        dl = p.get("effective_deadline_minutes", p.get("deadline_minutes"))
        if dl is None or dl - clock_minutes <= EMPTY_PRESSURE_MINUTES_AWAY:
            continue
        # New steps are spaced 60 minutes apart and leave the required gap before deadline.
        last_at = max(
            clock_minutes,
            max((_num(s.get("at_minutes"), 0) for s in p.get("steps", [])), default=clock_minutes),
        )
        added = 0
        for _ in range(EMPTY_PRESSURE_MAX_STEPS):
            new_at = last_at + 60
            if new_at + MIN_DEADLINE_GAP > dl:
                break
            p.setdefault("steps", []).append({
                "id": f"{p.get('id')}.s{len(p.get('steps', [])) + 1}",
                "at_minutes": new_at,
                "effect": f"Pressure {p.get('id')} continues to escalate",
                "status": "pending",
            })
            last_at = new_at
            added += 1
        changed = changed or added > 0

    if changed:
        arc["revision"] = arc.get("revision", 0) + 1
        save_arc(root, arc)
        append_arc_log(
            root,
            rev=arc["revision"],
            at_msg=int(msg_idx) if msg_idx is not None else arc.get("clock", {}).get("minutes", 0),
            act=arc.get("act", 1),
            kind="pressure_revision",
            diff={"filled_empty_pressures": True},
            reason="Appended future steps to pressures with no pending steps",
        )
    return arc
