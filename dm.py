#!/usr/bin/env python3
"""
dm.py v3 — lean chat-based RPG dungeon master (terminal + web UI).

Doctrine: files are the source of truth; context is assembled by code. With a 1M-token model the narrator
sees the FULL verbatim history; summaries and the ledger are an index and a safety net, not a replacement.

  campaigns/settings.json   all settings (edit on /config in the web UI, or by hand)
  campaigns/<name>/
    history.jsonl   append-only log (/undo and /retry move old text to undone.jsonl)
    premise.md      pinned canon (seeded from your first message; edit freely)
    summary.md      chapters of folded play (appended, never rewritten)
    ledger.json     keyed canon state: [{id, kind, name, state, status, src, msg}]
    audit.md        latest continuity audit
    state.json      {"folded": N, "ledgered": M, "audited": K}

Prompt = [system: persona + premise (+ chapters if folded)] + verbatim history since `folded`
         + the player's action, with the canon ledger appended to that last message.
Everything before the last message is append-only, so provider prefix caching can hit.

  python dm.py                      terminal play (/undo /retry /context /ledger /audit /rebuild /quit)
  python dm.py --web                web UI + /config page
  python dm.py --campaign noir
  python dm.py --show-context       print what the next turn would send, then exit
  python dm.py --rebuild-ledger     rebuild ledger.json from the raw history
  python dm.py --audit              run the continuity audit
  python dm.py --reset-fold         migrate a v2 campaign: archive summary/facts, folded=0, rebuild ledger

Dependencies: pip install flask requests
"""

import hmac
import html
import io
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import threading
import time
import zipfile
from collections import defaultdict, deque
from time import monotonic
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import analyst
import arc
from campaign import Campaign
from config import SETTINGS_PATH, cfg
from ledger import KINDS as KINDS
from ledger import _lst as _lst
from ledger import apply_ledger_ops, render_ledger
from ledger import slug as slug
from narrator_context import assemble_context
from narrator_context import merge_same_role as merge_same_role
from storage import append_jsonl, atomic_write, clean_reply, parse_json_response
from style_guard import compute_style_guard

logger = logging.getLogger(__name__)

# ---------------- paths ----------------

CAMPAIGN = cfg.campaign
if "--campaign" in sys.argv:
    CAMPAIGN = re.sub(r"[^\w.\-]", "_", sys.argv[sys.argv.index("--campaign") + 1]) or "default"

def bind_campaign(campaigns_dir=None, name=None):
    """Set the active campaign and refresh compatibility aliases used by commands/tests."""
    global CAMPAIGN_CONTEXT, CAMPAIGN, CAMPAIGNS_DIR, ROOT
    global HISTORY, UNDONE, PREMISE, SUMMARY, LEDGER, AUDIT, STATE
    global TURN_LOCK, MAINT_LOCK, STATE_LOCK, JOB_LOCK
    if isinstance(campaigns_dir, Campaign):
        context = campaigns_dir
    else:
        base = campaigns_dir or os.environ.get("DM_CAMPAIGNS_DIR")
        if not base:
            base = pathlib.Path(__file__).parent / "campaigns"
        context = Campaign(base, name or CAMPAIGN)
    previous = globals().get("CAMPAIGN_CONTEXT")
    thread = globals().get("MAINT_THREAD")
    if previous is not None and previous is not context and thread and thread.is_alive():
        thread.join()
    CAMPAIGN_CONTEXT = context
    CAMPAIGN = context.name
    CAMPAIGNS_DIR, ROOT = context.campaigns_dir, context.root
    HISTORY, UNDONE = context.history, context.undone
    PREMISE, SUMMARY = context.premise, context.summary
    LEDGER, AUDIT, STATE = context.ledger, context.audit, context.state
    TURN_LOCK, MAINT_LOCK = context.turn_lock, context.maintenance_lock
    STATE_LOCK, JOB_LOCK = context.state_lock, context.job_lock
    cfg.set_campaign_path(context.root / "campaign.json")
    return context


CAMPAIGN_CONTEXT = bind_campaign()

MAINT_THREAD = None
JOB = {"name": "", "running": False, "msg": ""}
LAST = {"est": 0, "prompt_tokens": None, "cached": None}
POST_COUNTS = defaultdict(lambda: deque())
AUTH_FAILURES = {}
AUTH_FAILURE_LOCK = threading.Lock()
LEDGER_LOCK = threading.RLock()

RETRY_CODES = (408, 425, 429, 500, 502, 503, 504)


def validate_config():
    missing = [k for k in ("api_key", "model") if not getattr(cfg, k)]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}\nRun `python dm.py --web` and open /config, "
                 f"or edit {SETTINGS_PATH}")


def ensure_configured():
    if not cfg.api_key or not cfg.model:
        raise RuntimeError(f"model / API key not set — open /config in the web UI or edit {SETTINGS_PATH}")


def print_config():
    key = cfg.api_key[:6] + "…" if len(cfg.api_key) > 6 else ("***" if cfg.api_key else "(none)")
    print(f"settings={SETTINGS_PATH}")
    print(f"endpoint={cfg.base_url}  key={key}")
    print(f"model={cfg.model or '(none)'}  scribe={cfg.scribe_model or 'same'}  fallback={cfg.fallback_model or 'none'}")
    print(f"campaign='{CAMPAIGN}'  fold {cfg.ctx_budget_tok // 1000}k→{cfg.ctx_target_tok // 1000}k tok  "
          f"ledger={'on' if cfg.ledger_enabled else 'off'}")


# ---------------- storage helpers ----------------

_warned = set()
def warn(msg):
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)

