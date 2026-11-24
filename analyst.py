"""analyst.py — Director's brief and out-of-character (OOC) notes routing.

doctrine:
- The analyst runs before the narrator when cfg.analyst_enabled.
- Returns a structured JSON brief clamped and validated in code.
- Fallback never blocks play (I6).
- Briefs are stored in briefs.jsonl (keyed by player message index) and injected
  as volatile additions in the last user message at build time (I3).
- Notes routing handles OOC inquiries without polluting history.jsonl.
"""
import concurrent.futures
import copy
import json
import re
import threading
import time
import uuid

from config import cfg
from storage import append_jsonl, parse_json_response, replace_lines

BRIEF_SCHEMA = {
    "intent": "act",
    "assessment": "The player acts.",
    "outcome": "clean",
    "plot_moves": [],
    "deferred": [],
    "keep_true": [],
    "narration": {"words": 140, "push": 0.6, "end": "decision"},
    "clarify": None,
    "act_complete": False,
}

VALID_INTENTS = ("act", "ask", "steer", "wait")
VALID_OUTCOMES = ("clean", "partial", "complication", "setback")
VALID_ENDS = ("decision", "question", "beat")
VALID_HOWS = ("overt", "subtle", "offscreen")
FALLBACK_REASONS = ("timeout", "invalid_response", "analyst_error")

# A move may be deferred at most MAX_DEFERS consecutive turns before code forces it in.
MAX_DEFERS = 2


