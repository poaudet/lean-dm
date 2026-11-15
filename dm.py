#!/usr/bin/env python3
"""
dm.py — lean chat-based RPG dungeon master (terminal + tiny web UI).

Doctrine: files are the source of truth; the context is assembled by code, never trusted to the model.

  campaigns/<name>/
    history.jsonl  append-only log of every exchange (/undo and /retry move old text to undone.jsonl)
    premise.md     pinned canon: character, tone, world rules. Seeded from your first message; edit freely
    summary.md     rolling summary of everything already folded
    facts.json     current facts (the scribe adds AND removes entries)
    state.json     {"folded": N}: how many history messages are already absorbed into summary + facts
    summaries/     archive of every previous summary
    undone.jsonl   exchanges removed by /undo and /retry

Per-turn context = ONE system message (persona + premise + summary + facts) + every message after the
fold pointer. When the unfolded tail passes HISTORY_TURNS + FOLD_TURNS exchanges, a background job folds
the oldest FOLD_TURNS exchanges (player + DM text) into summary + facts and advances the pointer LAST, so a
crash can duplicate work but never lose it. The verbatim window therefore floats between HISTORY_TURNS and
HISTORY_TURNS + FOLD_TURNS exchanges, and /undo and /retry can only reach back into that window.

Modes:
  python dm.py                   terminal play (/undo /retry /context /quit)
  python dm.py --web             small web UI on DM_PORT (default 7860)
  python dm.py --campaign noir
  python dm.py --show-context    print exactly what the next turn would send, then exit

Configuration — env vars only (a .env file next to this script is loaded first):
  DM_BASE_URL    OpenAI-compatible endpoint (default https://openrouter.ai/api/v1)
  DM_API_KEY     API key ("ollama" for local Ollama)          [required]
  DM_MODEL       narrator model                               [required]
  SCRIBE_MODEL   cheap summarizer/extractor model (default: DM_MODEL)
  SCRIBE_TEMP    scribe temperature (default 0; set empty to omit if your model rejects it)
  HISTORY_TURNS  minimum exchanges kept verbatim (default 8)
  FOLD_TURNS     exchanges folded per background pass (default 4)
  FACTS_CAP      max stored facts, newest kept (default 200)
  DM_PORT        web UI port (default 7860)
  CAMPAIGN       default campaign name (default "default")

Local servers: make sure the server's context length (Ollama: num_ctx) exceeds the assembled prompt.
The per-turn "context ≈Nk tok" line and --show-context tell you how big it is.

Dependencies: pip install flask requests
"""

import json, os, sys, pathlib, threading, time, html, re, shutil
import requests
from flask import Flask, request, jsonify, Response

# ---------------- config ----------------

def load_dotenv(path=pathlib.Path(__file__).parent / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)   # real env wins over .env

load_dotenv()

CFG = {
    "base_url":    os.environ.get("DM_BASE_URL", "https://openrouter.ai/api/v1"),
    "api_key":     os.environ.get("DM_API_KEY", ""),
    "model":       os.environ.get("DM_MODEL", ""),
    "scribe":      os.environ.get("SCRIBE_MODEL", ""),
    "scribe_temp": os.environ.get("SCRIBE_TEMP", "0"),
    "hist_n":      max(1, int(os.environ.get("HISTORY_TURNS", "8"))),
    "fold_n":      max(1, int(os.environ.get("FOLD_TURNS", "4"))),
    "facts_cap":   max(1, int(os.environ.get("FACTS_CAP", "200"))),
    "port":        int(os.environ.get("DM_PORT", "7860")),
    "campaign":    os.environ.get("CAMPAIGN", "default"),
}
CFG["scribe"] = CFG["scribe"] or CFG["model"]
SCRIBE_EXTRA = {"temperature": float(CFG["scribe_temp"])} if CFG["scribe_temp"].strip() else {}

CAMPAIGN = CFG["campaign"]
if "--campaign" in sys.argv:
    CAMPAIGN = sys.argv[sys.argv.index("--campaign") + 1]

