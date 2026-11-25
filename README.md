# lean-dm

A lean chat-based RPG dungeon master for terminal and web UI. Built with Python 3, Flask, and standard HTTP requests—no JS frameworks, no complex build steps, no heavy database.

Doctrine: **Files are the source of truth; context is assembled by code.**

---

## Features (v4)

- **Verbatim Context & Sliding Window Folding**: Models see the verbatim recent play. Older turns are folded into summary chapters while key facts live in an indexed canon ledger appended to the latest message (provider prefix cache friendly).
- **Hidden Story Arc & Difficulty Pacing (P3)**: Optional hidden 3-act narrative arc (`arc.json`, `arc_snap/`, `records.jsonl`). Per-turn record passes track clock progression, pressure deadlines, and beat resolution. Pure function `allowed_moves()` schedules due antagonist pressure moves with difficulty bending (`story`, `standard`, `hard`). Hard never bends. The hidden arc never leaks to player transcripts or UI.
- **Beat Hooks & Mandatory Moves (WP2)**: Each beat carries a spoiler-safe `hook` — a lead the player can perceive — which `allowed_moves()` pushes once the act stalls. Every due move is mandatory: the analyst plays it or lists it in `deferred`, and `clamp_brief` forces in anything it drops. A hook escalates subtle → overt → offer while it goes unanswered.
- **Arc Drift & Act Pacing (WP6)**: The record pass tracks on-arc progress, replans after `drift_turns` consecutive off-arc turns, retains superseded beats for history, and can force an act transition after `act_max_turns`.
- **Canon Naming & Merge (WP7)**: Ledger entries keep up to six alternate names; lookups resolve aliases, duplicate entries can be merged, and continuity audits report alias collisions.
- **Steering State (WP1)**: Optional player-facing objective and open question (`steering.json`), written by the record pass after each reply and shown in the web UI's Now panel and the CLI `/now` command. Contains no arc data.
- **Narrator Contract (WP4)**: Every reply has three parts — the consequence, at most one new fact, and a closing line stating the live decision. The brief's fields are defined for the narrator in the prompt, so `end`, `push`, `clarify` and `intent=ask` work with style enforcement off. `brief.narration.words` is the single word target.
- **Director's Analyst Brief (P2)**: Optional pre-turn director call producing structured JSON guidance (`briefs.jsonl`) clamped in code (intent, assessment, outcome, plot moves, keep-true items). Fallback never blocks play; its diagnostic category is stored outside the narrator-visible brief.
- **Out-of-Character (OOC) Notes Routing (P2)**: Messages starting with `//`, `ooc`, or `note to dm:` bypass `history.jsonl` and are answered out of character in `notes.jsonl`. Promoted to canon only upon explicit request.
- **Writing Style Enforcement & Style Guard (P1)**: Style presets in `styles/` (`subtle`, `pulp`, `noir`, `terse`) with word count ranges, push factor, and style notes. Pure deterministic `style_guard` prevents repetitive sentence openers and overused 3-grams.
- **Modern Responsive Web UI & SSE Streaming (P4)**: Dark/light themes, mobile drawer, textarea auto-grow, SSE streaming (`/api/stream`), live stage indicators (`analyzing` → `narrating` → `recording`), premise/ledger/notes editors, HTTP Basic auth, and campaign transcript export.

---

## File Layout

```
lean-dm/
├── dm.py                     # Main entry point, Campaign context, Flask web & CLI
├── config.py                 # Configuration schema, coercion & dual-scope persistence
├── analyst.py                # Pre-turn director's brief & OOC notes routing
├── arc.py                    # Hidden arc planning, difficulty scheduling & record pass
├── steering.py               # Player-facing steering state (objective, open question)
├── campaign.py               # Campaign filesystem paths and locks
├── ledger.py                 # Canon ledger operations and rendering
├── narrator_context.py       # Narrator context, brief field meanings, word target
├── storage.py                # Atomic writes, JSONL append, and model JSON parsing
├── style_guard.py            # Deterministic style repetition guard
├── styles/                   # Markdown writing style presets
│   ├── subtle.md
│   ├── pulp.md
│   ├── noir.md
│   └── terse.md
├── web/                      # Standalone static web UI assets (no build step)
│   ├── index.html
│   ├── config.html
│   ├── app.js
│   ├── now.js
│   └── style.css
├── decisions.md              # Architecture decision records (D-01 through D-27)
├── Dockerfile                # Container definition
├── docker-compose.yaml
└── campaigns/
    ├── settings.json         # Global settings defaults
    └── <campaign_name>/
        ├── campaign.json     # Per-campaign setting overrides
        ├── history.jsonl     # Append-only conversation transcript (role + content only)
        ├── undone.jsonl      # Archive of undone exchanges
        ├── premise.md        # Pinned campaign premise
        ├── summary.md        # Folded chapter summaries
        ├── ledger.json       # Canon state index (entities, items, threads, clocks)
        ├── audit.md          # Continuity audit report
        ├── state.json        # Engine pointers (folded, ledgered, audited, shape_ok)
        ├── briefs.jsonl      # Analyst director briefs keyed by player msg index
        ├── notes.jsonl       # OOC notes & canon proposals
        ├── steering.json     # Player-facing objective and open question
        ├── steering_snap/    # Pre-turn steering snapshots for safe undo/retry
        │   └── <msg>.json
        ├── arc.json          # Hidden narrative arc & pressure clock (never leaked)
        ├── arc_log.jsonl     # Arc revision log (act boundaries)
        ├── records.jsonl     # Per-turn record pass output
        └── arc_snap/         # Pre-turn arc snapshots for safe undo/retry
            └── <msg>.json
```

