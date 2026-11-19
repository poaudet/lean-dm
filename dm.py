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

import json, os, sys, pathlib, threading, time, html, re, shutil
import requests
from flask import Flask, request, jsonify, Response
from config import cfg, SETTINGS_PATH
import analyst

# ---------------- paths ----------------

CAMPAIGN = cfg.campaign
if "--campaign" in sys.argv:
    CAMPAIGN = re.sub(r"[^\w.\-]", "_", sys.argv[sys.argv.index("--campaign") + 1]) or "default"

CAMPAIGNS_DIR = pathlib.Path(os.environ.get("DM_CAMPAIGNS_DIR") or pathlib.Path(__file__).parent / "campaigns")
ROOT = CAMPAIGNS_DIR / CAMPAIGN
HISTORY, UNDONE = ROOT / "history.jsonl", ROOT / "undone.jsonl"
PREMISE, SUMMARY = ROOT / "premise.md", ROOT / "summary.md"
LEDGER, AUDIT, STATE = ROOT / "ledger.json", ROOT / "audit.md", ROOT / "state.json"

TURN_LOCK = threading.Lock()    # one player action (turn / undo / retry) at a time
MAINT_LOCK = threading.Lock()   # one maintenance job (ledger / fold / audit) at a time
STATE_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
MAINT_THREAD = None
JOB = {"name": "", "running": False, "msg": ""}
LAST = {"est": 0, "prompt_tokens": None, "cached": None}

KINDS = ("player", "person", "place", "item", "promise", "clock", "rule", "thread")
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
        print(f"[warn: {msg}]")

def atomic_write(path, text):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

def append_jsonl(path, items):
    data = "".join((x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) + "\n" for x in items)
    if path.exists() and path.stat().st_size:
        with path.open("rb") as f:                      # never glue onto an unterminated last line
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                data = "\n" + data
    with path.open("a", encoding="utf-8") as f:
        f.write(data)

def read(path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""

def raw_history_lines():
    return [l for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]

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

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48]

def _lst(x):
    return x if isinstance(x, list) else []

def apply_ledger_ops(ledger, ops):
    by = {e["id"]: e for e in ledger}
    for u in _lst(ops.get("upsert")):
        if not isinstance(u, dict):
            continue
        eid = slug(u.get("id") or u.get("name") or "")
        if not eid or not str(u.get("state", "")).strip():
            continue
        e = by.get(eid) or {"id": eid}
        kind = str(u.get("kind", "")).strip().lower()
        e["kind"] = kind if kind in KINDS else e.get("kind", "thread")
        e["name"] = str(u.get("name") or e.get("name") or eid).strip()
        e["state"] = str(u["state"]).strip()
        src = str(u.get("src", "")).strip().lower()
        e["src"] = src if src in ("player", "dm", "note") else e.get("src", "dm")
        try:
            e["msg"] = int(u.get("msg"))
        except (TypeError, ValueError):
            e.setdefault("msg", None)
        e["status"] = "active"                          # an upsert reopens a closed entry
        by[eid] = e
    for i in _lst(ops.get("close")):
        if slug(i) in by:
            by[slug(i)]["status"] = "closed"
    for i in _lst(ops.get("delete")):
        by.pop(slug(i), None)
    out = list(by.values())
    if len(out) > cfg.ledger_max:                       # over the cap: oldest CLOSED entries go first
        closed = sorted((e for e in out if e.get("status") == "closed"), key=lambda e: e.get("msg") or 0)
        drop = {e["id"] for e in closed[: len(out) - cfg.ledger_max]}
        out = [e for e in out if e["id"] not in drop]
    return out

def render_ledger(ledger):
    if not ledger:
        return ""
    order = {k: i for i, k in enumerate(KINDS)}
    act = sorted((e for e in ledger if e.get("status") != "closed"),
                 key=lambda e: (order.get(e.get("kind"), 99), str(e.get("name", ""))))
    closed = sorted((e for e in ledger if e.get("status") == "closed"),
                    key=lambda e: e.get("msg") or 0)[-cfg.ledger_closed_max:] if cfg.ledger_closed_max else []
    lines, cur = [], None
    for e in act:
        if e.get("kind") != cur:
            cur = e.get("kind")
            lines.append(f"[{str(cur).upper()}]")
        lines.append(f"- {e.get('name')}: {e.get('state')}")
    if closed:
        lines.append("[PAST / RESOLVED — do not revive]")
        lines += [f"- {e.get('name')}: {e.get('state')}" for e in closed]
    return "\n".join(lines)