ROOT = pathlib.Path(__file__).parent / "campaigns" / CAMPAIGN
HISTORY, UNDONE = ROOT / "history.jsonl", ROOT / "undone.jsonl"
PREMISE, SUMMARY = ROOT / "premise.md", ROOT / "summary.md"
FACTS, STATE, ARCHIVE = ROOT / "facts.json", ROOT / "state.json", ROOT / "summaries"

TURN_LOCK = threading.Lock()   # one player action (turn / undo / retry) at a time
FOLD_LOCK = threading.Lock()   # one background fold at a time
FOLD_THREAD = None
LAST = {"est": 0, "prompt_tokens": None}   # size of the last assembled prompt, for display

SYSTEM = """You are the Dungeon Master of a text RPG.
Rules:
- Narrate vividly but concisely (120-220 words per turn). End with the situation awaiting the player's action.
- Never act or decide for the player. Never mention dice or mechanics.
- PREMISE and CURRENT FACTS are canon and override your own recollection. Names, debts, wounds, promises and items persist until the story changes them.
- Speak NPC dialogue directly. Stay consistent with the established world.
- Vary your openings and imagery; never echo phrasing from earlier turns."""

def validate_config():
    missing = [k for k in ("api_key", "model") if not CFG[k]]
    if missing:
        sys.exit(f"Missing required config: {', '.join('DM_' + m.upper() for m in missing)}\n"
                 f"Set them as env vars or in a .env file next to dm.py.")

def print_config():
    key = CFG["api_key"][:6] + "…" if len(CFG["api_key"]) > 6 else "***"
    print(f"endpoint={CFG['base_url']}  key={key}")
    print(f"model={CFG['model']}  scribe={CFG['scribe']}")
    print(f"campaign='{CAMPAIGN}'  window={CFG['hist_n']}+{CFG['fold_n']} exchanges  facts_cap={CFG['facts_cap']}")

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

def load_facts(strict=False):
    try:
        facts = json.loads(FACTS.read_text(encoding="utf-8"))
        if not isinstance(facts, list):
            raise ValueError("expected a JSON array")
        return [f for f in facts if isinstance(f, str)]
    except (ValueError, OSError) as e:
        if strict:
            raise                                       # the fold must abort, never overwrite a hand edit
        warn(f"facts.json unreadable ({e}); ignoring it until it is fixed")
        return []

def save_facts(facts):
    atomic_write(FACTS, json.dumps(facts[-CFG["facts_cap"]:], ensure_ascii=False, indent=1))

def folded():
    try:
        return max(0, int(json.loads(STATE.read_text(encoding="utf-8"))["folded"]))
    except (ValueError, KeyError, TypeError, OSError):
        return 0

def init_files():
    ARCHIVE.mkdir(parents=True, exist_ok=True)          # also creates ROOT
    for p in (HISTORY, UNDONE, SUMMARY):
        if not p.exists():
            p.write_text("", encoding="utf-8")
    if not FACTS.exists():
        FACTS.write_text("[]", encoding="utf-8")
    history = load_history()                            # warns once about unreadable lines
    if not STATE.exists():                              # new campaign, or one that predates the fold design
        n = max(0, len(history) - 2 * CFG["hist_n"]) if read(SUMMARY) else 0
        atomic_write(STATE, json.dumps({"folded": n - n % 2}))
    if history and not read(PREMISE):
        first = next((m["content"] for m in history if m["role"] == "user"), "")
        if first:
            atomic_write(PREMISE, first)

# ---------------- context builder ----------------

