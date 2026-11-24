# Lean-DM: Steering Requirements v2 (remaining work for Copilot)

Project: lean-dm · Owner: P-O · Audience: Copilot (implementing agent) · Status: ready for implementation · updated 24 November 2026

This document is self-contained. It was written after reviewing the repo files shared on 24 November 2026, after FIX1 to FIX4, FIX6, WP6 and most of WP7 landed against the first steering handoff (November 2026). Requirement IDs (A1 to G3) are kept so they match decisions.md. Everything Copilot needs is below: the context, the invariants, the current state, what each remaining item must do, and how to test it.

## Context and invariants

lean-dm is a chat-based RPG dungeon master (Python, Flask, terminal and web UI). Files are the source of truth and context is assembled by code. The goal of this work: after every reply the player can tell what is being asked of them and where the story is pushing, without asking out-of-character questions.

**Moving parts.** The analyst (`analyst.py`) writes a JSON director's brief before each reply. The hidden arc (`arc.py`) holds acts, beats and timed pressures. The record pass runs after each reply and updates the arc clock and the steering state. The narrator prompt is built in `narrator_context.py`. The OOC path answers `//` messages outside history. The ledger (`ledger.py`) is keyed canon state. `decisions.md` holds the decision records (D-NN).

**Already built (do not regress):**

- Steering: `steering.json` (objective, open question, live lead), written only by the record pass, snapshotted for undo and retry, shown in the Now panel and `/now`.
- Beat hooks and moves: each beat has a player-visible hook; `allowed_moves()` returns due moves; hooks escalate subtle, overt, offer while unanswered; every due move is mandatory (played or deferred); the fallback brief plays them.
- Analyst inputs: ledger, premise, summary, previous brief, hooks and steering; `keep_true` resolved from ledger ids with pinned entries added; one retry on 429 or 5xx.
- Narrator contract: three-part reply, brief fields defined in the prompt, one word target from the brief, post-check flags `overlong` and `shape_ok` in state, retry with a 0.7 budget through `/api/retry` with `{"shorter": true}`.
- OOC: only `//`, `ooc`, `ooc:` and `note to dm:`; routing on by default; the last five answers go to the narrator; an ask intent plays no moves.
- Earlier defects: (1) the rerun-analyst retry path now passes the ledger and act goal; (2) the default system prompt no longer states its own word count; (3) the analyst request was not bounded by its timeout (fixed: the request timeout now follows the analyst budget).

**Invariants (hold for every change):**

- I3: volatile additions (ledger, guard, brief, steering, notes) go only in the last user message. Everything before it stays append-only so prefix caching works.
- I6: fallback never blocks play. Any new LLM call that fails degrades to code-built defaults.
- I8: the hidden arc never appears in history, premise, summary, ledger, audit, steering.json, any API or UI response, or default logs. Beat text, beat ids, pressure ids and antagonist data stay out of player-facing surfaces. Hooks are spoiler-safe: they never name the antagonist or reveal the solution.
- D-09: difficulty bending is runtime state (`effective_deadline_minutes`), not a plan revision.
- D-10: the UI shows only the non-spoiler arc chip and the Now panel.
- State pointers are written last, and all model output is clamped and validated in code.
- /undo and /retry roll back every piece of state, including anything new.
- Pinned ledger entries are never modified by automatic passes.

**Prompt format.** Static rules go in the system message, variable data in the user message. Sections: ROLE (one sentence); INPUTS (each block in authority order; on conflict the ledger beats the summary, which beats recent play); TASK; OUTPUT (the exact shape; for JSON one schema with types, enums and limits, then "Output only the JSON object, no code fences, no commentary"); RULES (numbered, testable, numbers from the brief or settings, never hard-coded twice); WHEN UNSURE (the safe default); EXAMPLES (one good, one bad). Keep each prompt under about 400 words.

## 1. Status by requirement