def merge_same_role(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    return out

def compute_style_guard(history):
    dm_msgs = [m["content"] for m in history if m.get("role") == "assistant"]
    if not dm_msgs:
        return []
    openers = []
    for msg in dm_msgs[-8:]:
        words = msg.strip().split()
        if words:
            opener = " ".join(words[:5])
            if opener and opener not in openers:
                openers.append(opener)
    from collections import Counter
    counts = Counter()
    for msg in dm_msgs[-12:]:
        words = re.findall(r"\b[A-Za-z0-9'-]+\b", msg.lower())
        for i in range(len(words) - 2):
            counts[f"{words[i]} {words[i+1]} {words[i+2]}"] += 1
    trigrams = [tri for tri, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if cnt >= 3]
    combined = []
    for op in openers:
        if op not in combined:
            combined.append(op)
    for tri in trigrams:
        if tri not in combined:
            combined.append(tri)
    return combined[:8]

def build_context(history, brief=None):
    """history must end with the pending player action."""
    k = folded()
    parts = [cfg.system_prompt]
    if p := read(PREMISE):
        parts.append("PREMISE (fixed canon):\n" + p)
    if cfg.style_enabled:
        style_parts = []
        preset = cfg.style_preset
        if preset and preset != "none":
            preset_file = pathlib.Path(__file__).parent / "styles" / f"{preset}.md"
            if preset_file.exists():
                style_parts.append(preset_file.read_text(encoding="utf-8").strip())
        if cfg.style_words_min > 0 or cfg.style_words_max > 0:
            if cfg.style_words_min > 0 and cfg.style_words_max > 0:
                style_parts.append(f"Target length: {cfg.style_words_min}–{cfg.style_words_max} words per reply.")
            elif cfg.style_words_max > 0:
                style_parts.append(f"Target length: at most {cfg.style_words_max} words per reply.")
            else:
                style_parts.append(f"Target length: at least {cfg.style_words_min} words per reply.")
        if cfg.style_push:
            style_parts.append(f"Style push: {cfg.style_push}/100.")
        if cfg.style_notes and cfg.style_notes.strip():
            style_parts.append("Style notes:\n" + cfg.style_notes.strip())
        style_parts.append(
            "Rules: Every reply must end on a concrete new development or decision point, "
            "avoid recapping the player's action, and keep atmosphere to the minority of sentences."
        )
        parts.append("STYLE:\n" + "\n\n".join(style_parts))
    if k and (s := read(SUMMARY)):
        parts.append("STORY SO FAR (older play, already summarized; everything after it is verbatim below):\n" + s)
    tail = history[min(k, len(history) - 1):]
    if tail and tail[0]["role"] == "assistant":         # only if history parity was ever disturbed
        tail = tail[1:]
    msgs = merge_same_role(tail)
    canon = render_ledger(load_ledger()) if cfg.ledger_enabled else ""
    if canon and msgs and msgs[-1]["role"] == "user":   # volatile part goes LAST to keep the prefix cacheable
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nCANON LEDGER — authoritative current state. If it conflicts with your recollection "
                    "of earlier turns, the ledger wins. Never mention it to the player.\n" + canon +
                    "\n--- end of ledger: now narrate the outcome of the player's action above."}
    if cfg.style_guard and msgs and msgs[-1]["role"] == "user":
        guard = compute_style_guard(history)
        if guard:
            msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                        "\n\n---\nAVOID REPEATING:\n" + "\n".join(f"- {g}" for g in guard)}
    if brief and msgs and msgs[-1]["role"] == "user":
        brief_text = json.dumps(brief, ensure_ascii=False, indent=1)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nDIRECTOR'S BRIEF (never mention it to the player):\n" + brief_text}
    return [{"role": "system", "content": "\n\n".join(parts)}] + msgs

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
            print(f"[api error ({str(e)[:120]}); retry {attempt + 1}/{n - 1} in {wait:.0f}s]")
            time.sleep(wait)