def merge_same_role(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    return out

def build_context(history):
    """history must end with the pending player action."""
    parts = [SYSTEM]
    if p := read(PREMISE): parts.append("PREMISE (fixed canon):\n" + p)
    if s := read(SUMMARY): parts.append("STORY SO FAR:\n" + s)
    if f := load_facts():  parts.append("CURRENT FACTS:\n- " + "\n- ".join(f))
    tail = history[min(folded(), len(history) - 1):]
    if tail and tail[0]["role"] == "assistant":         # only if history parity was ever disturbed
        tail = tail[1:]
    return [{"role": "system", "content": "\n\n".join(parts)}] + merge_same_role(tail)

def est_tokens(msgs):
    return sum(len(m["content"]) for m in msgs) // 4

def ctx_label():
    est, real = LAST["est"], LAST["prompt_tokens"]
    if not est:
        return ""
    return f"context ≈{est / 1000:.1f}k tok" + (f" (provider: {real / 1000:.1f}k)" if real else "")

def show_context():
    h = load_history()
    ctx = build_context(h + [{"role": "user", "content": "<your next action>"}])
    for m in ctx:
        print(f"--- {m['role']} ---\n{m['content']}\n")
    print(f"[≈{est_tokens(ctx)} tokens · {len(h) - min(folded(), len(h))} of {len(h)} messages verbatim]")

# ---------------- model calls ----------------

class ApiError(RuntimeError):
    def __init__(self, msg, retry=False):
        super().__init__(msg)
        self.retry = retry

def _post(payload, stream=False):
    url = f"{CFG['base_url'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {CFG['api_key']}"}
    timeout = (10, 300) if stream else 180
    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout, stream=stream)
            if r.status_code >= 400:                    # 4xx other than 429 will never succeed on retry
                raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}",
                               retry=r.status_code in (429, 500, 502, 503, 504))
            return r
        except (requests.ConnectionError, requests.Timeout, ApiError) as e:
            last = e
            if attempt == 2 or (isinstance(e, ApiError) and not e.retry):
                break
            wait = 2 ** attempt
            print(f"[api error ({e}); retry {attempt + 1}/2 in {wait}s]")
            time.sleep(wait)
    raise last

def note_usage(u):
    if isinstance(u, dict) and u.get("prompt_tokens"):
        LAST["prompt_tokens"] = u["prompt_tokens"]

def clean_reply(text):
    """Strip reasoning blocks that some local models emit inline."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:                              # the opening tag lived in the prompt template
        text = text.split("</think>", 1)[1]
    return text.split("<think>", 1)[0].strip()          # unterminated block: drop it

def chat_complete(messages, model=None, *, track=True, **extra):
    data = _post({"model": model or CFG["model"], "messages": messages, "stream": False, **extra}).json()
    if data.get("error"):
        raise RuntimeError(f"API error: {str(data['error'])[:300]}")
    if track:
        note_usage(data.get("usage"))
    msg = ((data.get("choices") or [{}])[0].get("message")) or {}
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model returned an empty reply")
    return content

def chat_stream(messages):
    """Stream the narrator's reply to the terminal; returns the full text."""
    r = _post({"model": CFG["model"], "messages": messages, "stream": True}, stream=True)
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

# ---------------- fold (background summarizer + fact keeper) ----------------

def fold_if_needed():
    """Absorb the oldest exchanges into summary + facts once the verbatim tail has grown too long."""
    if not FOLD_LOCK.acquire(blocking=False):           # single-flight
        return
    try:
        h, k = load_history(), folded()
        if len(h) - k < 2 * (CFG["hist_n"] + CFG["fold_n"]):
            return
        chunk = h[k : k + 2 * CFG["fold_n"]]            # oldest exchanges: player AND DM text
        play = "\n".join(f'{m["role"].upper()}: {m["content"]}' for m in chunk)
        facts = load_facts(strict=True)
        old_summary = read(SUMMARY)

        summary = clean_reply(chat_complete([
            {"role": "system", "content": "You maintain a campaign summary for a text RPG."},
            {"role": "user", "content": f"SUMMARY:\n{old_summary}\n\nNEW PLAY:\n{play}\n\n"
             "Rewrite the summary to include the new play. Max 500 words. Compress the oldest "
             "material first; keep names, open threads, and where things stand."}],
            model=CFG["scribe"], track=False, **SCRIBE_EXTRA))
        raw = clean_reply(chat_complete([
            {"role": "system", "content": "Output only JSON."},
            {"role": "user", "content": f"CURRENT FACTS: {json.dumps(facts)}\n\nNEW PLAY:\n{play}\n\n"
             'Return {"add": [...], "remove": [...]}. Facts are durable things: people, places, items, '
             "debts, promises, injuries, quests, and what the PLAYER declared or did. 'remove' = exact "
             "strings from CURRENT FACTS that are now false, resolved, or superseded."}],
            model=CFG["scribe"], track=False, **SCRIBE_EXTRA))
        if len(summary) < 50:
            raise ValueError("summarizer returned an empty or too-short summary")
        ops = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])

        gone = {x.strip() for x in ops.get("remove", []) if isinstance(x, str)}
        facts = [f for f in facts if f.strip() not in gone]
        facts += [x for x in ops.get("add", []) if isinstance(x, str) and x.strip() and x not in facts]

        # Write order matters: summary, facts, then the pointer LAST. A crash in between can
        # only duplicate work on the next try, never lose it.
        dest = ARCHIVE / f"{k:06d}.md"
        if old_summary and not dest.exists():
            shutil.copy(SUMMARY, dest)                  # keep the summary this fold replaces
        atomic_write(SUMMARY, summary)
        save_facts(facts)
        atomic_write(STATE, json.dumps({"folded": k + len(chunk)}))
        print(f"[fold: messages {k}–{k + len(chunk) - 1} absorbed; {len(facts)} facts]")
    except Exception as e:
        print(f"[fold failed, will retry next turn: {e}]")
    finally:
        FOLD_LOCK.release()