---

## Configuration Reference

Settings are stored in `campaigns/settings.json` (override with `DM_SETTINGS`). Campaign-scoped settings can be overridden in `campaigns/<name>/campaign.json`.

| Key | Type | Default | Scope | Group | Description |
|---|---|---|---|---|---|
| `base_url` | str | `https://openrouter.ai/api/v1` | global | Connection | OpenAI-compatible endpoint |
| `api_key` | secret | `""` | global | Connection | API key (never sent to browser) |
| `model` | str | `""` | global | Connection | Narrator model |
| `fallback_model` | str | `""` | global | Connection | Fallback model on retry exhaustion |
| `scribe_model` | str | `""` | global | Connection | Model for ledger/summary/records |
| `analyst_model` | str | `""` | global | Connection | Model for director brief (empty = scribe) |
| `planner_model` | str | `""` | global | Connection | Model for arc planning (empty = scribe) |
| `ctx_budget_tok` | int | `350000` | global | Context | Token threshold to trigger folding |
| `ctx_target_tok` | int | `200000` | global | Context | Token target after folding |
| `ledger_enabled` | bool | `true` | global | Context | Enable keyed canon ledger |
| `difficulty` | select | `standard` | campaign | Gameplay | Pacing: `story`, `standard`, `hard` |
| `arc_enabled` | bool | `false` | campaign | Gameplay | Enable hidden narrative arc manager |
| `drift_turns` | int | `4` | campaign | Arc | Consecutive off-arc turns before a replan is triggered |
| `act_max_turns` | int | `0` | campaign | Arc | Max turns in an act before forced transition (0 = off) |
| `steering_enabled` | bool | `false` | campaign | Gameplay | Track the player-facing objective and open question |
| `style_enabled` | bool | `false` | campaign | Style | Enable style enforcement |
| `style_preset` | select | `none` | campaign | Style | Preset: `none`, `subtle`, `pulp`, `noir`, `terse` |
| `style_words_min` | int | `0` | campaign | Style | Minimum words per reply (0 = off) |
| `style_words_max` | int | `0` | campaign | Style | Maximum words per reply (0 = off) |
| `style_push` | int | `60` | campaign | Style | Adherence push factor (0–100) |
| `style_notes` | text | `""` | campaign | Style | Freeform writing style notes |
| `style_guard` | bool | `false` | campaign | Style | Prevent repetitive openers and trigrams |
| `analyst_enabled` | bool | `false` | campaign | Analyst | Generate pre-turn director's brief |
| `analyst_timeout_sec` | int | `25` | global | Reliability | Analyst timeout in seconds |
| `analyst_retries` | int | `1` | global | Reliability | Extra analyst attempts on 429/5xx |
| `analyst_summary_tok` | int | `4000` | global | Reliability | Summary budget sent to the analyst |
| `overlong_ratio` | float | `1.5` | global | Reliability | Overlong threshold as a multiple of the word target |
| `notes_routing` | bool | `true` | campaign | Notes | Intercept OOC notes (`//`, `ooc`, `note to dm:`) |
| `notes_in_context` | int | `5` | campaign | Notes | Recent OOC answers the narrator is told to treat as true |
| `web_password` | secret | `""` | global | App | HTTP Basic authentication password |

Arc recording runs synchronously to keep undo and retry ordered. The unused
`record_wait_sec` setting is ignored and removed on the next settings save.

---

## CLI Usage & Commands

The web server defaults to `127.0.0.1` (the Docker image sets `DM_HOST=0.0.0.0`) and
refuses to start on a non-loopback address unless `web_password` is configured.
`DM_WEB_PASSWORD` and `DM_HOST` are runtime overrides: they win over `settings.json`
on every start and are never written to it. Other `DM_*` variables only seed the first run.
When using the Compose bind mount, ensure the host `campaigns/` directory is
writable by the image's non-root `dm` user (uid 1000), or set `user:` in the compose file.
Behind a TLS-terminating reverse proxy set `DM_TRUST_PROXY=1` so the scheme, host and client
address come from the `X-Forwarded-*` headers (trust only a proxy you control).
After ten failed authentication attempts, further attempts from that address
are temporarily throttled; the healthcheck remains available.