def _with_fallback(call, primary, fallback=True):
    try:
        return call(primary)
    except TRANSIENT as e:
        fb = cfg.fallback_model
        if fallback and fb and fb != primary:
            print(f"[{primary} failed ({str(e)[:100]}); trying fallback {fb}]")
            return call(fb)
        raise

def note_usage(u):
    if isinstance(u, dict) and u.get("prompt_tokens"):
        LAST["prompt_tokens"] = u["prompt_tokens"]
        d = u.get("prompt_tokens_details")
        LAST["cached"] = d.get("cached_tokens") if isinstance(d, dict) else None

def clean_reply(text):
    """Strip reasoning blocks that some models emit inline."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.split("<think>", 1)[0].strip()

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
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])

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

def record_pass(upto=None):
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
        ledger = apply_ledger_ops(ledger, parse_json_obj(raw))
        atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
        set_state(ledgered=b)                           # pointer LAST, per range
        print(f"[ledger: messages {a}–{b - 1} recorded; {len(ledger)} entries]")

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
        record_pass(upto=end)                           # never fold what the ledger has not seen
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
    print(f"[fold: messages {k}–{end - 1} folded into chapters]")

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
    print(f"[audit written to {AUDIT}]")

def run_audit():
    with MAINT_LOCK:
        _audit()

def rebuild_ledger():
    with MAINT_LOCK:
        h = load_history()
        if LEDGER.exists() and read(LEDGER) not in ("", "[]"):
            shutil.copy(LEDGER, ROOT / f"ledger.{int(time.time())}.bak.json")
        atomic_write(LEDGER, "[]")
        set_state(ledgered=0)
        upto = max(0, len(h) - 2 * cfg.ledger_lag_turns)
        record_pass(upto=upto - upto % 2)

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
    print(f"[v2 summary/facts archived in {legacy}; folded=0]")

def maintain():
    if not MAINT_LOCK.acquire(blocking=False):          # single-flight
        return
    try:
        if cfg.ledger_enabled:
            record_pass()
        fold_if_needed()
        if cfg.audit_every_turns:
            n = len(load_history())
            if n - _int(get_state().get("audited")) >= 2 * cfg.audit_every_turns:
                _audit()
    except Exception as e:
        print(f"[maintenance failed, will retry next turn: {e}]")
    finally:
        MAINT_LOCK.release()

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

def player_turn(text, stream=False):
    with TURN_LOCK:
        history = load_history()
        if cfg.notes_routing and analyst.is_ooc_note(text):
            entry = analyst.handle_ooc_note(
                ROOT, text, history, read(PREMISE), read(SUMMARY),
                render_ledger(load_ledger()), chat_complete
            )
            return entry["a"]

        brief = None
        if cfg.analyst_enabled:
            allowed = get_allowed_moves() if 'get_allowed_moves' in globals() else []
            guard_items = compute_style_guard(history) if cfg.style_guard else []
            brief, source, model_used, ms = analyst.run_analyst(
                ROOT, history, text, allowed, guard_items, chat_complete
            )
            analyst.save_brief(ROOT, len(history), source, model_used, ms, brief)

        user = {"role": "user", "content": text}
        dm = _generate(build_context(history + [user], brief=brief), stream)
        append_jsonl(HISTORY, [user, {"role": "assistant", "content": dm}])   # commit the pair together
        words = len(dm.split())
        overlong = bool(cfg.style_words_max > 0 and words > int(1.5 * cfg.style_words_max))
        set_state(last_words=words, overlong=overlong)
        if not history and not read(PREMISE):
            atomic_write(PREMISE, text)                                       # first message = pinned premise
    start_maintain()
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
    with TURN_LOCK:
        lines, h = raw_history_lines(), load_history()
        _last_exchange(lines, h)
        analyst.truncate_briefs(ROOT, len(h) - 2)
        append_jsonl(UNDONE, lines[-2:])
        atomic_write(HISTORY, "".join(l + "\n" for l in lines[:-2]))
        text = h[-2]["content"]
        if len(h) == 2 and read(PREMISE) == text.strip():
            PREMISE.unlink()                            # undoing the opening un-pins it too
        return text

def retry_turn(stream=False, rerun_analyst=False):
    """Regenerate the DM's last reply for the same player action."""
    with TURN_LOCK:
        lines, h = raw_history_lines(), load_history()
        _last_exchange(lines, h)
        brief = None
        if cfg.analyst_enabled:
            player_idx = len(h) - 2
            if rerun_analyst:
                allowed = get_allowed_moves() if 'get_allowed_moves' in globals() else []
                guard_items = compute_style_guard(h[:-2]) if cfg.style_guard else []
                brief, source, model_used, ms = analyst.run_analyst(
                    ROOT, h[:-2], h[-2]["content"], allowed, guard_items, chat_complete
                )
                analyst.save_brief(ROOT, player_idx, source, model_used, ms, brief)
            else:
                brief = analyst.load_brief_for_msg(ROOT, player_idx)
                if not brief:
                    allowed = get_allowed_moves() if 'get_allowed_moves' in globals() else []
                    guard_items = compute_style_guard(h[:-2]) if cfg.style_guard else []
                    brief, source, model_used, ms = analyst.run_analyst(
                        ROOT, h[:-2], h[-2]["content"], allowed, guard_items, chat_complete
                    )
                    analyst.save_brief(ROOT, player_idx, source, model_used, ms, brief)

        dm = _generate(build_context(h[:-1], brief=brief), stream)
        new = json.dumps({"role": "assistant", "content": dm}, ensure_ascii=False)
        append_jsonl(UNDONE, lines[-1:])
        atomic_write(HISTORY, "".join(l + "\n" for l in lines[:-1]) + new + "\n")
        words = len(dm.split())
        overlong = bool(cfg.style_words_max > 0 and words > int(1.5 * cfg.style_words_max))
        set_state(last_words=words, overlong=overlong)
    return dm