def start_fold():
    global FOLD_THREAD
    FOLD_THREAD = threading.Thread(target=fold_if_needed, daemon=True)
    FOLD_THREAD.start()

# ---------------- game actions (shared by CLI and web) ----------------

def _generate(ctx, stream):
    LAST["est"], LAST["prompt_tokens"] = est_tokens(ctx), None
    dm = clean_reply(chat_stream(ctx) if stream else chat_complete(ctx))
    if not dm:
        raise RuntimeError("model returned an empty reply")
    return dm

def player_turn(text, stream=False):
    with TURN_LOCK:
        history = load_history()
        user = {"role": "user", "content": text}
        dm = _generate(build_context(history + [user]), stream)
        append_jsonl(HISTORY, [user, {"role": "assistant", "content": dm}])   # commit the pair together
        if not history and not read(PREMISE):
            atomic_write(PREMISE, text)                                       # first message = pinned premise
    start_fold()
    return dm

def _last_exchange(lines, h):
    if len(lines) != len(h):
        raise RuntimeError("history.jsonl has unreadable lines; fix it by hand first")
    if len(h) < 2 or h[-1]["role"] != "assistant" or h[-2]["role"] != "user":
        raise RuntimeError("nothing to undo")
    if len(h) - 2 < folded():
        raise RuntimeError("that exchange is already folded into the summary; can't rewind past it")

def undo_turn():
    """Remove the last exchange (kept in undone.jsonl). Returns the player's text."""
    with TURN_LOCK:
        lines, h = raw_history_lines(), load_history()
        _last_exchange(lines, h)
        append_jsonl(UNDONE, lines[-2:])
        atomic_write(HISTORY, "".join(l + "\n" for l in lines[:-2]))
        text = h[-2]["content"]
        if len(h) == 2 and read(PREMISE) == text.strip():
            PREMISE.unlink()                            # undoing the opening un-pins it too
        return text

def retry_turn(stream=False):
    """Regenerate the DM's last reply for the same player action."""
    with TURN_LOCK:
        lines, h = raw_history_lines(), load_history()
        _last_exchange(lines, h)
        dm = _generate(build_context(h[:-1]), stream)
        new = json.dumps({"role": "assistant", "content": dm}, ensure_ascii=False)
        append_jsonl(UNDONE, lines[-1:])
        atomic_write(HISTORY, "".join(l + "\n" for l in lines[:-1]) + new + "\n")
    return dm

def game_state():
    history = load_history()
    return {
        "campaign": CAMPAIGN,
        "turns": sum(1 for m in history if m["role"] == "user"),
        "history": history[-60:],
        "premise": read(PREMISE),
        "summary": read(SUMMARY),
        "facts": load_facts(),
        "ctx": ctx_label(),
    }

# ---------------- web UI ----------------

