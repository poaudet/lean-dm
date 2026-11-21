# Architecture Decision Records

This file records design decisions for the lean-dm project.

Format per entry:

    ## D-NN  <Title>

    Decision: <the rule the codebase follows>

    Alternatives considered: <rejected options, so future changes know the "why-not">

All entries below are implemented and merged (main as of `898bcbb`). A decision
may only be changed by writing a new decision record that supersedes it.

---

## D-01  State owners

Decision: The existing ledger pass owns `ledger.json`. The per-turn record pass owns the arc clock/progress and `records.jsonl`. The analyst persists only `briefs.jsonl`.

Alternatives considered: Single writer for all state; shared lock around a unified state file.

---

## D-02  Act-boundary planning

Decision: A separate planner call is triggered by code when the act's beats are all done/skipped, or when the record pass flags `act_complete`. The analyst may only flag `act_complete`; it must not trigger the planner itself.

Alternatives considered: Analyst triggers the planner directly; planner runs on every turn and self-determines readiness.

---

## D-03  Model roles

Decision: Settings `analyst_model` and `planner_model` fall back to `scribe_model`, then `model`, when empty. There is no summary compactor: chapters use `scribe_model`.

Alternatives considered: Hard-code analyst/planner to scribe_model; add a single `aux_model` setting for all auxiliary roles.

---

## D-04  Arc scope

Decision: Acts 1–3 are sketched up front at campaign start. Later acts are generated only from the recorded outcomes of the previous act.

Alternatives considered: Generate all acts up front; generate each act only when the previous one completes; generate acts lazily one beat at a time.

---

## D-05  Brief format

Decision: The JSON brief format — fields `intent`, `assessment`, `outcome`, `plot_moves`, `keep_true`, `narration`, `clarify`, `act_complete` — is frozen. Changing it requires a new decision record.

Alternatives considered: Free-form text brief; Markdown brief; schema versioned independently.

---

## D-06  Notes

Decision: OOC notes bypass `history.jsonl` and are stored in `notes.jsonl`. Notes become canon only on explicit player request. Legacy notes already in history and the system-prompt line about them stay untouched.

Alternatives considered: Keep notes in history with a tag; strip notes from context at build time; store notes in the ledger with `src="note"`.

---

## D-07  Precedence

Decision: Narration instruction precedence is per-turn brief > style block > system_prompt. The default system_prompt keeps its "120-220 words" line unchanged; each style preset states explicitly that it overrides length guidance.

Alternatives considered: Last-wins (append order in the system message); style block always wins over system_prompt; brief is advisory only.

---

## D-08  Campaign scope

Decision: Fields flagged `scope="campaign"` can be overridden in `campaigns/<name>/campaign.json`; `settings.json` supplies the global defaults.

Alternatives considered: Per-campaign settings in a separate config file keyed by campaign name in `settings.json`; command-line overrides only; no per-campaign overrides.

---

## D-09  Difficulty bending

Decision: Difficulty bending is runtime state expressed as `effective_deadline_minutes`, not a plan revision. The arc plan (`arc.json`) is never modified during play.

Alternatives considered: Write a new act plan with adjusted beats; store effective difficulty in the brief; expose a difficulty slider that edits campaign.json.

---

## D-10  Spoiler policy

Decision: The UI never shows the arc or the brief's `plot_moves`; it shows only a non-spoiler arc chip (e.g. "Act 2 · active"). The CLI `/brief` command prints the brief only after an explicit spoiler warning.

Alternatives considered: Hide everything arc-related from both UI and CLI; show full arc in a locked "GM screen" panel; no spoiler protection.

---

## D-11  Retry

Decision: `/retry` reuses the stored brief (no analyst re-run). `retry!` re-runs the analyst and then regenerates the DM reply.

Alternatives considered: Always re-run the analyst on retry; never store the brief (always regenerate); single retry command with a flag.

---

## D-12  Pinned ledger entries

Decision: Hand-edited ledger entries carry `pinned: true` set manually. `apply_ledger_ops` skips upserts, closes, and deletes on pinned entries.

Alternatives considered: Separate hand-edit file merged at render time; version-controlled ledger with conflict resolution; no pin support (document that manual edits are fragile).

---

## D-13  Existing campaign adoption

Decision: Arc is opt-in for existing campaigns via `--plan-arc`. New campaigns auto-plan after the first exchange when `arc_enabled` is true in settings.

Alternatives considered: Arc always on for new campaigns, never for old; manual migration script; arc enabled by a per-campaign flag only.

---

## D-14  Analyst latency

Decision: The analyst runs synchronously before the narrator with a configurable `analyst_timeout_sec` (default 25 s). On timeout or error it falls back gracefully: the narrator runs without a brief.

Alternatives considered: Run analyst in parallel with narrator (brief applied retroactively); run analyst async and store for next turn; skip analyst entirely and always use the previous brief.

---

## D-15  Campaigns directory relocation

Decision: `DM_CAMPAIGNS_DIR` overrides the default `campaigns/` root directory when set.

Alternatives considered: Mock all filesystem paths individually in every test; pass `--campaigns-dir` CLI flag.

---

## D-16  Server self-restart

Decision: `POST /api/restart` re-execs `python dm.py --web` via `os.execv` from a delayed daemon thread, replacing the container's main process. Restart-flagged settings (`campaign`, `host`, `port`) take effect on the reboot without restarting Docker. The Werkzeug reloader socket fd is made non-inheritable first so the new process can rebind the port.

Alternatives considered: Kill the container from inside (loses shell access and breaks non-Docker runs); supervisord-style process manager in the container; external watchdog container.

---

## D-17  Config zero values

Decision: `0` is the canonical "off/disabled" value for numeric settings. The config UI renders stored `0` values and submits `0` for cleared numeric fields whose default is `0`; empty input is never persisted for such fields. Backend validation continues to reject non-numeric values.

Alternatives considered: Treat empty as "unset/default" (ambiguous with 0-as-off); store booleans for on/off instead of numeric thresholds.