class _Brief(dict):
    """Brief carrying diagnostics outside its narrator-visible dictionary fields."""

    def __init__(self, *args, diagnostics=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics or {}


def parse_json_tolerantly(raw):
    """Find and parse first balanced JSON object."""
    if not raw or not isinstance(raw, str):
        return None
    parsed = parse_json_response(raw)
    return parsed if isinstance(parsed, dict) else None


def clamp_brief(d, allowed_move_ids=None, defer_counts=None):
    """Clamp and validate raw dictionary to strict brief schema.

    allowed_move_ids: None (no filter), a set of ids, or a dict id -> scheduled move. With a dict,
    scheduled `how` and `note` are authoritative.
    defer_counts: dict id -> number of consecutive most-recent briefs that deferred it.
    Moves deferred MAX_DEFERS times in a row are forced into plot_moves.
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
    defer_counts = defer_counts or {}
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
            if isinstance(sched, dict):
                sched_how = str(sched.get("how", "subtle")).strip().lower()
                how = sched_how if sched_how in VALID_HOWS else "subtle"
                note = str(sched.get("note", "")).strip()[:200]
            else:
                note = str(m.get("note", "")).strip()[:200]
            seen.add(mid)
            moves.append({"id": mid, "how": how, "note": note})
            if len(moves) >= 2:
                break

    # deferred: enforce the defer cap. A move deferred MAX_DEFERS consecutive turns
    # is forced into plot_moves instead of staying deferred.
    raw_deferred = d.get("deferred")
    deferred = []
    seen_deferred = set()
    if isinstance(raw_deferred, list):
        for item in raw_deferred:
            if isinstance(item, dict):
                mid = str(item.get("id", "")).strip()
                reason = str(item.get("reason", "")).strip()[:200]
            elif isinstance(item, str):
                mid = item.strip()
                reason = ""
            else:
                continue
            if not mid or mid in seen_deferred:
                continue
            if defer_counts.get(mid, 0) >= MAX_DEFERS:
                # Defer cap reached: force into plot_moves if allowed and not already there
                if (allowed_move_ids is None or mid in allowed_move_ids) and mid not in seen:
                    sched = allowed_move_ids.get(mid) if isinstance(allowed_move_ids, dict) else None
                    how = str(sched.get("how", "subtle")).strip().lower() if isinstance(sched, dict) else "subtle"
                    if how not in VALID_HOWS:
                        how = "subtle"
                    note = str(sched.get("note", "")).strip()[:200] if isinstance(sched, dict) else ""
                    seen.add(mid)
                    moves.append({"id": mid, "how": how, "note": note})
                    if len(moves) >= 2:
                        continue
                continue  # drop from deferred
            seen_deferred.add(mid)
            deferred.append({"id": mid, "reason": reason})
    out["plot_moves"] = moves
    out["deferred"] = deferred

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
    reason = reason if reason in FALLBACK_REASONS else "analyst_error"
    brief = _Brief(copy.deepcopy(BRIEF_SCHEMA),
                   diagnostics={"fallback_reason": reason})
    brief["narration"] = {
        "words": cfg.style_words_max if cfg.style_words_max > 0 else 140,
        "push": round((cfg.style_push / 100.0) if cfg.style_push is not None else 0.6, 2),
        "end": "decision",
    }
    brief["assessment"] = "Action proceeds."
    # FIX6: the fallback brief carries no steering key; steering survives via the
    # record pass / steering.json path, not the brief schema.
    return brief


def is_ooc_note(text):
    """Check whether a player input is an OOC note."""
    t = (text or "").strip().lower()
    return t.startswith("//") or t == "ooc" or t.startswith("ooc ") or t.startswith("ooc:") or t.startswith("note to dm:")


def strip_ooc_prefix(text):
    t = (text or "").strip()
    if t.lower().startswith("//"):
        return t[2:].strip()
    if t.lower() == "ooc":
        return ""
    if t.lower().startswith("ooc "):
        return t[3:].strip()
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
    diagnostics = getattr(brief, "diagnostics", None)
    if diagnostics:
        entry["diagnostics"] = diagnostics
    append_jsonl(p, [entry])


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
    replace_lines(p, kept)


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


NOTES_LOCK = threading.Lock()


def save_note(root, note):
    p = notes_path(root)
    with NOTES_LOCK:
        append_jsonl(p, [note])


def update_note_canon(root, note_id, is_canon=True):
    p = notes_path(root)
    with NOTES_LOCK:
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
            replace_lines(p, [json.dumps(n, ensure_ascii=False) for n in notes])
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
    '  "deferred": [{"id": "move_id", "reason": "why deferred"}],\n'
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
    "- A move may be deferred at most MAX_DEFERS consecutive turns; after that code forces it in.\n"
)


def _truncate_to_cap(text, cap_chars):
    """Truncate text to at most cap_chars characters, cutting from the end."""
    if cap_chars is None or cap_chars <= 0 or len(text) <= cap_chars:
        return text
    return text[:cap_chars]


def _build_analyst_user_prompt(premise, summary, ledger_text, hooks, previous_brief, steering,
                               act_goal, cap_chars):
    """Build the analyst user prompt with total size capped, truncated oldest-first.

    Sections in authority order: premise, summary, ledger, previous brief.
    Truncation removes oldest sections first (premise first, then summary, etc.).
    """
    sections = []
    if premise:
        sections.append(("PREMISE", premise))
    if summary:
        sections.append(("SUMMARY", summary))
    if ledger_text:
        sections.append(("CANON LEDGER", ledger_text))
    if hooks:
        sections.append(("PENDING HOOKS", hooks))
    if previous_brief:
        sections.append(("PREVIOUS BRIEF", previous_brief))
    if steering:
        sections.append(("STEERING", steering))
    if act_goal:
        sections.append(("CURRENT ACT GOAL", act_goal))

    # Build full prompt, then truncate oldest-first if over cap.
    parts = []
    for name, body in sections:
        parts.append(f"{name}:\n{body}")
    full = "\n\n".join(parts)

    if len(full) <= cap_chars:
        return full

    # Truncate oldest-first: drop entire sections starting from the oldest,
    # then truncate within the oldest remaining section if still over cap.
    remaining = list(sections)
    while remaining and len("\n\n".join(f"{n}:\n{b}" for n, b in remaining)) > cap_chars:
        remaining.pop(0)  # drop oldest section

    if not remaining:
        return ""

    result = "\n\n".join(f"{n}:\n{b}" for n, b in remaining)
    if len(result) > cap_chars:
        # Truncate within the oldest remaining section
        oldest_name, oldest_body = remaining[0]
        # Keep the header, truncate the body
        budget = cap_chars - sum(len(f"{n}:\n{b}") + 2 for n, b in remaining[1:]) - len(f"{oldest_name}:\n")
        if budget > 0:
            truncated_body = oldest_body[:budget]
            result = f"{oldest_name}:\n{truncated_body}" + "\n\n" + "\n\n".join(
                f"{n}:\n{b}" for n, b in remaining[1:]
            )
        else:
            result = "\n\n".join(f"{n}:\n{b}" for n, b in remaining[1:])
    return result


def run_analyst(root, history, player_action, allowed_moves, style_guard_list, chat_fn,
                ledger_text="", act_goal="", premise_text="", summary_text="",
                previous_brief="", hooks="", steering="", defer_counts=None,
                timeout_sec=None, budget_mult=1.0):
    """Run analyst model call with timeout and fallback.

    FIX2: The analyst prompt now includes pinned markers on the ledger (handled by
    the caller rendering ledger_text with markers), the latest summary chapter
    (capped by analyst_summary_tok), the pending hooks, the previous brief, and
    the steering state. The total prompt is capped by analyst_prompt_cap and
    truncated oldest-first (premise, summary, ledger, previous brief).

    FIX4: timeout_sec bounds the analyst HTTP call. When provided, chat_fn is
    called with timeout_sec=timeout_sec so the call cannot outlive the analyst
    budget.
    """
    model = cfg.analyst_model or cfg.scribe_model or cfg.model
    allowed_ids = {m["id"]: m for m in allowed_moves} if allowed_moves else {}

    # Cap the summary chapter by analyst_summary_tok (default 4000 tokens ≈ chars/4).
    summary_cap_chars = int(cfg.analyst_summary_tok * cfg.chars_per_tok)
    summary_capped = _truncate_to_cap(summary_text, summary_cap_chars)

    # Build the user prompt with total cap, truncated oldest-first.
    cap_chars = int(cfg.analyst_prompt_cap)
    user_prompt = _build_analyst_user_prompt(
        premise=premise_text,
        summary=summary_capped,
        ledger_text=ledger_text,
        hooks=hooks,
        previous_brief=previous_brief,
        steering=steering,
        act_goal=act_goal,
        cap_chars=cap_chars,
    )

    # Append fixed metadata that must always be present (not subject to truncation).
    user_prompt += (
        f"\n\nDIFFICULTY: {cfg.difficulty}\n"
        f"STYLE KNOBS: preset={cfg.style_preset}, words={cfg.style_words_min}-{cfg.style_words_max}, push={cfg.style_push}\n"
        f"ALLOWED PLOT MOVES: {json.dumps(allowed_moves or [], ensure_ascii=False)}\n"
        f"STYLE GUARD (avoid): {json.dumps(style_guard_list or [], ensure_ascii=False)}\n"
        f"PLAYER ACTION:\n{player_action}\n\n"
        "Output ONLY the JSON brief."
    )

    messages = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    t0 = time.time()
    timeout = max(0.1, float(timeout_sec or cfg.analyst_timeout_sec))

    # No `with` block: its exit calls shutdown(wait=True) and would sit there until the
    # model call finishes, which defeats the timeout (D-14).
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(chat_fn, messages, model=model, track=False, retries=0,
                                 fallback=False, timeout_sec=timeout)
        raw = future.result(timeout=timeout)
        parsed = parse_json_tolerantly(raw)
        if not parsed:
            raise ValueError("Unparseable JSON from analyst")
        brief = clamp_brief(parsed, allowed_ids, defer_counts)
        # E4: apply budget multiplier to narration words
        if budget_mult != 1.0 and "narration" in brief:
            brief["narration"]["words"] = max(50, int(brief["narration"]["words"] * budget_mult))
        ms = int((time.time() - t0) * 1000)
        return brief, "analyst", model, ms
    except concurrent.futures.TimeoutError:
        brief = make_fallback_brief(reason="timeout")
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms
    except ValueError:
        brief = make_fallback_brief(reason="invalid_response")
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms
    except Exception:
        brief = make_fallback_brief(reason="analyst_error")
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