def read(path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""

def raw_history_lines():
    return [line for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()]

def load_history():
    out = []
    for n, line in enumerate(HISTORY.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            m = json.loads(line)
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                out.append(m)
                continue
        except (json.JSONDecodeError, AttributeError):
            pass
        warn(f"history.jsonl line {n} is unreadable and is being skipped")
    return out

def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0

def get_state():
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (ValueError, OSError):
        return {}

def set_state(**kw):
    with STATE_LOCK:
        s = get_state()
        s.update(kw)
        atomic_write(STATE, json.dumps(s))

def folded():
    return _int(get_state().get("folded"))

def ledgered():
    return _int(get_state().get("ledgered"))

def load_ledger(strict=False):
    try:
        d = json.loads(LEDGER.read_text(encoding="utf-8"))
        if not isinstance(d, list):
            raise ValueError("expected a JSON array")
        return [e for e in d if isinstance(e, dict) and e.get("id")]
    except (ValueError, OSError) as e:
        if strict:
            raise                                       # never overwrite a hand edit
        warn(f"ledger.json unreadable ({e}); ignoring it until it is fixed")
        return []

def init_files():
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg.set_campaign_path(ROOT / "campaign.json")
    for p in (HISTORY, UNDONE, SUMMARY):
        if not p.exists():
            p.write_text("", encoding="utf-8")
    if not LEDGER.exists():
        LEDGER.write_text("[]", encoding="utf-8")
    history = load_history()
    if not STATE.exists():                              # new campaign, or a pre-state one
        n = max(0, len(history) - 2 * cfg.keep_min_turns) if read(SUMMARY) else 0
        atomic_write(STATE, json.dumps({"folded": n - n % 2, "ledgered": 0}))
    if history and not read(PREMISE):
        first = next((m["content"] for m in history if m["role"] == "user"), "")
        if first:
            atomic_write(PREMISE, first)


# ---------------- tokens, ledger, context ----------------

def tok(n_chars):
    return int(n_chars / max(0.5, cfg.chars_per_tok))

def est_tokens(msgs):
    return tok(sum(len(m["content"]) for m in msgs))

def build_context(history, brief=None):
    """Build the narrator prompt for history ending in the pending player action."""
    k = folded()
    return assemble_context(
        history, brief, folded_count=k, premise=read(PREMISE),
        summary=read(SUMMARY) if k else "",
        canon=render_ledger(load_ledger()) if cfg.ledger_enabled else "",
    )

def ctx_label():
    est, real = LAST["est"], LAST["prompt_tokens"]
    if not est:
        return ""
    s = f"context ≈{est / 1000:.1f}k tok"
    if real:
        s += f" (provider {real / 1000:.1f}k"
        if LAST["cached"]:
            s += f", {100 * LAST['cached'] // real}% cached"
        s += ")"
    return s

def show_context():
    h = load_history()
    ctx = build_context(h + [{"role": "user", "content": "<your next action>"}])
    for m in ctx:
        print(f"--- {m['role']} ---\n{m['content']}\n")
    print(f"[≈{est_tokens(ctx)} tokens · {len(h) - min(folded(), len(h))} of {len(h)} messages verbatim]")


# ---------------- model calls ----------------

class ApiError(RuntimeError):
    def __init__(self, msg, retry=False, wait=None):
        super().__init__(msg)
        self.retry, self.wait = retry, wait

TRANSIENT = (requests.ConnectionError, requests.Timeout, ApiError)

def _raw_post(payload, stream=False):
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    r = requests.post(url, headers={"Authorization": f"Bearer {cfg.api_key}"}, json=payload,
                      timeout=(10, cfg.timeout_sec), stream=stream)
    if r.status_code >= 400:
        wait = None
        try:
            wait = min(120.0, float(r.headers.get("Retry-After")))
        except (TypeError, ValueError):
            pass
        raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}", retry=r.status_code in RETRY_CODES, wait=wait)
    return r

def _with_retries(fn, retries=None):
    n = (cfg.retries if retries is None else retries) + 1
    for attempt in range(n):
        try:
            return fn()
        except TRANSIENT as e:
            if attempt == n - 1 or (isinstance(e, ApiError) and not e.retry):
                raise
            wait = (e.wait if isinstance(e, ApiError) and e.wait else None) or min(90.0, cfg.retry_base_sec * 2 ** attempt)
            logger.info("API error (%s); retry %s/%s in %.0fs",
                        str(e)[:120], attempt + 1, n - 1, wait)
            time.sleep(wait)

def _with_fallback(call, primary, fallback=True):
    try:
        return call(primary)
    except TRANSIENT as e:
        fb = cfg.fallback_model
        if fallback and fb and fb != primary:
            logger.info("%s failed (%s); trying fallback %s", primary, str(e)[:100], fb)
            return call(fb)
        raise

def note_usage(u):
    if isinstance(u, dict) and u.get("prompt_tokens"):
        LAST["prompt_tokens"] = u["prompt_tokens"]
        d = u.get("prompt_tokens_details")
        LAST["cached"] = d.get("cached_tokens") if isinstance(d, dict) else None

def narrator_extra():
    x = {}
    if cfg.narrator_temp.strip():
        x["temperature"] = float(cfg.narrator_temp)
    if cfg.narrator_max_tokens > 0:
        x["max_tokens"] = cfg.narrator_max_tokens
    return x

def scribe_extra():
    return {"temperature": float(cfg.scribe_temp)} if cfg.scribe_temp.strip() else {}

def scribe_model():
    return cfg.scribe_model or cfg.model

def chat_complete(messages, model=None, *, track=True, retries=None, fallback=True, **extra):
    def call(m):
        def go():
            r = _raw_post({"model": m, "messages": messages, "stream": False, **extra})
            try:
                data = r.json()
            except ValueError:
                raise ApiError("non-JSON response", retry=True)
            if data.get("error"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                raise ApiError(f"API error: {str(err)[:300]}", retry=code in RETRY_CODES)
            if track:
                note_usage(data.get("usage"))
            msg = ((data.get("choices") or [{}])[0].get("message")) or {}
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ApiError("model returned an empty reply", retry=True)
            return content
        return _with_retries(go, retries)
    return _with_fallback(call, model or cfg.model, fallback)

def chat_stream(messages):
    """Stream the narrator's reply to the terminal; returns the full text."""
    def open_(m):
        payload = {"model": m, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}, **narrator_extra()}
        return _with_retries(lambda: _raw_post(payload, stream=True))
    r = _with_fallback(open_, cfg.model)
    text = ""
    try:
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            payload = line[6:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"API error mid-stream: {str(chunk['error'])[:300]}")
            note_usage(chunk.get("usage"))
            choices = chunk.get("choices") or []
            piece = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
            text += piece
            print(piece, end="", flush=True)
    finally:
        r.close()
    if not text.strip():
        raise RuntimeError("model returned an empty reply")
    return text


# ---------------- maintenance: ledger, fold, audit ----------------

def fmt_play(h, a, b):
    return "\n".join(f"[{i}] {'PLAYER' if h[i]['role'] == 'user' else 'DM'}: {h[i]['content']}" for i in range(a, b))

def split_ranges(h, a, end):
    """Cut h[a:end] into whole-exchange ranges of at most ~scribe_chunk_tok tokens."""
    out = []
    while a < end:
        b, total = a, 0
        while b < end and total < cfg.scribe_chunk_tok:
            total += tok(len(h[b]["content"]))
            b += 1
        if (b - a) % 2:
            b += 1
        b = max(min(b, end), a + 1)
        out.append((a, b))
        a = b
    return out

def parse_json_obj(raw):
    value = parse_json_response(raw)
    if not isinstance(value, dict):
        raise ValueError("model response did not contain a JSON object")
    return value

LEDGER_PROMPT = (
    "Update the canon ledger from the new play. Return ONLY JSON: "
    '{"upsert": [entry...], "close": [ids], "delete": [ids]}.\n'
    'entry = {"id": stable-lowercase-slug, "kind": "player|person|place|item|promise|clock|rule|thread", '
    '"name": "...", "state": "one line: the CURRENT truth", "src": "player|dm|note", '
    '"msg": index of the message where it last changed}\n'
    "Rules:\n"
    "- One entry per entity/thing. Reuse the existing id to update it; never create a second entry for the same thing.\n"
    "- Durable canon only: who/where/what-state, exact counts, days, deadlines, ownership, promises, debts, injuries, "
    "decisions the player declared. Skip moment-to-moment action and atmosphere.\n"
    "- Be specific: keep numbers, names, locations. Characters who matter must keep an entry with their last known "
    "location and open business, even if they have been offscreen for a long time.\n"
    "- Player messages starting with 'note to dm', 'ooc' or in parentheses are out-of-character corrections: apply "
    "them and set src to 'note'.\n"
    "- If the DM contradicts an established value without the story changing it, keep the established value and add "
    "'(DM said X at [n])' to the state.\n"
    "- 'close' = resolved, dead, finished or obsolete (kept as history). 'delete' only for entries that were mistakes.")

def ledger_pass(upto=None):
    """Advance the ledger over history[ledgered:upto]. Caller holds MAINT_LOCK."""
    h = load_history()
    ptr = ledgered()
    if upto is None:                                    # scheduled pass: trail the newest exchanges
        upto = len(h) - 2 * cfg.ledger_lag_turns
        if upto - ptr < 2 * cfg.ledger_every_turns:
            return
    upto = min(upto, len(h))
    upto -= upto % 2
    if upto <= ptr:
        return
    ledger = load_ledger(strict=True)
    for a, b in split_ranges(h, ptr, upto):
        raw = clean_reply(chat_complete([
            {"role": "system", "content": "You maintain the canon ledger of a text RPG. Output only JSON."},
            {"role": "user", "content": "CURRENT LEDGER:\n" + json.dumps(ledger, ensure_ascii=False) +
             "\n\nNEW PLAY (message index in brackets):\n" + fmt_play(h, a, b) + "\n\n" + LEDGER_PROMPT}],
            model=scribe_model(), track=False, **scribe_extra()))
        ops = parse_json_obj(raw)
        with LEDGER_LOCK:                               # the model call took minutes; edits made meanwhile must survive
            ledger = apply_ledger_ops(load_ledger(strict=True), ops)
            atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
        set_state(ledgered=b)                           # pointer LAST, per range
        logger.info("Ledger recorded messages %s–%s; %s entries", a, b - 1, len(ledger))

def fold_if_needed():
    """Once the verbatim tail passes the budget, fold its oldest part into chapters. Caller holds MAINT_LOCK."""
    h, k = load_history(), folded()
    tail = h[k:]
    total = est_tokens(tail)
    if total < cfg.ctx_budget_tok:
        return
    keep, n = 2 * cfg.keep_min_turns, 0
    while total > cfg.ctx_target_tok and n < len(tail) - keep:
        total -= tok(len(tail[n]["content"]))
        n += 1
    n -= n % 2
    if n < 2:
        return
    end = k + n
    if cfg.ledger_enabled:
        ledger_pass(upto=end)                           # never fold what the ledger has not seen
    ref = render_ledger(load_ledger())
    summary = read(SUMMARY)
    for a, b in split_ranges(h, k, end):
        header = f"## Messages {a}–{b - 1}"
        if header in summary:                           # crash recovery: chapter already written
            continue
        words = sum(len(m["content"].split()) for m in h[a:b])
        target = max(250, min(2500, words // cfg.summary_ratio))
        chapter = clean_reply(chat_complete([
            {"role": "system", "content": "You write the chronicle of a text RPG campaign."},
            {"role": "user", "content":
             "REFERENCE (current canon ledger, for names and spellings):\n" + ref +
             "\n\nNEW PLAY (message index in brackets):\n" + fmt_play(h, a, b) +
             f"\n\nWrite the next chapter of the chronicle covering ONLY this play, about {target} words. "
             "Chronological and concrete: who did what, where, exact numbers, names, decisions the player made, promises, "
             "debts, what changed state. Keep long-running characters visible. End with threads still open at the end "
             "of this play. Out-of-character 'note to dm' lines are corrections: write the corrected version."}],
            model=scribe_model(), track=False, **scribe_extra()))
        if len(chapter) < 50:
            raise ValueError("scribe returned an empty or too-short chapter")
        summary = (summary + "\n\n" if summary else "") + header + "\n" + chapter
        atomic_write(SUMMARY, summary)
    set_state(folded=end)                               # pointer LAST: a crash can duplicate, never lose
    logger.info("Folded messages %s–%s into chapters", k, end - 1)

def _audit():
    h = load_history()
    start, total = len(h), 0
    while start > 0 and total + tok(len(h[start - 1]["content"])) <= cfg.audit_max_tok:
        start -= 1
        total += tok(len(h[start]["content"]))
    start += start % 2
    if start >= len(h):
        raise RuntimeError("nothing to audit")
    report = clean_reply(chat_complete([
        {"role": "system", "content": "You are the continuity editor of a long-running text RPG."},
        {"role": "user", "content":
         "PREMISE:\n" + read(PREMISE) + "\n\nCANON LEDGER:\n" + (render_ledger(load_ledger()) or "(empty)") +
         "\n\nPLAY (message index in brackets):\n" + fmt_play(h, start, len(h)) +
         "\n\nWrite a markdown report with message indices, in these sections:\n"
         "1. CONTRADICTIONS — numbers, counts, names, locations, ownership, timeline/day counts. Give both sides with "
         "indices and say which is likely correct.\n"
         "2. LEDGER ERRORS — entries that are wrong or stale versus the play, with corrected text.\n"
         "3. DROPPED THREADS — characters, promises, clocks, objects introduced but not seen again (last index seen).\n"
         "4. NARRATOR HABITS — repeated phrases, imagery, structures; the DM acting or deciding for the player.\n"
         "5. TOP 10 FIXES, ranked. Be specific; no filler."}],
        model=scribe_model(), track=False, **scribe_extra()))
    atomic_write(AUDIT, f"# Continuity audit — {time.strftime('%Y-%m-%d %H:%M')}\n"
                        f"Covers messages {start}–{len(h) - 1} of {len(h)}\n\n{report}")
    set_state(audited=len(h))
    logger.info("Audit written to %s", AUDIT)

def run_audit():
    with MAINT_LOCK:
        _audit()

def rebuild_ledger():
    with MAINT_LOCK:
        h = load_history()
        if LEDGER.exists() and read(LEDGER) not in ("", "[]"):
            shutil.copy(LEDGER, ROOT / f"ledger.{int(time.time())}.bak.json")
        with LEDGER_LOCK:
            atomic_write(LEDGER, "[]")
        set_state(ledgered=0)
        upto = max(0, len(h) - 2 * cfg.ledger_lag_turns)
        ledger_pass(upto=upto - upto % 2)

def reset_fold():
    """Migrate a v2 campaign: archive summary/facts, show the full history again."""
    legacy = ROOT / "legacy"
    legacy.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if read(SUMMARY):
        shutil.copy(SUMMARY, legacy / f"summary.{stamp}.md")
    facts = ROOT / "facts.json"
    if facts.exists():
        shutil.move(str(facts), str(legacy / f"facts.{stamp}.json"))
    atomic_write(SUMMARY, "")
    set_state(folded=0, ledgered=0)
    logger.info("Archived v2 summary/facts in %s; folded=0", legacy)

def maintain():
    lock = MAINT_LOCK
    if not lock.acquire(blocking=False):                # single-flight
        return
    try:
        if cfg.ledger_enabled:
            ledger_pass()
        fold_if_needed()
        if cfg.audit_every_turns:
            n = len(load_history())
            if n - _int(get_state().get("audited")) >= 2 * cfg.audit_every_turns:
                _audit()
    except Exception as e:
        logger.warning("Maintenance failed; will retry next turn: %s", e)
    finally:
        lock.release()

def start_maintain():
    global MAINT_THREAD
    MAINT_THREAD = threading.Thread(target=maintain, daemon=True)
    MAINT_THREAD.start()

def start_job(name, fn):
    with JOB_LOCK:
        if JOB["running"]:
            raise RuntimeError(f"'{JOB['name']}' is still running")
        JOB.update(name=name, running=True, msg="started")
    def run():
        try:
            fn()
            msg = "done"
        except Exception as e:
            msg = f"failed: {e}"
        JOB.update(running=False, msg=msg)
    threading.Thread(target=run, daemon=True).start()


# ---------------- game actions (shared by CLI and web) ----------------

def _generate(ctx, stream):
    ensure_configured()
    LAST.update(est=est_tokens(ctx), prompt_tokens=None, cached=None)
    dm = clean_reply(chat_stream(ctx) if stream else chat_complete(ctx, **narrator_extra()))
    if not dm:
        raise RuntimeError("model returned an empty reply")
    return dm

def _generate_stream(ctx):
    """Stream a narrator reply while preserving retries, fallback, usage, and cleaning."""
    ensure_configured()
    LAST.update(est=est_tokens(ctx), prompt_tokens=None, cached=None)

    def open_(model):
        payload = {"model": model, "messages": ctx, "stream": True,
                   "stream_options": {"include_usage": True}, **narrator_extra()}
        return _with_retries(lambda: _raw_post(payload, stream=True))

    r = _with_fallback(open_, cfg.model)
    full = ""
    try:
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            payload = line[6:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"API error mid-stream: {str(chunk['error'])[:300]}")
            note_usage(chunk.get("usage"))
            choices = chunk.get("choices") or []
            piece = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
            if piece:
                full += piece
                yield {"chunk": piece}
    finally:
        r.close()
    dm = clean_reply(full)
    if not dm:
        raise RuntimeError("model returned an empty reply")
    yield {"done": True, "dm": dm}

def _act_goal():
    if not cfg.arc_enabled:
        return ""
    arc_state = arc.load_arc(ROOT)
    if not arc_state:
        return ""
    act = next((a for a in arc_state.get("acts", []) if a.get("n") == arc_state.get("act", 1)), {})
    return str(act.get("goal") or "")

def get_allowed_moves(history=None):
    if not cfg.arc_enabled:
        return []
    try:
        arc_state = arc.load_arc(ROOT)
        if not arc_state:
            return []
        h = load_history() if history is None else history
        turn = len(h) // 2 + 1
        rt = arc_state.get("runtime") or {}
        return arc.allowed_moves(
            arc_state, cfg.difficulty, turn,
            last_overt_turn=rt.get("last_overt_turn"),
            stalled_turns=_int(rt.get("stalled_turns")),
        )
    except Exception as e:
        logger.warning("Could not compute allowed plot moves; continuing without: %s", e)
        return []

def _record_kwargs(brief):
    """Return the record-pass arguments derived from the analyst brief."""
    if not brief:
        return {}
    return {
        "played_moves": brief.get("plot_moves") or [],
        "intent": brief.get("intent", "act"),
        "analyst_act_complete": bool(brief.get("act_complete", False)),
    }

def _arc_step(text, dm, msg_idx, history, user, played_history, brief, retry):
    """Auto-plan a missing arc, then run the record pass for a committed turn."""
    if not retry and arc.arc_is_corrupt(ROOT):
        warn("arc.json exists but is unreadable; the arc is paused until you fix or delete it")
    elif not retry and not arc.load_arc(ROOT):
        try:
            new_arc = arc.plan_initial_arc(
                read(PREMISE), read(SUMMARY), render_ledger(load_ledger()),
                history + [user, {"role": "assistant", "content": dm}],
                chat_complete
            )
            arc.save_arc(ROOT, new_arc)
        except Exception as e:
            logger.warning("Arc auto-planning failed: %s", e)
    try:
        arc.run_record_pass(ROOT, text, dm, msg_idx, chat_complete,
                            history=played_history, **_record_kwargs(brief))
    except Exception as e:
        logger.warning("Arc record pass failed; the arc did not advance for message %s: %s", msg_idx, e)
    try:
        arc.prune_arc_snapshots(ROOT, max(folded(), ledgered()))
    except OSError as e:
        logger.warning("Could not prune arc snapshots: %s", e)

def run_turn(text="", stream=False, lock_acquired=False, retry=False, rerun_analyst=False):
    """Single turn engine shared by CLI, web turns, streaming turns, and retries."""
    if not lock_acquired and not TURN_LOCK.acquire(blocking=False):
        raise RuntimeError("turn already in progress")
    elif not lock_acquired:
        lock_acquired = True
    arc_backup = None
    committed = False
    maintain_needed = False
    try:
        if retry:
            lines, history = raw_history_lines(), load_history()
            _last_exchange(lines, history)
            text = history[-2]["content"]
            if cfg.arc_enabled:
                arc_backup = arc.backup_state(ROOT)
                arc.restore_arc_snapshot(ROOT, len(history) - 1)
                arc.truncate_records(ROOT, len(history) - 1)
                arc.truncate_arc_log(ROOT, len(history) - 1)
            if stream and cfg.analyst_enabled and rerun_analyst:
                yield {"stage": "analyzing"}
            brief = _retry_brief(history, rerun_analyst)
            user = history[-2]
            msg_idx = len(history) - 1
            played_history = history[:-1]
            ctx = build_context(history[:-1], brief=brief)
        else:
            history = load_history()
            if cfg.notes_routing and analyst.is_ooc_note(text):
                entry = analyst.handle_ooc_note(
                    ROOT, text, history, read(PREMISE), read(SUMMARY),
                    render_ledger(load_ledger()), chat_complete
                )
                yield {"chunk": entry["a"], "ooc": True}
                yield {"done": True, "dm": entry["a"], "ooc": True}
                return

            brief = None
            if cfg.analyst_enabled:
                if stream:
                    yield {"stage": "analyzing"}
                allowed = get_allowed_moves()
                guard_items = compute_style_guard(history) if cfg.style_guard else []
                brief, source, model_used, ms = analyst.run_analyst(
                    ROOT, history, text, allowed, guard_items, chat_complete,
                    ledger_text=render_ledger(load_ledger()), act_goal=_act_goal()
                )
                analyst.save_brief(ROOT, len(history), source, model_used, ms, brief)

            user = {"role": "user", "content": text}
            msg_idx = len(history) + 1
            played_history = history + [user]
            ctx = build_context(history + [user], brief=brief)

        if stream:
            yield {"stage": "narrating"}
            dm = None
            for event in _generate_stream(ctx):
                yield event
                if event.get("done"):
                    dm = event["dm"]
        else:
            dm = _generate(ctx, stream=False)
        if not dm:
            raise RuntimeError("model returned an empty reply")

        if retry:
            new = json.dumps({"role": "assistant", "content": dm}, ensure_ascii=False)
            append_jsonl(UNDONE, lines[-1:])
            atomic_write(HISTORY, "".join(line + "\n" for line in lines[:-1]) + new + "\n")
        else:
            append_jsonl(HISTORY, [user, {"role": "assistant", "content": dm}])
            if not history and not read(PREMISE):
                atomic_write(PREMISE, text)
        committed = True
        maintain_needed = True
        played_history = played_history + [{"role": "assistant", "content": dm}]

        words = len(dm.split())
        overlong = bool(cfg.style_words_max > 0 and words > int(1.5 * cfg.style_words_max))
        set_state(last_words=words, overlong=overlong)

        if cfg.arc_enabled:
            if stream:
                try:
                    yield {"stage": "recording"}
                except GeneratorExit:                   # client left: the turn is committed, keep the arc in step
                    _arc_step(text, dm, msg_idx, history, user, played_history, brief, retry)
                    raise
            _arc_step(text, dm, msg_idx, history, user, played_history, brief, retry)
        yield {"done": True, "dm": dm}
    except BaseException:
        if retry and not committed and arc_backup is not None:
            arc.restore_state(ROOT, arc_backup)
        raise
    finally:
        TURN_LOCK.release()
        if maintain_needed:
            start_maintain()

def player_turn(text, stream=False):
    dm = None
    for event in run_turn(text, stream=stream):
        if stream and event.get("chunk"):
            print(event["chunk"], end="", flush=True)
        if event.get("done"):
            dm = event["dm"]
    return dm

def _last_exchange(lines, h):
    if len(lines) != len(h):
        raise RuntimeError("history.jsonl has unreadable lines; fix it by hand first")
    if len(h) < 2 or h[-1]["role"] != "assistant" or h[-2]["role"] != "user":
        raise RuntimeError("nothing to undo")
    if len(h) - 2 < max(folded(), ledgered()):
        raise RuntimeError("that exchange is already folded into the summary or ledger; can't rewind past it")

def undo_turn():
    """Remove the last exchange (kept in undone.jsonl). Returns the player's text."""
    if not TURN_LOCK.acquire(blocking=False):
        raise RuntimeError("turn already in progress")
    try:
        lines, h = raw_history_lines(), load_history()
        _last_exchange(lines, h)
        if cfg.arc_enabled:
            arc.restore_arc_snapshot(ROOT, len(h) - 1)
            arc.truncate_records(ROOT, len(h) - 2)
            arc.truncate_arc_log(ROOT, len(h) - 2)
        analyst.truncate_briefs(ROOT, len(h) - 2)
        append_jsonl(UNDONE, lines[-2:])
        atomic_write(HISTORY, "".join(line + "\n" for line in lines[:-2]))
        text = h[-2]["content"]
        if len(h) == 2 and read(PREMISE) == text.strip():
            PREMISE.unlink()                            # undoing the opening un-pins it too
        return text
    finally:
        TURN_LOCK.release()

def _retry_brief(h, rerun_analyst):
    """Director's brief for retrying the last exchange: reuse the stored one unless asked to re-run."""
    if not cfg.analyst_enabled:
        return None
    player_idx = len(h) - 2
    if not rerun_analyst:
        brief = analyst.load_brief_for_msg(ROOT, player_idx)
        if brief:
            return brief
    allowed = get_allowed_moves(h[:-2])
    guard_items = compute_style_guard(h[:-2]) if cfg.style_guard else []
    brief, source, model_used, ms = analyst.run_analyst(
        ROOT, h[:-2], h[-2]["content"], allowed, guard_items, chat_complete
    )
    analyst.save_brief(ROOT, player_idx, source, model_used, ms, brief)
    return brief

def retry_turn(stream=False, rerun_analyst=False):
    """Regenerate the DM's last reply for the same player action."""
    result = None
    for event in run_turn(stream=stream, retry=True, rerun_analyst=rerun_analyst):
        if stream and event.get("chunk"):
            print(event["chunk"], end="", flush=True)
        if event.get("done"):
            result = event["dm"]
    return result

def game_state():
    history = load_history()
    label = ctx_label()
    if not label:
        est = est_tokens(build_context(history + [{"role": "user", "content": "x"}]))
        label = f"next prompt ≈{est / 1000:.1f}k tok"
    st = get_state()
    arc_chip = arc.get_arc_chip(arc.load_arc(ROOT)) if cfg.arc_enabled else None

    # Undo / retry status
    can_undo = True
    undo_reason = ""
    if len(history) < 2 or history[-1]["role"] != "assistant" or history[-2]["role"] != "user":
        can_undo = False
        undo_reason = "No exchange to undo"
    elif len(history) - 2 < max(folded(), ledgered()):
        can_undo = False
        undo_reason = "Exchange is folded into summary or ledger"

    auth_warning = None
    if cfg.host not in LOOPBACK_HOSTS and not cfg.web_password:
        auth_warning = f"Warning: Server is accessible on {cfg.host}:{cfg.port} without a web_password set."
        if config_locked():
            auth_warning += " Global settings and restart are locked until you set one (DM_WEB_PASSWORD or settings.json)."

    return {
        "campaign": CAMPAIGN,
        "configured": bool(cfg.api_key and cfg.model),
        "turns": sum(1 for m in history if m["role"] == "user"),
        "history": history[-60:],
        "history_start": max(0, len(history) - 60),
        "premise": read(PREMISE),
        "summary": read(SUMMARY),
        "ledger": load_ledger(),
        "audit": read(AUDIT),
        "job": dict(JOB),
        "ctx": label,
        "overlong": bool(st.get("overlong", False)),
        "last_words": _int(st.get("last_words", 0)),
        "arc_chip": arc_chip,
        "can_undo": can_undo,
        "can_retry": can_undo,
        "undo_blocked_reason": undo_reason,
        "retry_blocked_reason": undo_reason,
        "auth_warning": auth_warning,
        "folded": folded(),
        "ledgered": ledgered(),
        "ctx_budget_tok": cfg.ctx_budget_tok,
        "est_tokens": LAST["est"] or est_tokens(history),
        "prompt_tokens": LAST["prompt_tokens"],
        "cached": LAST["cached"],
        "difficulty": cfg.difficulty,
        "style_preset": cfg.style_preset,
        "style_notes": cfg.style_notes,
    }


# ---------------- web UI ----------------

app = Flask(__name__)
if os.environ.get("DM_TRUST_PROXY") == "1":                  # behind a TLS-terminating reverse proxy
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
WEB_DIR = pathlib.Path(__file__).parent / "web"

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

def config_locked():
    """Reachable beyond loopback with no password: global config writes and restart are refused."""
    return cfg.host not in LOOPBACK_HOSTS and not cfg.web_password

def _same_origin():
    origin = request.headers.get("Origin")
    if not origin:
        return request.headers.get("Host", "").casefold() == request.host.casefold()
    parsed = urlsplit(origin)
    return (
        parsed.scheme == request.scheme
        and parsed.netloc.casefold() == request.host.casefold()
        and request.headers.get("Host", "").casefold() == request.host.casefold()
    )

def validate_web_security():
    if cfg.host not in LOOPBACK_HOSTS and not cfg.web_password:
        raise RuntimeError(
            "Refusing to bind beyond loopback without web_password; set DM_WEB_PASSWORD "
            "(applies even with an existing settings.json) or bind to 127.0.0.1."
        )

@app.before_request
def check_auth():
    if request.path == "/healthz":
        return None
    if not cfg.web_password:
        return None
    if not request.headers.get("Authorization"):
        return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="lean-dm"'})

    auth = request.authorization
    valid = bool(auth and auth.password and hmac.compare_digest(
        auth.password.encode("utf-8"), cfg.web_password.encode("utf-8")))
    now = monotonic()
    address = request.remote_addr or ""
    with AUTH_FAILURE_LOCK:
        failures = AUTH_FAILURES.get(address)
        if failures:
            while failures and now - failures[0] >= 60:
                failures.popleft()
            if len(failures) >= 10:
                retry_after = str(max(1, int(60 - (now - failures[0])) + 1))
                return Response("Too many authentication attempts", 429, {"Retry-After": retry_after})
        if valid:
            AUTH_FAILURES.pop(address, None)
            return None
        if not failures:
            if address not in AUTH_FAILURES and len(AUTH_FAILURES) >= 1024:
                oldest = min(AUTH_FAILURES, key=lambda key: AUTH_FAILURES[key][-1] if AUTH_FAILURES[key] else 0)
                del AUTH_FAILURES[oldest]
            failures = AUTH_FAILURES[address] = deque()
        failures.append(now)
    return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="lean-dm"'})

@app.before_request
def guard_admin():
    if request.method != "POST":
        return None
    if not _same_origin():
        return jsonify({"ok": False, "error": "cross-origin request refused", "errors": {}}), 403
    if not config_locked():
        return None
    body = request.get_json(silent=True)
    scope = request.args.get("scope") or (body.get("scope") if isinstance(body, dict) else None) or "global"
    if request.path == "/api/restart" or (request.path == "/api/config" and scope != "campaign"):
        msg = ("Global settings and restart are disabled while the server is reachable without a "
               "web_password. Set one (DM_WEB_PASSWORD or settings.json) or bind to 127.0.0.1.")
        return jsonify({"ok": False, "error": msg, "errors": {}}), 403

@app.before_request
def throttle_mutating_posts():
    if request.method != "POST":
        return None
    now = monotonic()
    window = POST_COUNTS[request.remote_addr]
    while window and now - window[0] > 10:
        window.popleft()
    window.append(now)
    if len(window) > 20:
        return jsonify({"ok": False, "error": "too many requests"}), 429

@app.route("/")
def index():
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        return Response("Web assets missing", 404)
    page = index_file.read_text(encoding="utf-8").replace("__CAMP__", html.escape(CAMPAIGN))
    return Response(page, mimetype="text/html")

@app.route("/config")
def config_page():
    cfg_file = WEB_DIR / "config.html"
    if not cfg_file.exists():
        return Response("Web assets missing", 404)
    return Response(cfg_file.read_text(encoding="utf-8"), mimetype="text/html")

@app.route("/web/<path:filename>")
def serve_web(filename):
    return send_from_directory(WEB_DIR, filename)

@app.route("/api/state")
def api_state():
    return jsonify(game_state())

@app.route("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")

@app.route("/api/turn", methods=["POST"])
def api_turn():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    try:
        dm = None
        for event in run_turn(text, stream=False):
            if event.get("done"):
                dm = event["dm"]
        return jsonify({"dm": dm})
    except Exception as e:
        if str(e) == "turn already in progress":
            return jsonify({"error": str(e)}), 409
        return jsonify({"error": str(e)}), 502

class ThinkFilter:
    """Hide <think>…</think> text from a live stream, tolerating tags split across chunks."""
    def __init__(self):
        self.buf, self.hidden = "", False

    def feed(self, piece):
        self.buf += piece
        out = ""
        while self.buf:
            tag = "</think>" if self.hidden else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if not self.hidden:
                    out += self.buf[:i]
                self.buf, self.hidden = self.buf[i + len(tag):], not self.hidden
                continue
            keep = next((n for n in range(min(len(tag) - 1, len(self.buf)), 0, -1)
                         if tag.startswith(self.buf[-n:])), 0)
            if not self.hidden:
                out += self.buf[:len(self.buf) - keep]
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return out

    def flush(self):
        out = "" if self.hidden else self.buf
        self.buf = ""
        return out

@app.route("/api/stream", methods=["POST"])
def api_stream():
    try:
        ensure_configured()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    if not TURN_LOCK.acquire(blocking=False):
        return jsonify({"error": "turn already in progress"}), 409

    def event_stream():
        think = ThinkFilter()
        try:
            for event in run_turn(text, stream=True, lock_acquired=True):
                if event.get("stage"):
                    yield f"data: {json.dumps({'stage': event['stage']})}\n\n"
                elif event.get("chunk"):
                    if event.get("ooc"):
                        yield f"data: {json.dumps({'chunk': event['chunk'], 'ooc': True})}\n\n"
                        continue
                    piece = think.feed(event["chunk"])
                    if piece:
                        yield f"data: {json.dumps({'chunk': piece})}\n\n"
            tail = think.flush()
            if tail:
                yield f"data: {json.dumps({'chunk': tail})}\n\n"
        except Exception as e:
            logger.warning("Streaming turn failed: %s", e)
            yield f"data: {json.dumps({'error': str(e)[:300]})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/api/undo", methods=["POST"])
def api_undo():
    try:
        return jsonify({"text": undo_turn()})
    except Exception as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/retry", methods=["POST"])
def api_retry():
    try:
        return jsonify({"dm": retry_turn()})
    except Exception as e:
        if str(e) == "turn already in progress":
            return jsonify({"error": str(e)}), 409
        return jsonify({"error": str(e)}), 502

@app.route("/api/rebuild", methods=["POST"])
def api_rebuild():
    try:
        ensure_configured()
        start_job("rebuild ledger", rebuild_ledger)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/audit", methods=["POST"])
def api_audit():
    try:
        ensure_configured()
        start_job("audit", run_audit)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/premise", methods=["POST"])
def api_premise():
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    if PREMISE.exists():
        shutil.copy(PREMISE, ROOT / "premise.md.bak")
    atomic_write(PREMISE, text)
    return jsonify({"ok": True})

@app.route("/api/ledger", methods=["POST"])
def api_ledger_action():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "unknown action"}), 400
    action = body.get("action")
    if action not in ("pin", "delete", "upsert"):
        return jsonify({"ok": False, "error": "unknown action"}), 400
    with LEDGER_LOCK:
        return _ledger_action(action, body)