def game_state():
    history = load_history()
    label = ctx_label()
    if not label:
        est = est_tokens(build_context(history + [{"role": "user", "content": "x"}]))
        label = f"next prompt ≈{est / 1000:.1f}k tok"
    st = get_state()
    return {
        "campaign": CAMPAIGN,
        "configured": bool(cfg.api_key and cfg.model),
        "turns": sum(1 for m in history if m["role"] == "user"),
        "history": history[-60:],
        "premise": read(PREMISE),
        "summary": read(SUMMARY),
        "ledger": load_ledger(),
        "audit": read(AUDIT),
        "job": dict(JOB),
        "ctx": label,
        "overlong": bool(st.get("overlong", False)),
        "last_words": _int(st.get("last_words", 0)),
    }


# ---------------- web UI ----------------

app = Flask(__name__)

CSS = r"""
body{font-family:system-ui,sans-serif;margin:0;background:#141220;color:#e8e5f5}
a{color:#b3a6ff}
button{padding:10px 16px;border-radius:8px;border:none;background:#6d4aff;color:#fff;cursor:pointer;font-size:.95em}
button.ghost{background:transparent;border:1px solid #3d3670;color:#c9c3ee}
button:disabled{opacity:.5;cursor:default}
input,textarea{padding:10px;border-radius:8px;border:1px solid #3d3670;background:#1c1930;color:#e8e5f5;font-size:1em;font-family:inherit}
"""

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DM — __CAMP__</title><style>__CSS__
body{display:flex;height:100vh}
#chat{flex:1;display:flex;flex-direction:column;max-width:820px;margin:0 auto;width:100%}
#log{flex:1;overflow-y:auto;padding:16px}
.msg{margin:10px 0;padding:10px 14px;border-radius:10px;white-space:pre-wrap;line-height:1.5}
.user{background:#2b2450;border:1px solid #3d3670;max-width:85%;margin-left:auto}
.dm{background:#1c1930;border:1px solid #2e2950;max-width:95%}
.meta{color:#8b84b8;font-size:.8em;padding:10px 16px 0}
#bar{display:flex;gap:8px;padding:12px;border-top:1px solid #2e2950}
input#in{flex:1}
#side{width:360px;border-left:1px solid #2e2950;display:flex;flex-direction:column}
#tabs{display:flex}#tabs button{flex:1;border-radius:0;background:#1c1930;color:#c9c3ee;padding:10px}
#tabs button.on{background:#2b2450;color:#fff}
#acts{display:flex;gap:6px;padding:8px}#acts button{flex:1;padding:6px;font-size:.8em}
#file{flex:1;overflow-y:auto;padding:14px;font-size:.9em;line-height:1.5}
#file h1,#file h2,#file h3{color:#b3a6ff}#file ul{padding-left:18px}
@media(max-width:900px){#side{display:none}}
</style></head><body>
<div id="chat">
  <div class="meta">__CAMP__ — <span id="tn"></span> turns — <span id="ctx"></span> — <span id="job"></span>
    <span id="warn" style="color:#ffb86b"></span> — <a href="/config">⚙ config</a></div>
  <div id="log"></div>
  <div id="bar">
    <button class="ghost" onclick="undo()">↶ undo</button>
    <button class="ghost" onclick="retry()">↻ retry</button>
    <input id="in" placeholder="Your action… (Enter to send)" autofocus>
    <button onclick="send()">Send</button>
  </div>
</div>
<div id="side">
  <div id="tabs">
    <button id="t-prem" onclick="setTab('prem')">Premise</button>
    <button id="t-sum" onclick="setTab('sum')">Summary</button>
    <button id="t-led" class="on" onclick="setTab('led')">Ledger</button>
    <button id="t-aud" onclick="setTab('aud')">Audit</button>
  </div>
  <div id="acts">
    <button class="ghost" onclick="act('/api/rebuild')">rebuild ledger</button>
    <button class="ghost" onclick="act('/api/audit')">run audit</button>
  </div>
  <div id="file"></div>
</div>
<script>
const $=id=>document.getElementById(id);
let busy=false,current='led',state=null;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(t){
  t=esc(t);
  const lines=t.split('\n');let out='',inList=false;
  for(let l of lines){
    if(/^#{1,3} /.test(l)){if(inList){out+='</ul>';inList=false}
      const n=l.match(/^#+/)[0].length;out+=`<h${n}>${l.replace(/^#+ /,'')}</h${n}>`}
    else if(/^[-*] /.test(l)){if(!inList){out+='<ul>';inList=true}out+=`<li>${l.slice(2)}</li>`}
    else if(/^---+$/.test(l)){if(inList){out+='</ul>';inList=false}out+='<hr>'}
    else if(l.trim()===''){if(inList){out+='</ul>';inList=false}}
    else{if(inList){out+='</ul>';inList=false}out+=`<p>${l}</p>`}
  }
  if(inList)out+='</ul>';
  return out.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>');
}
function ledgerHtml(){
  const L=state.ledger||[];
  if(!L.length)return '<p>(empty — it builds automatically as the story grows, or press “rebuild ledger”)</p>';
  const by={};
  for(const e of L)(by[e.kind||'other']=by[e.kind||'other']||[]).push(e);
  let out='';
  for(const k of Object.keys(by).sort()){
    out+='<h3>'+esc(k)+'</h3><ul>';
    for(const e of by[k])out+='<li'+(e.status==='closed'?' style="opacity:.55"':'')+'><b>'+esc(e.name||e.id)+'</b>: '+esc(e.state||'')+
      ' <small>['+esc(e.src||'?')+(e.msg!=null?' #'+e.msg:'')+(e.status==='closed'?' · closed':'')+']</small></li>';
    out+='</ul>';
  }
  return out;
}
function addMsg(cls,text){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;$('log').appendChild(d);return d}
function toBottom(){const l=$('log');l.scrollTop=l.scrollHeight}
function renderLog(s){
  $('log').innerHTML='';
  if(!s.history.length)addMsg('dm','New campaign — open with your character concept and the scene you want. This first message is pinned as the premise.');
  for(const m of s.history)addMsg(m.role==='user'?'user':'dm',m.content);
  toBottom();
}
function renderSide(){
  if(!state)return;
  const f=$('file');
  if(current==='prem')f.innerHTML=md(state.premise||'(empty — your first message is pinned here; you can also edit premise.md by hand)');
  else if(current==='sum')f.innerHTML=md(state.summary||'(empty — the narrator sees the full history; chapters start when it passes the fold budget)');
  else if(current==='led')f.innerHTML=ledgerHtml();
  else f.innerHTML=md(state.audit||'(none yet — press “run audit”)');
}
function setTab(t){current=t;['prem','sum','led','aud'].forEach(x=>$('t-'+x).classList.toggle('on',x===t));renderSide()}
function refresh(full=true){
  return fetch('/api/state').then(r=>r.json()).then(s=>{
    state=s;$('tn').textContent=s.turns;$('ctx').textContent=s.ctx||'';
    $('warn').textContent=s.configured?'':'⚠ not configured — open ⚙ config';
    const j=s.job;$('job').textContent=j.running?('⏳ '+j.name+'…'):(j.name?(j.name+': '+j.msg):'');
    if(full)renderLog(s);
    renderSide();
  });
}
function send(){
  const i=$('in'),t=i.value.trim();if(!t||busy)return;
  busy=true;i.value='';
  const u=addMsg('user',t),w=addMsg('dm','…');toBottom();
  fetch('/api/turn',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})
    .then(r=>r.json())
    .then(d=>{
      if(d.error){u.remove();w.textContent='[error: '+d.error+' — nothing was saved; your move is back in the input box]';i.value=t}
      else return refresh();
    })
    .catch(()=>{u.remove();w.textContent='[network error — your move is back in the input box]';i.value=t})
    .finally(()=>{busy=false});
}
function undo(){
  if(busy)return;busy=true;
  fetch('/api/undo',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.error){alert(d.error);return}
    $('in').value=d.text||'';return refresh();
  }).finally(()=>{busy=false});
}
function retry(){
  if(busy)return;busy=true;
  const w=addMsg('dm','…');toBottom();
  fetch('/api/retry',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.error){w.textContent='[error: '+d.error+']'}else return refresh();
  }).catch(()=>{w.textContent='[network error]'}).finally(()=>{busy=false});
}
function act(path){
  fetch(path,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.error)alert(d.error);return refresh(false)});
}
$('in').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
setTab('led');refresh();setInterval(()=>{if(!busy)refresh(false)},5000);
</script></body></html>"""

CONFIG_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DM — settings</title><style>__CSS__
body{padding:20px}
#wrap{max-width:820px;margin:0 auto}
fieldset{border:1px solid #2e2950;border-radius:10px;margin:18px 0;padding:8px 16px 16px}
legend{color:#b3a6ff;padding:0 8px}
.row{margin:14px 0;display:flex;flex-direction:column;gap:4px}
.row label{font-weight:600}
.row input[type=checkbox]{width:20px;height:20px}
.help{color:#8b84b8;font-size:.85em}
.err{color:#ff7b7b;font-size:.85em}
.badge{background:#3d3670;border-radius:6px;padding:1px 6px;font-size:.7em;font-weight:400}
#bar{position:sticky;bottom:0;background:#141220;padding:12px 0;display:flex;gap:8px;align-items:center;border-top:1px solid #2e2950}
#status{color:#8b84b8;font-size:.9em;white-space:pre-wrap}
</style></head><body><div id="wrap">
<h2>⚙ Settings <small style="font-weight:400"><a href="/">← back to the game</a></small></h2>
<div class="help" id="path"></div>
<div id="form"></div>
<div id="bar">
  <button onclick="save()">Save</button>
  <button class="ghost" onclick="saveTest()">Save &amp; test connection</button>
  <button class="ghost" onclick="resetAll()">Reset to defaults</button>
  <span id="status"></span>
</div></div>
<script>
const $=id=>document.getElementById(id);
let meta=null;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
async function load(){meta=await (await fetch('/api/config')).json();$('path').textContent='Stored in '+meta.path+' — values apply live except those marked “restart”.';render()}
function render(){
  const groups={};
  for(const f of meta.fields)(groups[f.group]=groups[f.group]||[]).push(f);
  let h='';
  for(const g in groups){
    h+=`<fieldset><legend>${esc(g)}</legend>`;
    for(const f of groups[g]){
      const v=meta.values[f.key],id='f_'+f.key;
      let input;
      if(f.type==='bool')input=`<input type="checkbox" id="${id}" ${v?'checked':''}>`;
      else if(f.type==='text')input=`<textarea id="${id}" rows="10">${esc(v)}</textarea>`;
      else if(f.type==='secret')input=`<input id="${id}" type="password" autocomplete="off" placeholder="${meta.secret_set[f.key]?'(set — leave blank to keep)':'(not set)'}">`;
      else if(f.type==='int'||f.type==='float')input=`<input id="${id}" type="number" step="${f.type==='int'?1:'any'}" value="${esc(v)}">`;
      else input=`<input id="${id}" type="text" value="${esc(v)}">`;
      const dflt=f.type==='secret'?'':` <i>default: ${esc(f.default===''?'(empty)':String(f.default).slice(0,60))}</i>`;
      h+=`<div class="row"><label for="${id}">${esc(f.label)}${f.restart?' <span class="badge">restart</span>':''}</label>${input}<div class="help">${esc(f.help||'')}${dflt}</div><div class="err" id="e_${f.key}"></div></div>`;
    }
    h+='</fieldset>';
  }
  $('form').innerHTML=h;
}
function collect(){
  const ch={};
  for(const f of meta.fields){
    const el=$('f_'+f.key);
    if(f.type==='bool')ch[f.key]=el.checked;
    else if(f.type==='secret'){if(el.value)ch[f.key]=el.value}
    else ch[f.key]=el.value;
  }
  return ch;
}
async function save(){
  document.querySelectorAll('.err').forEach(e=>e.textContent='');
  $('status').textContent='saving…';
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({changes:collect()})});
  const d=await r.json();
  if(!d.ok){for(const k in d.errors){const e=$('e_'+k);if(e)e.textContent=d.errors[k]}$('status').textContent='not saved — fix the errors above';return false}
  meta=d;render();$('status').textContent='saved ✓ '+new Date().toLocaleTimeString();return true;
}
async function saveTest(){
  if(!await save())return;
  $('status').textContent='testing…';
  const d=await (await fetch('/api/config/test',{method:'POST'})).json();
  $('status').textContent=d.results.map(x=>x.ok?`✓ ${x.role} ${x.model} (${x.sec}s)`:`✗ ${x.role} ${x.model}: ${x.error}`).join('\n');
}
async function resetAll(){
  if(!confirm('Reset every setting to its default? (the API key is kept)'))return;
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:true})});
  meta=await r.json();render();$('status').textContent='reset ✓';
}
load();
</script></body></html>"""