```bash
# Start in terminal mode
python dm.py

# Start with web UI
python dm.py --web

# Select a specific campaign
python dm.py --campaign noir

# Plan arc for an existing campaign
python dm.py --campaign noir --plan-arc

# Continuity audit
python dm.py --audit

# Inspect context that would be sent to the model on next turn
python dm.py --show-context
```

### In-Game CLI Commands

- `/undo`: Rewinds the last turn, restoring input.
- `/retry`: Regenerates the last DM reply reusing the stored brief (D-11).
- `retry!`: Re-runs the analyst model and regenerates the DM reply.
- `/brief`: Displays the analyst brief for the last turn after an explicit spoiler confirmation (D-10).
- `/notes`: Lists out-of-character notes and their canon status.
- `/now`: Shows the current objective and open question.
- `/ledger`: Displays the active and resolved canon ledger entries.
- `/context`: Displays assembled context.
- `/rebuild`: Rebuilds the canon ledger from raw history.
- `/audit`: Executes a continuity audit.
- `/quit`: Exits cleanly after waiting for background maintenance threads.

---

## Web API Endpoints

- `GET /healthz`: Unauthenticated liveness probe for container healthchecks.
- `GET /`: Game interface.
- `GET /config`: Settings management interface.
- `GET /api/state`: Returns game status, turns, ledger, premise, non-spoiler arc chip, the player-facing steering fields, and the `shape_ok` / `overlong` post-check flags.
- `POST /api/turn`: Standard turn completion (`{"text": "..."}`).
- `POST /api/stream`: Server-Sent Events (SSE) streaming turn with stages: `analyzing` → `narrating` → `recording`.
- `POST /api/undo`: Rewinds last exchange.
- `POST /api/retry`: Regenerates DM reply. Accepts `{"shorter": true}` to retry with the word budget multiplied by 0.7 (E4).
- `POST /api/premise`: Atomically updates `premise.md` (retaining `premise.md.bak`).
- `POST /api/ledger`: Pin, delete, or upsert entries (`{"action": "pin|delete|upsert", ...}`).
- `GET /api/notes`: Returns OOC notes list.
- `POST /api/notes/<id>/canon`: Promotes an OOC note or proposal to canon.
- `GET /api/export/transcript`: Downloads campaign transcript as Markdown.
- `GET /api/export/campaign.zip`: Downloads campaign archive zip (arc internals excluded by default; add `?include_arc=1` to include them).
- `GET /api/campaigns` & `POST /api/campaigns`: List, create, or duplicate campaigns.

---

## Running Tests

Tests run using pytest and use temporary isolated directories via `DM_CAMPAIGNS_DIR`. They mock LLM API calls and never touch real campaigns.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/
ruff check dm.py analyst.py arc.py campaign.py config.py ledger.py narrator_context.py steering.py storage.py style_guard.py
mypy --strict campaign.py storage.py style_guard.py
```

Test coverage:
- `tests/test_p0.py`: Characterization baseline (golden context, undo/retry guards, atomic order).
- `tests/test_p1.py`: Config schema, campaign overlay, writing style injection, deterministic style guard, word counting.
- `tests/test_p2.py`: Analyst briefs, timeouts, fallbacks, OOC notes routing, canon promotion.
- `tests/test_p3.py`: Hidden arc, scheduling, difficulty bending table, and sentinel leak tests.
- `tests/test_p4.py`: Web UI assets, SSE streaming, HTTP Basic auth, premise/ledger editing, ZIP and transcript exports.
- `tests/test_wp1_steering.py`: Steering state — single writer, rollback, prompt injection, UI fields.
- `tests/test_wp2_beat_moves.py`: Beat hooks, mandatory moves, escalation, and the fallback that plays.
- `tests/test_wp3_analyst_inputs.py`: Analyst inputs, ledger-backed keep_true, retry, built fallback.
- `tests/test_wp4_narrator_contract.py`: Reply shape, field meanings, word target, post-check.
- `tests/test_wp5_prompt_contracts.py`: Analyst, record, planner, ledger and narrator prompt contracts.
- `tests/test_wp6_wp7_acceptance.py`: Arc validation/retry/logging and ledger alias merge/lookup acceptance.
- `docs/playtest.md`: Manual steering and drift/replan playtest procedure.

---

## Upgrade Notes (v3 to v4)

1. **Backwards Compatibility**: With all new features at their defaults (`false` / `none` / `""`), prompt generation is byte-identical to v3 (Invariants I11).
2. **Opt-in Arc**: Existing campaigns continue unchanged. Enable arc management by setting `arc_enabled: true` in settings or running `python dm.py --campaign <name> --plan-arc`.
3. **Opt-in Steering**: Set `steering_enabled: true` to track the objective and open question. It works with or without the arc.
4. **No Migration Needed**: Old `settings.json` files automatically load with sensible defaults for new fields.
5. **Custom system prompts**: `DEFAULT_SYSTEM` was rewritten in the section 9 format and no longer states a word count. A campaign with a custom `system_prompt` keeps it unchanged, but will not get the E1 reply shape or the E5/E6 rules unless it is updated by hand.