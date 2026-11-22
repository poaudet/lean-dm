"""analyst.py — Director's brief and out-of-character (OOC) notes routing.

doctrine:
- The analyst runs before the narrator when cfg.analyst_enabled.
- Returns a structured JSON brief clamped and validated in code.
- Fallback never blocks play (I6).
- Briefs are stored in briefs.jsonl (keyed by player message index) and injected
  as volatile additions in the last user message at build time (I3).
- Notes routing handles OOC inquiries without polluting history.jsonl.
"""
import json
import os
import pathlib
import re
import time
import uuid
import concurrent.futures
import copy
from config import cfg

BRIEF_SCHEMA = {
    "intent": "act",
    "assessment": "The player acts.",
    "outcome": "clean",
    "plot_moves": [],
    "keep_true": [],
    "narration": {"words": 140, "push": 0.6, "end": "decision"},
    "clarify": None,
    "act_complete": False,
}

VALID_INTENTS = ("act", "ask", "steer", "wait")
VALID_OUTCOMES = ("clean", "partial", "complication", "setback")
VALID_ENDS = ("decision", "question", "beat")
VALID_HOWS = ("overt", "subtle", "offscreen")
_HOW_RANK = {"overt": 2, "subtle": 1, "offscreen": 0}


def parse_json_tolerantly(raw):
    """Find and parse first balanced JSON object."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


def clamp_brief(d, allowed_move_ids=None):
    """Clamp and validate raw dictionary to strict brief schema.

    allowed_move_ids: None (no filter), a set of ids, or a dict id -> scheduled move. With a dict,
    `how` may only be downgraded relative to the schedule, never upgraded.
    """
    if not isinstance(d, dict):
        d = {}
    out = copy.deepcopy(BRIEF_SCHEMA)

    # intent
    intent = str(d.get("intent", "")).strip().lower()
    out["intent"] = intent if intent in VALID_INTENTS else "act"

    # assessment
    assessment = str(d.get("assessment", "")).strip()
    out["assessment"] = assessment[:300] if assessment else "The player acts."

    # outcome
    outcome = str(d.get("outcome", "")).strip().lower()
    out["outcome"] = outcome if outcome in VALID_OUTCOMES else "clean"

    # plot_moves (cap at 2 moves max)
    raw_moves = d.get("plot_moves")
    moves = []
    seen = set()
    if isinstance(raw_moves, list):
        for m in raw_moves:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id", "")).strip()
            if not mid or mid in seen:
                continue
            if allowed_move_ids is not None and mid not in allowed_move_ids:
                continue
            sched = allowed_move_ids.get(mid) if isinstance(allowed_move_ids, dict) else None
            how = str(m.get("how", "subtle")).strip().lower()
            if how not in VALID_HOWS:
                how = "subtle"
            if sched:
                sched_how = str(sched.get("how", "subtle"))
                if _HOW_RANK[how] > _HOW_RANK.get(sched_how, 1):
                    how = sched_how
            note = (str(m.get("note", "")).strip() or str((sched or {}).get("note", "")).strip())[:200]
            seen.add(mid)
            moves.append({"id": mid, "how": how, "note": note})
            if len(moves) >= 2:
                break
    out["plot_moves"] = moves

    # keep_true (<= 8 items)
    kt = d.get("keep_true")
    if isinstance(kt, list):
        out["keep_true"] = [str(x).strip()[:150] for x in kt if str(x).strip()][:8]
    else:
        out["keep_true"] = []

    # narration
    narr = d.get("narration") if isinstance(d.get("narration"), dict) else {}
    target_words = narr.get("words")
    try:
        words = int(target_words)
        words = max(50, min(500, words))
    except (TypeError, ValueError):
        words = cfg.style_words_max if cfg.style_words_max > 0 else 140

    push = narr.get("push")
    try:
        push_val = float(push)
        push_val = max(0.0, min(1.0, push_val))
    except (TypeError, ValueError):
        push_val = (cfg.style_push / 100.0) if cfg.style_push is not None else 0.6

    end = str(narr.get("end", "")).strip().lower()
    if end not in VALID_ENDS:
        end = "decision"

    out["narration"] = {"words": words, "push": round(push_val, 2), "end": end}

    # clarify
    clarify = d.get("clarify")
    out["clarify"] = str(clarify).strip()[:200] if clarify else None

    # act_complete
    out["act_complete"] = bool(d.get("act_complete", False))
    return out


def make_fallback_brief(reason="fallback"):
    brief = copy.deepcopy(BRIEF_SCHEMA)
    brief["narration"] = {
        "words": cfg.style_words_max if cfg.style_words_max > 0 else 140,
        "push": round((cfg.style_push / 100.0) if cfg.style_push is not None else 0.6, 2),
        "end": "decision",
    }
    brief["assessment"] = "Action proceeds."
    return brief


def is_ooc_note(text):
    """Check whether a player input is an OOC note."""
    t = (text or "").strip().lower()
    return t.startswith("//") or t.startswith("ooc:") or t.startswith("note to dm:")


def strip_ooc_prefix(text):
    t = (text or "").strip()
    if t.lower().startswith("//"):
        return t[2:].strip()
    if t.lower().startswith("ooc:"):
        return t[4:].strip()
    if t.lower().startswith("note to dm:"):
        return t[11:].strip()
    return t


# ---------------------------------------------------------------------------
# Storage: briefs.jsonl & notes.jsonl
# ---------------------------------------------------------------------------

def briefs_path(root):
    return root / "briefs.jsonl"


def notes_path(root):
    return root / "notes.jsonl"


def load_briefs(root):
    p = briefs_path(root)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if "msg" in entry:
                out[int(entry["msg"])] = entry
        except (ValueError, TypeError):
            pass
    return out


def load_brief_for_msg(root, msg_idx):
    briefs = load_briefs(root)
    entry = briefs.get(int(msg_idx))
    return entry.get("brief") if entry else None


def save_brief(root, msg_idx, source, model, ms, brief):
    p = briefs_path(root)
    entry = {
        "msg": int(msg_idx),
        "source": source,
        "model": model,
        "ms": int(ms),
        "brief": brief,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def truncate_briefs(root, upto_index):
    """Keep only brief entries strictly before upto_index."""
    p = briefs_path(root)
    if not p.exists():
        return
    kept = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if int(entry.get("msg", -1)) < upto_index:
                kept.append(line)
        except (ValueError, TypeError):
            pass
    data = "".join(l + "\n" for l in kept)
    p.write_text(data, encoding="utf-8")


def load_notes(root):
    p = notes_path(root)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            pass
    return out


def save_note(root, note):
    p = notes_path(root)
    line = json.dumps(note, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def update_note_canon(root, note_id, is_canon=True):
    p = notes_path(root)
    if not p.exists():
        return False
    notes = load_notes(root)
    found = False
    for n in notes:
        if n.get("id") == note_id:
            n["canon"] = is_canon
            found = True
            break
    if found:
        data = "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes)
        p.write_text(data, encoding="utf-8")
    return found


# ---------------------------------------------------------------------------
# Analyst execution with timeout & fallback
# ---------------------------------------------------------------------------

ANALYST_SYSTEM = (
    "You are the narrative director and analyst of a text RPG.\n"
    "Evaluate the player's action and return ONLY a JSON brief with this structure:\n"
    "{\n"
    '  "intent": "act|ask|steer|wait",\n'
    '  "assessment": "one sentence: what the player attempts and how it lands in this scene",\n'
    '  "outcome": "clean|partial|complication|setback",\n'
    '  "plot_moves": [{"id": "move_id", "how": "overt|subtle|offscreen", "note": "what player perceives"}],\n'
    '  "keep_true": ["<=8 canon items at risk of contradiction"],\n'
    '  "narration": {"words": 140, "push": 0.7, "end": "decision|question|beat"},\n'
    '  "clarify": null,\n'
    '  "act_complete": false\n'
    "}\n"
    "Rules:\n"
    "- If intent is 'ask': direct answer <= 80 words, no plot move, no time passage.\n"
    "- If intent is 'steer': honor within difficulty.\n"
    "- If intent is 'act': resolve per assessment/outcome.\n"
    "- 'clarify' only if intent is genuinely ambiguous (rarely).\n"
    "- Choose plot_moves ONLY from the allowed_moves provided. If none are allowed, return [].\n"
)


def run_analyst(root, history, player_action, allowed_moves, style_guard_list, chat_fn):
    """Run analyst model call with timeout and fallback."""
    model = cfg.analyst_model or cfg.scribe_model or cfg.model
    allowed_ids = {m["id"]: m for m in allowed_moves} if allowed_moves else {}

    n_turns = cfg.analyst_context_turns
    h_tail = history[-2 * n_turns:] if len(history) > 2 * n_turns else history
    play_str = "\n".join(f"[{i}] {'PLAYER' if m['role'] == 'user' else 'DM'}: {m['content']}" for i, m in enumerate(h_tail))

    user_prompt = (
        f"DIFFICULTY: {cfg.difficulty}\n"
        f"STYLE KNOBS: preset={cfg.style_preset}, words={cfg.style_words_min}-{cfg.style_words_max}, push={cfg.style_push}\n"
        f"ALLOWED PLOT MOVES: {json.dumps(allowed_moves or [], ensure_ascii=False)}\n"
        f"STYLE GUARD (avoid): {json.dumps(style_guard_list or [], ensure_ascii=False)}\n\n"
        f"RECENT PLAY:\n{play_str}\n\n"
        f"PLAYER ACTION:\n{player_action}\n\n"
        "Output ONLY the JSON brief."
    )

    messages = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    t0 = time.time()
    timeout = max(0.1, float(cfg.analyst_timeout_sec))

    # No `with` block: its exit calls shutdown(wait=True) and would sit there until the
    # model call finishes, which defeats the timeout (D-14).
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(chat_fn, messages, model=model, track=False, retries=0, fallback=False)
        raw = future.result(timeout=timeout)
        parsed = parse_json_tolerantly(raw)
        if not parsed:
            raise ValueError("Unparseable JSON from analyst")
        brief = clamp_brief(parsed, allowed_ids)
        ms = int((time.time() - t0) * 1000)
        return brief, "analyst", model, ms
    except Exception as e:
        brief = make_fallback_brief(reason=(str(e) or type(e).__name__)[:50])
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# OOC Notes responder
# ---------------------------------------------------------------------------

OOC_SYSTEM = (
    "You are the Dungeon Master responding OUT OF CHARACTER (OOC) to a player note.\n"
    "Answer the player's question or clarification directly, plainly, and honestly.\n"
    "Never advance the story, roll dice, or narrate in character.\n"
    "If the player is proposing a canon fact or world detail, end your response with a JSON block:\n"
    '```json\n{"canon_proposal": {"id": "slug", "kind": "person|place|item|rule", "name": "...", "state": "..."}}\n```\n'
    "Otherwise do not include any JSON."
)


def handle_ooc_note(root, note_text, history, premise_text, summary_text, ledger_text, chat_fn):
    """Answer an OOC note, save to notes.jsonl, and return answer text."""
    clean_q = strip_ooc_prefix(note_text)
    model = cfg.analyst_model or cfg.scribe_model or cfg.model

    h_tail = history[-12:] if len(history) > 12 else history
    play_str = "\n".join(f"{'PLAYER' if m['role'] == 'user' else 'DM'}: {m['content']}" for m in h_tail)

    prompt = (
        f"PREMISE:\n{premise_text}\n\n"
        f"SUMMARY:\n{summary_text}\n\n"
        f"CANON LEDGER:\n{ledger_text}\n\n"
        f"RECENT PLAY:\n{play_str}\n\n"
        f"PLAYER'S OOC NOTE:\n{clean_q}\n"
    )

    messages = [
        {"role": "system", "content": OOC_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        reply = chat_fn(messages, model=model, track=False, retries=1, fallback=True)
    except Exception as e:
        reply = f"[OOC: received your note. (Error answering: {e})]"

    # Extract proposal if present
    proposal = None
    if "```json" in reply:
        try:
            json_str = reply.split("```json", 1)[1].split("```", 1)[0].strip()
            data = json.loads(json_str)
            proposal = data.get("canon_proposal")
        except Exception:
            pass
    elif '{"canon_proposal"' in reply:
        try:
            data = parse_json_tolerantly(reply)
            if data:
                proposal = data.get("canon_proposal")
        except Exception:
            pass

    clean_a = re.sub(r"```json.*?```", "", reply, flags=re.S).strip()

    note_entry = {
        "id": str(uuid.uuid4())[:8],
        "ts": int(time.time()),
        "q": note_text,
        "a": clean_a or reply,
        "canon": False,
        "proposal": proposal,
    }
    save_note(root, note_entry)
    return note_entry