@app.route("/")
def index():
    page = PAGE.replace("__CSS__", CSS).replace("__CAMP__", html.escape(CAMPAIGN))
    return Response(page, mimetype="text/html")

@app.route("/config")
def config_page():
    return Response(CONFIG_PAGE.replace("__CSS__", CSS), mimetype="text/html")

@app.route("/api/state")
def api_state():
    return jsonify(game_state())

@app.route("/api/turn", methods=["POST"])
def api_turn():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    try:
        return jsonify({"dm": player_turn(text)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

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

@app.route("/api/notes", methods=["GET"])
def api_notes_get():
    return jsonify({"notes": analyst.load_notes(ROOT)})

@app.route("/api/notes/<note_id>/canon", methods=["POST"])
def api_notes_canon(note_id):
    notes = analyst.load_notes(ROOT)
    note = next((n for n in notes if n.get("id") == note_id), None)
    if not note:
        return jsonify({"error": "note not found"}), 404
    if note.get("proposal") and isinstance(note["proposal"], dict):
        ledger = load_ledger(strict=True)
        ledger = apply_ledger_ops(ledger, {"upsert": [note["proposal"]]})
        atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
    else:
        prem = read(PREMISE)
        clean_q = analyst.strip_ooc_prefix(note.get("q", ""))
        addition = f"\n\n## Canon notes\n- {clean_q}: {note.get('a', '')}"
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
    if "--web" in argv:
        if not (cfg.api_key and cfg.model):
            print("[not configured yet — open /config to set the model and API key]")
        print(f"web UI → http://{'localhost' if cfg.host == '127.0.0.1' else cfg.host}:{cfg.port}\n")
        app.run(host=cfg.host, port=cfg.port, debug=False)
    else:
        cli()