| Package | IDs | Status |
| --- | --- | --- |
| WP1 Steering | A1 to A6 | Covered |
| WP2 Arc to narrator | B1 to B6 | Covered (the defer cap is now wired) |
| WP3 Analyst | C1 to C4 | Covered (pin markers, prompt cap, bounded request) |
| WP4 Narrator | E1 to E6 | Covered, with one bug: A4 below |
| WP5 OOC | D1 to D6 | Covered |
| Earlier gaps | FIX1 to FIX4, FIX6, WP0 | Done |
| WP6 Arc sync | F1 to F5 | Code present, **bugs**: A1, A2, A3, A5, A6 |
| WP7 Canon naming | G1, G2 | Code present, **bugs**: B1, B2 |
| WP7 | G3 | **Open**: B4 |
| Prompt rewrites | analyst, record, ledger, planner | **Open** |
| Docs and housekeeping |  | **Open** |
| Tests |  | Not reviewed: the test files were not in the review set |

Settings: every planned setting exists, including `drift_turns` and `act_max_turns`. The README settings table does not list the two new ones yet.

## 2. How to work from this document

1. Rules: no new dependencies, tests use a fake `chat_fn`, no behavior change outside the requirements, record conflicts in `decisions.md`.
2. One branch and one PR per package, in the order below. Each PR lists requirement IDs covered and tests added, and ships alone with the suite green.
3. The invariants in the Context section above apply to every change; they are also in `.github/copilot-instructions.md`. Pinned ledger entries are never touched by automatic passes. /undo and /retry must roll back every new piece of state.
4. Rewrite prompts you touch in the prompt format described above (ROLE, INPUTS, TASK, OUTPUT, RULES, WHEN UNSURE, EXAMPLES), under about 400 words each.

## 3. WP-A: Fix the bugs in WP6 and the retry-shorter flag

Found by reading the code. Each item names where it happens, what goes wrong, the fix and the acceptance test.

**A1 Plan validation rejects the planner's own plan.** `validate_arc` requires three to five beats in every act, but the planner schema plans acts 2 and 3 with empty beats (they are generated when the act starts). As written, `plan_initial_arc` and every replan fail validation, so an arc cannot be created. Fix: apply the three-to-five limit only to the active act and to any act that already has beats, counting only beats that are not superseded; an act with no beats is valid while its status is `planned`. Also check the beat `hook` is non-empty (today the check tests `text` while its message says hook). Accept: an arc with empty acts 2 and 3 validates; an active act with two beats, or a beat without a hook, does not.

**A2 A replan never resets the off-arc counter.** In `run_record_pass` the reset `rt["off_arc_turns"] = 0` runs after `save_arc`, so it is not saved, and the replan fires again on every following off-arc turn. Fix: reset the counter before saving, on success and on failure (a failing planner is then retried every `drift_turns` turns, not every turn), and record the choice in `decisions.md`. Accept: after a replan the saved arc shows zero off-arc turns.

**A3 A replan drops the old beats.** `plan_replan_arc` replaces the act's beat list, so superseded beats disappear; superseded and blocked beats must never be deleted (F1). New ids such as `a1b1` can also collide with ids already used in `records.jsonl`. The function also marks the old beats superseded in place before calling the planner, so a failed call leaves them marked. Fix: keep the old beats as `superseded`, append the new ones, give new ids that are unique across the whole arc (include the revision, for example `a1r2b1`), and restore the previous statuses if the planner call or the validation fails. Accept: after a replan the old beats are present as superseded and every id is unique; after a failed replan the beats are exactly as before.

**A4 Retry shorter shows after every reply when the analyst is off.** `compute_shape_ok` returns False when there is no requested `end`, and `web/app.js` shows the button when `shape_ok === false`. With the analyst off (the default) there is no brief, so the button never goes away. Fix: with no requested end there is nothing to check, so return True; keep the overlong check as it is. Update D-26, which states the opposite. Accept: no button after a normal reply without a brief; the button still appears when the reply is overlong, or when `end=question` and the reply does not close on a question.

**A5 `fill_empty_pressures` logs the wrong unit.** It writes `at_msg` as clock minutes, but `truncate_arc_log` compares `at_msg` with message indexes, so undo cannot remove the entry correctly. Fix: pass `msg_idx` from the record pass and log that. Accept: undoing the turn removes the `pressure_revision` entry.

