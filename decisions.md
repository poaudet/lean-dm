# Architecture Decision Records

This file records design decisions for the lean-dm project. Each entry is "proposed" until implemented and merged.

---

## D-01  State owners

Status: proposed

Context: Multiple subsystems (ledger pass, record pass, analyst) write to overlapping state files, creating race conditions and unclear ownership.

Decision: The existing ledger pass owns `ledger.json`. The new per-turn record pass owns the arc clock/progress and `records.jsonl`. The analyst persists only `briefs.jsonl`.

Alternatives considered: Single writer for all state; shared lock around a unified state file.

Impact on existing logic: none — the ledger pass already owns `ledger.json`; new files are additive.

Migration: New files (`records.jsonl`, `briefs.jsonl`) are created on first use; no migration of existing campaigns needed.

---

## D-02  Act-boundary planning

Status: proposed

Context: Deciding when to trigger act-boundary planning (generating the next act's outline) needs a clear owner to avoid duplicate calls or missed transitions.

Decision: A separate planner call is triggered by code when the act's beats are all done/skipped, or when the record pass flags `act_complete`. The analyst may only flag `act_complete`; it must not trigger the planner itself.

Alternatives considered: Analyst triggers the planner directly; planner runs on every turn and self-determines readiness.

Impact on existing logic: none — act planning is new functionality.

Migration: n/a — new feature, no existing state to migrate.

---

## D-03  Model roles

Status: proposed

Context: The narrator and scribe use the same model pool. New analyst and planner roles need model assignment with sensible fallback.

Decision: New settings `analyst_model` and `planner_model` are added; an empty value falls back to `scribe_model`, then `model`. v3 has no summary compactor: chapters use `scribe_model`.

Alternatives considered: Hard-code analyst/planner to scribe_model; add a single `aux_model` setting for all auxiliary roles.

Impact on existing logic: none — new settings with empty-defaults fall back to existing behavior.

Migration: Existing `settings.json` files load without change; new keys default to empty.

---

## D-04  Arc scope

Status: proposed

Context: Pre-generating all acts up front risks stale plans that diverge from actual play; generating nothing up front leaves the story unanchored.

Decision: Acts 1–3 are sketched up front at campaign start. Later acts are generated only from the recorded outcomes of the previous act.

Alternatives considered: Generate all acts up front; generate each act only when the previous one completes; generate acts lazily one beat at a time.

Impact on existing logic: none — arc planning is new; existing campaigns without `--plan-arc` are unaffected.

Migration: Existing campaigns adopt arc via `--plan-arc` (opt-in, see D-13).

---

## D-05  Brief format

Status: proposed

Context: The per-turn analyst brief needs a stable schema that the narrator, planner, and UI can all depend on.

Decision: The JSON brief format in the spec — fields `intent`, `assessment`, `outcome`, `plot_moves`, `keep_true`, `narration`, `clarify`, `act_complete` — is final and must not be changed without a new decision record.

Alternatives considered: Free-form text brief; Markdown brief; schema versioned independently.

Impact on existing logic: none — briefs are a new addition.

Migration: n/a — new file format; old campaigns start without briefs.

---

## D-06  Notes

Status: proposed

Context: Out-of-character (OOC) notes currently go into `history.jsonl`, polluting the verbatim play transcript and making them visible to the narrator in context.

Decision: OOC notes bypass `history.jsonl`; they are stored in `notes.jsonl`. Notes become canon only on explicit player request. Legacy notes already in history and the system-prompt line about them stay untouched.

Alternatives considered: Keep notes in history with a tag; strip notes from context at build time; store notes in the ledger with `src="note"`.

Impact on existing logic: Minimal — the existing system-prompt note-handling line is left in place; new notes are redirected to `notes.jsonl`.

Migration: Existing notes in `history.jsonl` remain there; they are not migrated automatically.

---

## D-07  Precedence

Status: proposed

Context: When per-turn briefs, the style block, and the system prompt all give conflicting narration instructions, the narrator needs a clear priority order.

Decision: Per-turn brief > style block > system_prompt. The default system_prompt keeps its "120-220 words" line unchanged; the style block must state explicitly that it overrides length guidance.

Alternatives considered: Last-wins (append order in the system message); style block always wins over system_prompt; brief is advisory only.

Impact on existing logic: none — style block and brief are new; existing system_prompt behavior is unchanged.

Migration: n/a — new features; existing campaigns see no change.

---

## D-08  Campaign scope

Status: proposed

Context: Some settings (e.g. tone, genre) are campaign-specific while others (API key, model) are global. Mixing them in `settings.json` makes per-campaign overrides awkward.

Decision: Fields flagged `scope="campaign"` can be overridden in `campaigns/<name>/campaign.json`. `settings.json` supplies the global defaults.

Alternatives considered: Per-campaign settings in a separate config file keyed by campaign name in `settings.json`; command-line overrides only; no per-campaign overrides.

Impact on existing logic: none — `campaign.json` is a new optional file; existing behavior is unaffected if absent.

Migration: n/a — no existing campaigns have `campaign.json`; they continue to use global defaults.

---

## D-09  Difficulty bending

Status: proposed

Context: When the DM adjusts challenge level mid-session, it should not alter the arc plan (which describes story beats, not difficulty).

Decision: Difficulty bending is runtime state expressed as `effective_deadline_minutes`, not a plan revision. The arc plan is not modified.

Alternatives considered: Write a new act plan with adjusted beats; store effective difficulty in the brief; expose a difficulty slider that edits campaign.json.

Impact on existing logic: none — runtime state is new; plan files are not touched.

Migration: n/a — `effective_deadline_minutes` is a new runtime field.

---

## D-10  Spoiler policy

Status: proposed

Context: The arc plan and brief contain plot moves that would ruin surprise if shown in the game UI.

Decision: The UI never shows the arc or the brief's `plot_moves`. The UI shows only a non-spoiler arc chip (e.g. "Act 2 · active"). The CLI `/brief` command prints the brief only after an explicit spoiler warning.

Alternatives considered: Hide everything arc-related from both UI and CLI; show full arc in a locked "GM screen" panel; no spoiler protection.

Impact on existing logic: none — arc/brief display is new; existing UI is unaffected.

Migration: n/a — purely a UI policy for new features.

---

## D-11  Retry

Status: proposed

Context: `/retry` regenerates the DM's last reply. It is ambiguous whether the analyst brief should be regenerated too, which could change the narrative direction unexpectedly.

Decision: `/retry` reuses the stored brief (no analyst re-run). `retry!` re-runs the analyst and then regenerates the DM reply.

Alternatives considered: Always re-run the analyst on retry; never store the brief (always regenerate); single retry command with a flag.

Impact on existing logic: Existing `/retry` behavior (regenerate narrator reply) is unchanged; `retry!` is new.

Migration: n/a — `retry!` is a new command; `/retry` behavior is preserved.

---

## D-12  Pinned ledger entries

Status: proposed

Context: Hand-edited ledger entries get overwritten by automated ledger passes, losing manual corrections.

Decision: Hand-edited entries get `pinned: true` set manually. `apply_ledger_ops` must skip upserts, closes, and deletes on pinned entries.

Alternatives considered: Separate hand-edit file merged at render time; version-controlled ledger with conflict resolution; no pin support (document that manual edits are fragile).

Impact on existing logic: `apply_ledger_ops` gains a pinned-entry guard. Existing entries without `pinned` behave as before.

Migration: Existing ledger entries are not pinned; users who want to protect an entry must add `"pinned": true` by hand.

---

## D-13  Existing campaign adoption

Status: proposed

Context: Existing campaigns should not be forced into arc planning, which could disrupt ongoing play.

Decision: Arc is opt-in for existing campaigns via `--plan-arc`. New campaigns auto-plan after the first exchange when `arc_enabled` is true in settings.

Alternatives considered: Arc always on for new campaigns, never for old; manual migration script; arc enabled by a per-campaign flag only.

Impact on existing logic: none — existing campaigns without `--plan-arc` or `arc_enabled` are completely unaffected.

Migration: Run `python dm.py --plan-arc` on an existing campaign to generate the initial arc.

---

## D-14  Analyst latency

Status: proposed

Context: Running the analyst synchronously before the narrator adds latency. If the analyst times out or errors, the turn should still complete.

Decision: The analyst runs synchronously before the narrator with a configurable `analyst_timeout_sec` (default 25 s). On timeout or error it falls back gracefully (narrator runs without a brief).

Alternatives considered: Run analyst in parallel with narrator (brief applied retroactively); run analyst async and store for next turn; skip analyst entirely and always use the previous brief.

Impact on existing logic: none — analyst is new; existing narrator call is unchanged.

Migration: n/a — `analyst_timeout_sec` defaults to 25 s; no existing config changes needed.

---

## D-15  Campaigns directory relocation

Status: proposed

Context: Tests need to isolate campaign data completely without touching real user campaigns in `campaigns/`.

Decision: Introduce `DM_CAMPAIGNS_DIR` environment variable override in `dm.py`. If set, it overrides the default `campaigns/` root directory.

Alternatives considered: Mock all filesystem paths individually in every test; pass `--campaigns-dir` CLI flag.

Impact on existing logic: Defaults to existing `campaigns/` directory if env var is not set.

Migration: None needed. Existing campaigns remain in `campaigns/`.
