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
VALID_HOWS = ("overt", "subtle", "offscreen", "offer")
FALLBACK_REASONS = ("timeout", "invalid_response", "analyst_error")

# B4: a due move may be deferred at most this many consecutive turns.
MAX_DEFERS = 2
MAX_DEFERRED = 8

# C2: keep_true limits.
MAX_KEEP_TRUE = 8
MAX_FREE_TEXT = 3
RECENT_EXCHANGES = 3


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


def _defer_count(defer_counts, mid):
    try:
        return int((defer_counts or {}).get(mid, 0))
    except (TypeError, ValueError):
        return 0


def _norm_how(value):
    how = str(value or "").strip().lower()
    return how if how in VALID_HOWS else "subtle"


def _ledger_entry(ledger, ref):
    """Resolve an analyst keep_true reference to a ledger entry, by id or name."""
    if not isinstance(ledger, list):
        return None
    key = str(ref or "").strip().lower()
    if not key:
        return None
    for e in ledger:
        if not isinstance(e, dict):
            continue
        if str(e.get("id", "")).strip().lower() == key:
            return e
    for e in ledger:
        if not isinstance(e, dict):
            continue
        if str(e.get("name", "")).strip().lower() == key:
            return e
    return None


def _keep_true_line(entry):
    """The authoritative 'name: state' line the narrator sees for a ledger entry."""
    name = str(entry.get("name") or entry.get("id") or "").strip()
    state = str(entry.get("state") or "").strip()
    return f"{name}: {state}" if state else name


def _mentioned(entry, text):
    """True when the entry's name or id appears in the text."""
    if not text:
        return False
    low = str(text).lower()
    for field in ("name", "id"):
        token = str(entry.get(field) or "").strip().lower()
        if token and token in low:
            return True
    return False


def _recent_text(history, exchanges=RECENT_EXCHANGES):
    """The last N exchanges flattened to text, for pinned-entry matching."""
    if not isinstance(history, list) or not history:
        return ""
    tail = history[-2 * exchanges:] if len(history) > 2 * exchanges else history
    return "\n".join(str(m.get("content", "")) for m in tail if isinstance(m, dict))


def _clamp_keep_true(raw, ledger, player_action, recent_text):
    """C2: ledger ids only, pinned entries named in play first, free text last."""
    pinned, chosen, seen = [], [], set()
    free_text = []

    if isinstance(raw, list):
        for item in raw:
            why = ""
            ref = item
            if isinstance(item, dict):
                ref = item.get("id") or item.get("name")
                why = str(item.get("why", "")).strip()
            entry = _ledger_entry(ledger, ref)
            if entry is None:
                text = str(ref or "").strip()
                if text and len(free_text) < MAX_FREE_TEXT:
                    free_text.append(text[:150])
                continue
            eid = str(entry.get("id"))
            if eid in seen:
                continue
            seen.add(eid)
            chosen.append(entry)

    # Active pinned entries named in the action or the recent play are always kept.
    for e in ledger if isinstance(ledger, list) else []:
        if not isinstance(e, dict) or not e.get("pinned"):
            continue
        if e.get("status") == "closed":
            continue
        eid = str(e.get("id"))
        if eid in seen:
            continue
        if _mentioned(e, player_action) or _mentioned(e, recent_text):
            seen.add(eid)
            pinned.append(e)

    lines = [_keep_true_line(e) for e in pinned + chosen]
    lines += free_text
    return lines[:MAX_KEEP_TRUE]