**A6 Three F4 gaps.** (a) When validation fails, call the planner once more, then keep the previous arc and log it (initial plan, replan and next act). (b) Validate the beats from `plan_next_act_beats` (three to five, hooks present, ids unique) and fall back to the single beat when they fail. (c) Clamp `effective_deadline_minutes` to the original deadline times the difficulty stretch (`DEADLINE_MAX_STRETCH`) in `normalize_arc`, so an old or hand-edited arc cannot exceed it. Accept: a test for each.

## 4. WP-B: Finish WP7 (canon naming)

**B1 Merge keeps the merged name.** When `merge` removes the source entry, its own name (lowercased) must become an alias of the target, together with its aliases, still capped at six and never equal to the target's name. Today only the aliases move, so the merged-away name is lost and the scribe recreates it on the next pass. Accept: merging `the-smith` into `bran` leaves `bran` with the alias `the smith`; pinned entries on either side are still skipped.

**B2 Aliases in lookups.** `analyst._ledger_entry` (the `keep_true` references) and `_mentioned` (pinned entries named in recent play) match id and name only. Use `ledger.resolve_ledger_ref`, which also matches aliases, and let `_mentioned` test aliases too. Accept: a pinned entry named only by an alias in recent play is kept in `keep_true`.

**B3 Ledger prompt.** `LEDGER_PROMPT` in `dm.py` must tell the scribe: when the play uses a different name for an existing entry, add it to `aliases` instead of creating a new entry; report duplicates already in the ledger with `"merge": [{"from": id, "into": id}]`; never alter pinned entries; at most six aliases per entry. Accept: the prompt text contains these rules, and `apply_ledger_ops` accepts the `merge` key.

**B4 Audit (G3).** Add an ALIAS COLLISIONS section to the audit prompt: entries that probably describe the same thing, with a suggested merge, and any name or alias used by two entries. Accept: the audit prompt sent to a fake `chat_fn` contains the section.

## 5. WP-C: Prompt rewrites

Rewrite in the prompt format described above, and keep each under about 400 words:

- **Analyst (`ANALYST_SYSTEM`):** inputs in authority order, the pin marker, mandatory moves and `deferred`, `keep_true` as ledger ids (aliases allowed), the ask rule, the escalation levels.
- **Record (`RECORD_PROMPT` and its system message):** limits for `elapsed_minutes`, `on_arc`, `matched_beats`, `objective`, `open_question` and `progress`.
- **Ledger (`LEDGER_PROMPT`):** with B3.
- **Planner (`PLANNER_SYSTEM`, replan and next-act prompts):** id rules (unique across the arc), the F4 limits, empty beats for acts 2 and 3, and the spoiler-safe hook rule.
- **Test:** key strings and numbers appear exactly once in the assembled narrator prompt (the word target is tested already; add the field definitions and the shape rule).

## 6. WP-D: Docs and housekeeping

- **decisions.md:** commit the reconciliation table (every requirement marked Covered, Partial or Open). Rewrite D-26 as shipped, with the A4 change. Add records for WP6 (beat states, drift and replan, act cap, planner validation limits, superseded beats kept), for the A2 reset choice and for WP7 (aliases, merge rule). Supersede earlier records, never edit them.
- **README.md:** add `drift_turns` and `act_max_turns` to the settings table, and WP6 and WP7 to the features. Add `steering.py` to the `ruff check` list in the README and in `.github/workflows/ci.yml`.
- **Cleanup:** delete the stray empty file `.gitkeep-wp1-tmp` and the unused `compute_defer_counts` in `dm.py` (the live function is `_defer_counts`).
- **Playtest script** (`docs/playtest.md`): five turns with steering on, checking the Now panel against the closing line; one `//` note; one undo; one retry; one retry shorter. With the arc on, add a scripted off-arc run that triggers exactly one replan.

## 7. Definition of done

- A1 to A6 and B1 to B4 merged, each with a test using a fake `chat_fn`. The existing tests were not reviewed here: run them and update any that encode the old behavior (A4 and the validation rules).
- The WP-C prompts rewritten, with the exactly-once prompt test.
- No arc content in any player-facing output: extend the sentinel leak test to superseded beats, `on_arc`, the drift counter and replan log entries.
- `decisions.md`, README, CI and the playtest script updated; the full suite and `ruff` green.
