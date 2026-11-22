# lean-dm

A lean chat-based RPG dungeon master for terminal and web UI. Built with Python 3, Flask, and standard HTTP requests—no JS frameworks, no complex build steps, no heavy database.

Doctrine: **Files are the source of truth; context is assembled by code.**

---

## Features (v4)

- **Verbatim Context & Sliding Window Folding**: Models see the verbatim recent play. Older turns are folded into summary chapters while key facts live in an indexed canon ledger appended to the latest message (provider prefix cache friendly).
- **Hidden Story Arc & Difficulty Pacing (P3)**: Optional hidden 3-act narrative arc (`arc.json`, `arc_snap/`, `records.jsonl`). Background record passes track clock progression, pressure deadlines, and beat resolution. Pure function `allowed_moves()` schedules due antagonist pressure moves with difficulty bending (`story`, `standard`, `hard`). Hard never bends. The hidden arc never leaks to player transcripts or UI.
- **Director's Analyst Brief (P2)**: Optional pre-turn director call producing structured JSON guidance (`briefs.jsonl`) clamped in code (intent, assessment, outcome, plot moves, keep-true items). Fallback never blocks play.
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
├── storage.py                # Atomic writes, JSONL append, and model JSON parsing
├── styles/                   # Markdown writing style presets
│   ├── subtle.md
│   ├── pulp.md
│   ├── noir.md
│   └── terse.md
├── web/                      # Standalone static web UI assets (no build step)
│   ├── index.html
│   ├── config.html
│   ├── app.js
│   └── style.css
├── decisions.md              # Architecture decision records (D-01 through D-18)
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
        ├── state.json        # Engine pointers (folded, ledgered, audited)
        ├── briefs.jsonl      # Analyst director briefs keyed by player msg index
        ├── notes.jsonl       # OOC notes & canon proposals
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
| `style_enabled` | bool | `false` | campaign | Style | Enable style enforcement |
| `style_preset` | select | `none` | campaign | Style | Preset: `none`, `subtle`, `pulp`, `noir`, `terse` |
| `style_words_min` | int | `0` | campaign | Style | Minimum words per reply (0 = off) |
| `style_words_max` | int | `0` | campaign | Style | Maximum words per reply (0 = off) |
| `style_push` | int | `60` | campaign | Style | Adherence push factor (0–100) |
| `style_notes` | text | `""` | campaign | Style | Freeform writing style notes |
| `style_guard` | bool | `false` | campaign | Style | Prevent repetitive openers and trigrams |
| `analyst_enabled` | bool | `false` | campaign | Analyst | Generate pre-turn director's brief |
| `analyst_timeout_sec` | int | `25` | global | Reliability | Analyst timeout in seconds |
| `notes_routing` | bool | `false` | campaign | Notes | Intercept OOC notes (`//`, `ooc:`) |
| `web_password` | secret | `""` | global | App | HTTP Basic authentication password |

---

## CLI Usage & Commands

The web server refuses to start on a non-loopback address unless `web_password` is
configured. For local-only use, set `host` to `127.0.0.1`; for Docker or other
network access, configure `DM_WEB_PASSWORD` before starting the container.

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
- `/ledger`: Displays the active and resolved canon ledger entries.
- `/context`: Displays assembled context.
- `/rebuild`: Rebuilds the canon ledger from raw history.
- `/audit`: Executes a continuity audit.
- `/quit`: Exits cleanly after waiting for background maintenance threads.

---

## Web API Endpoints

- `GET /`: Game interface.
- `GET /config`: Settings management interface.
- `GET /api/state`: Returns game status, turns, ledger, premise, and non-spoiler arc chip.
- `POST /api/turn`: Standard turn completion (`{"text": "..."}`).
- `POST /api/stream`: Server-Sent Events (SSE) streaming turn with stages: `analyzing` → `narrating` → `recording`.
- `POST /api/undo`: Rewinds last exchange.
- `POST /api/retry`: Regenerates DM reply.
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
python -m pytest tests/
```

Test coverage:
- `tests/test_p0.py`: Characterization baseline (golden context, undo/retry guards, atomic order).
- `tests/test_p1.py`: Config schema, campaign overlay, writing style injection, deterministic style guard, word counting.
- `tests/test_p2.py`: Analyst briefs, timeouts, fallbacks, OOC notes routing, canon promotion.
- `tests/test_p3.py`: Hidden arc, scheduling, difficulty bending table, revision diff guards, sentinel leak tests.
- `tests/test_p4.py`: Web UI assets, SSE streaming, HTTP Basic auth, premise/ledger editing, ZIP and transcript exports.

---

## Upgrade Notes (v3 to v4)

1. **Backwards Compatibility**: With all new features at their defaults (`false` / `none` / `""`), prompt generation is byte-identical to v3 (Invariants I11).
2. **Opt-in Arc**: Existing campaigns continue unchanged. Enable arc management by setting `arc_enabled: true` in settings or running `python dm.py --campaign <name> --plan-arc`.
3. **No Migration Needed**: Old `settings.json` files automatically load with sensible defaults for new fields.