app = Flask(__name__)

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DM — __CAMP__</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#141220;color:#e8e5f5;display:flex;height:100vh}
#chat{flex:1;display:flex;flex-direction:column;max-width:820px;margin:0 auto;width:100%}
#log{flex:1;overflow-y:auto;padding:16px}
.msg{margin:10px 0;padding:10px 14px;border-radius:10px;white-space:pre-wrap;line-height:1.5}
.user{background:#2b2450;border:1px solid #3d3670;max-width:85%;margin-left:auto}
.dm{background:#1c1930;border:1px solid #2e2950;max-width:95%}
.meta{color:#8b84b8;font-size:.8em;padding:10px 16px 0}
#bar{display:flex;gap:8px;padding:12px;border-top:1px solid #2e2950}
input#in{flex:1;padding:12px;border-radius:8px;border:1px solid #3d3670;background:#1c1930;color:#e8e5f5;font-size:1em}
button{padding:10px 16px;border-radius:8px;border:none;background:#6d4aff;color:#fff;cursor:pointer;font-size:.95em}
button.ghost{background:transparent;border:1px solid #3d3670;color:#c9c3ee}
#side{width:340px;border-left:1px solid #2e2950;display:flex;flex-direction:column}
#tabs{display:flex}#tabs button{flex:1;border-radius:0;background:#1c1930;color:#c9c3ee;padding:10px}
#tabs button.on{background:#2b2450;color:#fff}
#file{flex:1;overflow-y:auto;padding:14px;font-size:.9em;line-height:1.5}
#file h1,#file h2,#file h3{color:#b3a6ff}#file ul{padding-left:18px}
@media(max-width:900px){#side{display:none}}
</style></head><body>
<div id="chat">
  <div class="meta">__CAMP__ — <span id="tn"></span> turns — <span id="ctx"></span> — files in campaigns/__CAMP__/</div>
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
    <button id="t-sum" class="on" onclick="setTab('sum')">Summary</button>
    <button id="t-facts" onclick="setTab('facts')">Facts</button>
  </div>
  <div id="file"></div>
</div>
<script>
const FIRST_FOLD=__FIRST_FOLD__;
const $=id=>document.getElementById(id);
let busy=false,current='sum',state=null;
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(t){ // tiny markdown: headings, bold, italics, hr, lists, paragraphs
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
  else if(current==='sum')f.innerHTML=md(state.summary||'(empty — first fold after '+FIRST_FOLD+' turns)');
  else f.innerHTML='<ul>'+state.facts.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
}
function setTab(t){current=t;['prem','sum','facts'].forEach(x=>$('t-'+x).classList.toggle('on',x===t));renderSide()}
function refresh(full=true){
  return fetch('/api/state').then(r=>r.json()).then(s=>{
    state=s;$('tn').textContent=s.turns;$('ctx').textContent=s.ctx||'';
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
$('in').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
setTab('sum');refresh();setInterval(()=>{if(!busy)refresh(false)},15000);
</script></body></html>"""

@app.route("/")
def index():
    page = (PAGE.replace("__CAMP__", html.escape(CAMPAIGN))
                .replace("__FIRST_FOLD__", str(CFG["hist_n"] + CFG["fold_n"])))
    return Response(page, mimetype="text/html")

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

# ---------------- terminal mode ----------------

def cli():
    history = load_history()
    print(f"=== Campaign '{CAMPAIGN}' — {sum(m['role'] == 'user' for m in history)} turns — "
          f"/undo /retry /context /quit ===\n")
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
            elif player == "/undo":
                print(f"[removed; your move was: {undo_turn()}]\n")
            elif player == "/retry":
                print()
                retry_turn(stream=True)
                print(f"\n\n[{ctx_label()}]\n")
            elif player.startswith("/"):
                print("[commands: /undo /retry /context /quit]\n")
            else:
                print()
                player_turn(player, stream=True)
                print(f"\n\n[{ctx_label()}]\n")
        except KeyboardInterrupt:
            print("\n[interrupted — nothing was saved]\n")
        except Exception as e:
            hint = "" if player.startswith("/") else " — nothing was saved; retype your move"
            print(f"\n[error{hint}: {e}]\n")
    if FOLD_THREAD and FOLD_THREAD.is_alive():
        print("[finishing background fold…]")
        FOLD_THREAD.join(timeout=90)
    print("[campaign saved]")

if __name__ == "__main__":
    show = "--show-context" in sys.argv
    if not show:
        validate_config()
    init_files()
    if show:
        show_context()
        sys.exit()
    print_config()
    if "--web" in sys.argv:
        print(f"web UI → http://localhost:{CFG['port']}\n")
        app.run(host="127.0.0.1", port=CFG["port"], debug=False)
    else:
        cli()