def _ledger_action(action, body):
    if LEDGER.exists():
        shutil.copy(LEDGER, ROOT / f"ledger.{int(time.time())}.bak.json")
        backups = sorted(ROOT.glob("ledger.*.bak.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[10:]:
            old.unlink(missing_ok=True)
    ledger = load_ledger(strict=True)
    if action == "pin":
        entry_id = body.get("id")
        pinned = bool(body.get("pinned", True))
        for e in ledger:
            if e.get("id") == entry_id:
                e["pinned"] = pinned
                break
    elif action == "delete":
        entry_id = body.get("id")
        ledger = [e for e in ledger if e.get("id") != entry_id]
    elif action == "upsert":
        entry = body.get("entry", {})
        ledger = apply_ledger_ops(ledger, {"upsert": [entry]})
    atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
    return jsonify({"ok": True, "ledger": ledger})

@app.route("/api/export/transcript", methods=["GET"])
def api_export_transcript():
    history = load_history()
    lines = [f"# Campaign Transcript — {CAMPAIGN}\n"]
    if p := read(PREMISE):
        lines.append(f"## Premise\n{p}\n")
    for i, m in enumerate(history):
        role = "Player" if m["role"] == "user" else "DM"
        lines.append(f"### [{i}] {role}\n{m['content']}\n")
    return Response("\n".join(lines), mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{CAMPAIGN}-transcript.md"'})

@app.route("/api/export/campaign.zip", methods=["GET"])
def api_export_zip():
    buf = io.BytesIO()
    include_arc = request.args.get("include_arc") == "1"
    arc_names = {"arc.json", "arc_log.jsonl", "records.jsonl", "briefs.jsonl", "arc_snap"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in ROOT.rglob("*"):
            if f.is_file():
                if f.name in ("settings.json", "campaign.json"):
                    continue
                rel = f.relative_to(ROOT)
                if not include_arc and (rel.name in arc_names or "arc_snap" in rel.parts):
                    continue
                zf.write(f, arcname=str(rel))
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{CAMPAIGN}.zip"'})

@app.route("/api/campaigns", methods=["GET"])
def api_campaigns_list():
    camps = []
    if CAMPAIGNS_DIR.exists():
        for d in CAMPAIGNS_DIR.iterdir():
            if d.is_dir() and (d / "history.jsonl").exists():
                camps.append(d.name)
    return jsonify({"campaigns": camps, "current": CAMPAIGN})

@app.route("/api/campaigns", methods=["POST"])
def api_campaigns_manage():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    name = re.sub(r"[^\w.\-]", "_", body.get("name", "")).strip()
    if not name or name in (".", ".."):
        return jsonify({"error": "invalid name"}), 400
    target = CAMPAIGNS_DIR / name
    if action == "create":
        target.mkdir(parents=True, exist_ok=True)
        try:
            (target / "history.jsonl").open("x", encoding="utf-8").close()
        except FileExistsError:
            return jsonify({"error": "campaign exists"}), 409
        return jsonify({"ok": True, "name": name})
    elif action == "duplicate":
        if target.exists():
            return jsonify({"error": "campaign exists"}), 409
        shutil.copytree(ROOT, target)
        return jsonify({"ok": True, "name": name})
    return jsonify({"error": "unknown action"}), 400

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(cfg.public())

@app.route("/api/config", methods=["POST"])
def api_config_set():
    body = request.get_json(silent=True) or {}
    scope = request.args.get("scope") or body.get("scope") or "global"
    if body.get("reset"):
        cfg.reset(scope=scope)
        return jsonify({"ok": True, "errors": {}, **cfg.public()})
    errors = cfg.update(body.get("changes") or {}, scope=scope)
    return jsonify({"ok": not errors, "errors": errors, **cfg.public()}), (400 if errors else 200)

@app.route("/api/restart", methods=["POST"])
def api_restart():
    def restart():
        time.sleep(0.5)
        # Werkzeug keeps its listening socket inheritable for its reloader.
        try:
            os.set_inheritable(int(os.environ["WERKZEUG_SERVER_FD"]), False)
        except (KeyError, ValueError, OSError):
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=restart, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/notes", methods=["GET"])
def api_notes_get():
    return jsonify({"notes": analyst.load_notes(ROOT)})

@app.route("/api/notes/<note_id>/canon", methods=["POST"])
def api_notes_canon(note_id):
    notes = analyst.load_notes(ROOT)
    note = next((n for n in notes if n.get("id") == note_id), None)
    if not note:
        return jsonify({"error": "note not found"}), 404
    if note.get("canon"):
        return jsonify({"ok": True})
    if note.get("proposal") and isinstance(note["proposal"], dict):
        with LEDGER_LOCK:
            ledger = apply_ledger_ops(load_ledger(strict=True), {"upsert": [note["proposal"]]})
            atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
    else:
        prem = read(PREMISE)
        clean_q = analyst.strip_ooc_prefix(note.get("q", ""))
        line = f"- {clean_q}: {note.get('a', '')}"
        if "## Canon notes" in prem:
            addition = "\n" + line
        else:
            addition = f"\n\n## Canon notes\n{line}"
        atomic_write(PREMISE, (prem + addition).strip())
    analyst.update_note_canon(ROOT, note_id, True)
    return jsonify({"ok": True})

@app.route("/api/config/test", methods=["POST"])
def api_config_test():
    results = []
    models = [("narrator", cfg.model)]
    if cfg.scribe_model and cfg.scribe_model != cfg.model:
        models.append(("scribe", cfg.scribe_model))
    for role, model in models:
        t = time.time()
        try:
            if not (cfg.api_key and model):
                raise RuntimeError("model or API key not set")
            r = chat_complete([{"role": "user", "content": "Reply with the single word: ok"}],
                              model=model, track=False, retries=0, fallback=False)
            results.append({"role": role, "model": model, "ok": True, "sec": round(time.time() - t, 1), "reply": r[:80]})
        except Exception as e:
            results.append({"role": role, "model": model or "(none)", "ok": False, "error": str(e)[:300]})
    return jsonify({"results": results})


# ---------------- terminal mode ----------------

def cli():
    history = load_history()
    print(f"=== Campaign '{CAMPAIGN}' — {sum(m['role'] == 'user' for m in history)} turns — "
          f"/undo /retry /context /ledger /rebuild /audit /quit ===\n")
    if not history:
        print("(new campaign — open with your character concept and the opening scene; "
              "this first message is pinned as the premise)\n")
    else:
        last = next((m for m in reversed(history) if m["role"] == "assistant"), None)
        if last:
            print(f"{last['content']}\n")               # where you left off
    while True:
        try:
            player = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not player:
            continue
        if player == "/quit":
            break
        try:
            if player == "/context":
                show_context()
            elif player == "/ledger":
                print(render_ledger(load_ledger()) or "(ledger is empty)", "\n")
            elif player == "/rebuild":
                rebuild_ledger()
            elif player == "/audit":
                run_audit()
                print(read(AUDIT), "\n")
            elif player == "/undo":
                print(f"[removed; your move was: {undo_turn()}]\n")
            elif player == "/notes":
                notes = analyst.load_notes(ROOT)
                if not notes:
                    print("(no notes recorded yet)\n")
                else:
                    for n in notes:
                        c = " [canon]" if n.get("canon") else ""
                        print(f"[{n.get('id')}{c}] Q: {n.get('q')}\nA: {n.get('a')}\n")
            elif player == "/brief":
                print("[SPOILER WARNING: The analyst brief contains director notes for the DM]")
                confirm = input("Show brief? [y/N]: ").strip().lower()
                if confirm == "y":
                    h = load_history()
                    last_user_idx = max((i for i, m in enumerate(h) if m["role"] == "user"), default=None)
                    if last_user_idx is not None:
                        b = analyst.load_brief_for_msg(ROOT, last_user_idx)
                        print(json.dumps(b, indent=2, ensure_ascii=False) if b else "(no brief for last turn)\n")
                    else:
                        print("(no player turns yet)\n")
            elif player in ("/retry", "retry!"):
                print()
                retry_turn(stream=True, rerun_analyst=(player == "retry!"))
                print(f"\n\n[{ctx_label()}]\n")
            elif player.startswith("/"):
                print("[commands: /undo /retry retry! /brief /notes /context /ledger /rebuild /audit /quit]\n")
            else:
                print()
                player_turn(player, stream=True)
                print(f"\n\n[{ctx_label()}]\n")
        except KeyboardInterrupt:
            print("\n[interrupted — nothing was saved]\n")
        except Exception as e:
            hint = "" if player.startswith("/") else " — nothing was saved; retype your move"
            print(f"\n[error{hint}: {e}]\n")
    if MAINT_THREAD and MAINT_THREAD.is_alive():
        print("[finishing background maintenance…]")
        MAINT_THREAD.join(timeout=120)
    print("[campaign saved]")

if __name__ == "__main__":
    argv = sys.argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    maint_cmd = any(a in argv for a in ("--reset-fold", "--rebuild-ledger", "--audit"))
    init_files()
    if "--show-context" in argv:
        show_context()
        sys.exit()
    if "--web" not in argv or maint_cmd:
        validate_config()                               # the web UI may start unconfigured; fix it on /config
    print_config()
    if "--reset-fold" in argv:
        reset_fold()
        rebuild_ledger()
        sys.exit()
    if "--rebuild-ledger" in argv:
        rebuild_ledger()
        sys.exit()
    if "--audit" in argv:
        run_audit()
        print(read(AUDIT))
        sys.exit()
    if "--plan-arc" in argv:
        ensure_configured()
        print(f"[planning arc for campaign '{CAMPAIGN}'...]")
        if arc.arc_path(ROOT).exists():         # back up even an unreadable arc.json
            bak = ROOT / "arc.json.bak"
            shutil.copy(arc.arc_path(ROOT), bak)
            print(f"[backed up existing arc to {bak}]")
        h = load_history()
        new_arc = arc.plan_initial_arc(read(PREMISE), read(SUMMARY), render_ledger(load_ledger()), h, chat_complete)
        arc.save_arc(ROOT, new_arc)
        print(f"[arc planned successfully: central question='{new_arc.get('central_question')}']")
        sys.exit()
    if "--web" in argv:
        validate_web_security()
        init_files()
        if not (cfg.api_key and cfg.model):
            print("[not configured yet — open /config to set the model and API key]")
        print(f"web UI → http://{'localhost' if cfg.host == '127.0.0.1' else cfg.host}:{cfg.port}\n")
        if os.environ.get("DM_USE_WAITRESS", "").strip() == "1":
            from waitress import serve
            serve(app, host=cfg.host, port=cfg.port, threads=4)
        else:
            app.run(host=cfg.host, port=cfg.port, debug=False)
    else:
        cli()