def clamp_brief(d, allowed_move_ids=None, defer_counts=None, ledger=None,
                player_action="", recent_text=""):
    """Clamp and validate raw dictionary to strict brief schema.

    allowed_move_ids: None (no filter), a set of ids, or a dict id -> scheduled move. With a dict,
    scheduled `how` and `note` are authoritative and every due move is mandatory (B4).
    defer_counts: {move id: consecutive turns deferred}, used to force a move back in.
    ledger: the canon ledger, used to resolve keep_true ids to authoritative lines (C2).
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

    # deferred (B4): {id, reason}, capped
    deferred = []
    deferred_ids = set()
    raw_deferred = d.get("deferred")
    if isinstance(raw_deferred, list):
        for item in raw_deferred:
            if not isinstance(item, dict):
                continue
            did = str(item.get("id", "")).strip()
            if not did or did in deferred_ids:
                continue
            if allowed_move_ids is not None and did not in allowed_move_ids:
                continue
            deferred_ids.add(did)
            deferred.append({"id": did, "reason": str(item.get("reason", "")).strip()[:150]})
            if len(deferred) >= MAX_DEFERRED:
                break

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
            how = _norm_how(m.get("how", "subtle"))
            if isinstance(sched, dict):
                how = _norm_how(sched.get("how", "subtle"))
                note = str(sched.get("note", "")).strip()[:200]
            else:
                note = str(m.get("note", "")).strip()[:200]
            seen.add(mid)
            moves.append({"id": mid, "how": how, "note": note})
            if len(moves) >= 2:
                break

    # B4: every due move is mandatory. One in neither plot_moves nor deferred is added
    # with its scheduled how/note; one deferred MAX_DEFERS times in a row is forced in.
    if isinstance(allowed_move_ids, dict):
        forced = {i for i in deferred_ids if _defer_count(defer_counts, i) >= MAX_DEFERS}
        if forced:
            deferred = [x for x in deferred if x["id"] not in forced]
            deferred_ids -= forced
        for mid, sched in allowed_move_ids.items():
            if len(moves) >= 2:
                break
            if mid in seen or mid in deferred_ids:
                continue
            moves.append({"id": mid, "how": _norm_how(sched.get("how", "subtle")),
                          "note": str(sched.get("note", "")).strip()[:200]})
            seen.add(mid)

    out["plot_moves"] = moves
    out["deferred"] = deferred

    # keep_true (C2: ledger ids resolved to authoritative lines, pinned first)
    out["keep_true"] = _clamp_keep_true(
        d.get("keep_true"), ledger, player_action, recent_text)

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

    # D5: an ask advances nothing, so no move is played and none is deferred.
    if out["intent"] == "ask":
        out["plot_moves"] = []
        out["deferred"] = []
    return out


def make_fallback_brief(reason="fallback", allowed_moves=None, ledger=None, player_action="",
                        recent_text=""):
    """Built brief for a failed analyst call. Plays the due moves in code (B5, C3)."""
    reason = reason if reason in FALLBACK_REASONS else "analyst_error"
    brief = _Brief(copy.deepcopy(BRIEF_SCHEMA),
                   diagnostics={"fallback_reason": reason})
    brief["narration"] = {
        "words": cfg.style_words_max if cfg.style_words_max > 0 else 140,
        "push": round((cfg.style_push / 100.0) if cfg.style_push is not None else 0.6, 2),
        "end": "decision",
    }
    brief["assessment"] = "Action proceeds."
    played = []
    for m in (allowed_moves or [])[:2]:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        played.append({"id": str(m["id"]), "how": _norm_how(m.get("how", "subtle")),
                       "note": str(m.get("note", "")).strip()[:200]})
    brief["plot_moves"] = played
    brief["keep_true"] = _clamp_keep_true([], ledger, player_action, recent_text)
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
    '  "plot_moves": [{"id": "move_id", "how": "overt|subtle|offscreen|offer", "note": "what player perceives"}],\n'
    '  "deferred": [{"id": "move_id", "reason": "why it waits"}],\n'
    '  "keep_true": [{"id": "ledger_id", "why": "what must stay true"}],\n'
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
    "- Every allowed move is MANDATORY: play it, or list it in 'deferred' with a reason.\n"
    "- A move deferred twice in a row is played whether you defer it again or not.\n"
    "- 'how' is set by the scheduler; never change it. 'offer' means a clear in-world opening.\n"
    "- 'keep_true' takes ledger ids only. An id not in the ledger is dropped.\n"
    "- Authority on conflict: ledger beats summary beats recent play.\n"
)


def _truncate_tok(text, budget):
    """Keep the newest `budget` tokens of text, cutting oldest-first (C1)."""
    if not text:
        return ""
    limit = max(200, int(budget)) * max(0.5, float(cfg.chars_per_tok))
    limit = int(limit)
    if len(text) <= limit:
        return text
    return text[-limit:]


def _is_retryable(exc):
    """C4: retry once on 429/5xx, mirroring dm.ApiError.retry."""
    if isinstance(exc, concurrent.futures.TimeoutError):
        return False
    if getattr(exc, "retry", False) is True:
        return True
    text = str(exc)
    return text.startswith("HTTP 5") or text.startswith("HTTP 429")


def run_analyst(root, history, player_action, allowed_moves, style_guard_list, chat_fn,
                ledger_text="", act_goal="", steering_text="", defer_counts=None,
                premise="", summary="", hooks=None, previous_brief=None, ledger=None):
    """Run analyst model call with timeout, one retry, and fallback (C1, C4)."""
    model = cfg.analyst_model or cfg.scribe_model or cfg.model
    allowed_ids = {m["id"]: m for m in allowed_moves} if allowed_moves else {}
    recent_text = _recent_text(history)

    n_turns = cfg.analyst_context_turns
    h_tail = history[-2 * n_turns:] if len(history) > 2 * n_turns else history
    play_str = "\n".join(f"[{i}] {'PLAYER' if m['role'] == 'user' else 'DM'}: {m['content']}" for i, m in enumerate(h_tail))

    user_prompt = (
        f"DIFFICULTY: {cfg.difficulty}\n"
        f"STYLE KNOBS: preset={cfg.style_preset}, words={cfg.style_words_min}-{cfg.style_words_max}, push={cfg.style_push}\n"
        f"ALLOWED PLOT MOVES: {json.dumps(allowed_moves or [], ensure_ascii=False)}\n"
        f"STYLE GUARD (avoid): {json.dumps(style_guard_list or [], ensure_ascii=False)}\n"
        + (f"PREMISE:\n{premise}\n" if premise else "")
        + (f"CURRENT ACT GOAL: {act_goal}\n" if act_goal else "")
        + (f"STORY SO FAR:\n{_truncate_tok(summary, cfg.analyst_summary_tok)}\n" if summary else "")
        + (f"PENDING HOOKS: {json.dumps(hooks, ensure_ascii=False)}\n" if hooks else "")
        + (f"PREVIOUS BRIEF: {json.dumps(previous_brief, ensure_ascii=False)}\n" if previous_brief else "")
        + (f"CANON LEDGER:\n{ledger_text}\n" if ledger_text else "")
        + (f"CURRENT STEERING (player-facing):\n{steering_text}\n" if steering_text else "")
        + "\n"
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
    attempts = max(0, int(cfg.analyst_retries)) + 1

    def attempt():
        """One analyst call bounded by the remaining timeout budget (C4)."""
        remaining = timeout - (time.time() - t0)
        if remaining <= 0:
            raise concurrent.futures.TimeoutError()
        # No `with` block: its exit calls shutdown(wait=True) and would sit there until the
        # model call finishes, which defeats the timeout (D-14).
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(chat_fn, messages, model=model, track=False, retries=0, fallback=False)
            return future.result(timeout=remaining)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    try:
        raw = None
        for n in range(attempts):
            try:
                raw = attempt()
                break
            except concurrent.futures.TimeoutError:
                raise
            except Exception as e:
                if n == attempts - 1 or not _is_retryable(e):
                    raise
        parsed = parse_json_tolerantly(raw)
        if not parsed:
            raise ValueError("Unparseable JSON from analyst")
        brief = clamp_brief(parsed, allowed_ids, defer_counts, ledger=ledger,
                            player_action=player_action, recent_text=recent_text)
        ms = int((time.time() - t0) * 1000)
        return brief, "analyst", model, ms
    except concurrent.futures.TimeoutError:
        brief = make_fallback_brief(reason="timeout", allowed_moves=allowed_moves, ledger=ledger,
                                    player_action=player_action, recent_text=recent_text)
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms
    except ValueError:
        brief = make_fallback_brief(reason="invalid_response", allowed_moves=allowed_moves, ledger=ledger,
                                    player_action=player_action, recent_text=recent_text)
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms
    except Exception:
        brief = make_fallback_brief(reason="analyst_error", allowed_moves=allowed_moves, ledger=ledger,
                                    player_action=player_action, recent_text=recent_text)
        ms = int((time.time() - t0) * 1000)
        return brief, "fallback", model, ms


# ---------------------------------------------------------------------------
# OOC Notes responder
# ---------------------------------------------------------------------------

OOC_SYSTEM = (
    "You are the Dungeon Master responding OUT OF CHARACTER (OOC) to a player note.\n"
    "ROLE: Answer the player's question or clarification directly, plainly, and honestly.\n"
    "\n"
    "INPUTS: PREMISE, SUMMARY, the CANON LEDGER, CURRENT STEERING and RECENT PLAY. On conflict\n"
    "the ledger beats the summary, and the summary beats recent play.\n"
    "\n"
    "TASK: Answer in at most 120 words. Never advance the story, roll dice, or narrate in\n"
    "character.\n"
    "\n"
    "OUTPUT: The answer as plain prose. If, and only if, the player is proposing a canon fact\n"
    "or world detail, end with this block:\n"
    '```json\n{"canon_proposal": {"id": "slug", "kind": "person|place|item|rule", "name": "...", "state": "..."}}\n```\n'
    "Output only the answer and, when it applies, that one JSON block. No code fences around\n"
    "the prose, no commentary.\n"
    "\n"
    "RULES:\n"
    "1. Never invent a fact. If something is not established, say so plainly.\n"
    "2. When the player seems to want something that is not established, offer a canon\n"
    "   proposal rather than asserting it.\n"
    "3. Use the canonical names from the ledger.\n"
    "4. Do not hint at anything the player has not been shown.\n"
    "\n"
    "WHEN UNSURE: say it is not established, and offer a canon proposal."
)


def handle_ooc_note(root, note_text, history, premise_text, summary_text, ledger_text, chat_fn,
                    steering_text=""):
    """Answer an OOC note, save to notes.jsonl, and return answer text."""
    clean_q = strip_ooc_prefix(note_text)
    model = cfg.analyst_model or cfg.scribe_model or cfg.model

    h_tail = history[-12:] if len(history) > 12 else history
    play_str = "\n".join(f"{'PLAYER' if m['role'] == 'user' else 'DM'}: {m['content']}" for m in h_tail)

    prompt = (
        f"PREMISE:\n{premise_text}\n\n"
        f"SUMMARY:\n{summary_text}\n\n"
        f"CANON LEDGER:\n{ledger_text}\n\n"
        + (f"CURRENT STEERING (player-facing):\n{steering_text}\n\n" if steering_text else "")
        + f"RECENT PLAY:\n{play_str}\n\n"
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