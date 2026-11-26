# Architecture Decision Records

This file records design decisions for the lean-dm project.

Format per entry:

    ## D-NN  <Title>

    Decision: <the rule the codebase follows>

    Alternatives considered: <rejected options, so future changes know the "why-not">

All entries below are implemented unless superseded by a later decision. A
decision may only be changed by writing a new decision record that supersedes it.

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

This field set remains the baseline. It is extended, not replaced, by D-20,
which authorises the additions the steering handoff requires (`deferred`,
`keep_true` as `{id, why}`, and the record pass's `objective`,
`open_question` and `progress`).

Alternatives considered: Free-form text brief; Markdown brief; schema versioned independently.

---

## D-06  Notes

Decision: OOC notes bypass `history.jsonl` and are stored in `notes.jsonl`. Notes become canon only on explicit player request. Legacy notes already in history and the system-prompt line about them stay untouched.

D-27 records the `notes_routing` default flip to on (D2) and the recent-answers
context block (D3).

Alternatives considered: Keep notes in history with a tag; strip notes from context at build time; store notes in the ledger with `src="note"`.

---

## D-07  Precedence

Decision: Narration instruction precedence is per-turn brief > style block > system_prompt. Each style preset states explicitly that it overrides length guidance.

The clause stating that the default system_prompt keeps its "120-220 words"
line unchanged is superseded by D-22: `brief.narration.words` becomes the
single word target. WP4 (E3) removed the hard-coded range from DEFAULT_SYSTEM,
so the two numbers no longer coexist.

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

---

## D-18  Arc runtime and outcome records

Decision: Runtime scheduling state (`last_overt_turn`, `stalled_turns`), delivered plot move completion, deadline bending, and delayed/advanced/cancelled pressure effects are persisted in `arc.json`. Act outcomes are factual summaries of played exchanges and record-pass data, never the planner's planned beat text.

This supersedes D-09's statement that `arc.json` is never modified during play:
runtime fields and progress are persisted there without using difficulty bending
as a planner revision.

Alternatives considered: Keep scheduling state only in memory; replay the same due moves until manually retired; use planned beat text as if it were resolved play.

---

## D-19  Analyst fallback

Decision: On timeout or error, the narrator receives a safe default brief using the frozen D-05 schema. A classified fallback reason is stored separately in `briefs.jsonl` metadata, not in the narrator prompt. This supersedes D-14's statement that the narrator receives no brief.

Alternatives considered: Omit the brief entirely; include raw error text in the brief or narrator prompt.

---

## D-20  Steering requirements handoff

Decision: The "Lean-DM: Steering Requirements" handoff (November 2026) is adopted as the governing ADR for the steering work. It is implemented in packages WP1–WP7, one branch and one PR per package, in order. Each package must ship alone with the existing suite green. The Step 0 reconciliation table is committed before any package is coded, and every Partial or Open requirement is implemented; Covered requirements are left alone.

This supersedes D-05's freeze on the brief schema. The D-05 field set stays the baseline, but the handoff adds `deferred` (B4), changes `keep_true` to a list of `{id, why}` (C2), and adds `objective`, `open_question` and `progress` to the record pass (A2). Those additions are authorised by this record rather than by a separate one per field.

Alternatives considered: One ADR per requirement (seven-plus records for one coherent feature); treat the handoff as a design note and keep D-05 frozen (blocks every package).

---

## D-21  Steering state ownership

Decision: `steering.json` holds the player-facing objective, open question and live lead. The record pass is its single writer and runs after the reply is committed, so the state reflects what the player actually read. It runs whenever `steering_enabled` is on, even with `arc_enabled` off, and shares one model call with the arc when both are on. Snapshots live in `steering_snap/<msg>.json` and are restored by /undo and /retry. A failed update keeps the previous state, increments `turns_since_progress` and sets `source` to `fallback`.

The file never contains arc ids, beat text, pressure ids or antagonist data (I8).

Alternatives considered: Let the analyst write it (it runs before narration, so it would describe the turn the player has not read yet); store it in `state.json` (mixes engine pointers with player-facing text); no snapshots (undo would leave the Now panel describing a turn that no longer exists).

---

## D-22  Word budget single source

Decision: `brief.narration.words` is the only word target. The default system prompt no longer states a number; when no brief exists a configured default applies. This supersedes D-07's statement that the default system_prompt keeps its "120-220 words" line unchanged.

WP4 (E3) implements this: `DEFAULT_SYSTEM` states no count, `narrator_context.word_target()` resolves the target, and the assembled prompt states the target exactly once. With a brief that is the "Word target:" line; with no brief and style enforcement on it is the style block's "Target length:" line, and the standalone line is suppressed so the same number is never stated twice.

Alternatives considered: Keep the range in the system prompt and let the brief override it (two sources of truth, the conflict the handoff reports); drop the brief field and keep only the prompt (loses per-turn control).

---

## D-23  Beat hooks and stall escalation

Decision: Each beat carries a `hook` — at most 200 characters, a lead the player can perceive, never naming the antagonist or revealing the solution. `text` stays the private plan and is never shown. A beat without a hook never produces a move, so a planner that omits hooks degrades to pressure-only pacing rather than leaking plan text.

`allowed_moves()` returns the next pending beat's hook as a move when no pressure move is due, or when `stalled_turns` reaches the difficulty threshold (story 2, standard 3, hard 5). The two-move cap and deadline-first ordering are unchanged, so a hook never displaces a deadline.

While a hook goes unanswered its level rises: subtle, then overt after two stalled turns, then offer after four. **This escalation overrides the overt cooldown** (the handoff's B3 requires this and asks for it to be recorded here). The level is derived from `stalled_turns` rather than stored per beat, so it resets automatically when the beat is completed or superseded.

Playing a hook does not mark the beat done; only the record pass does (B6). For the same reason a played hook does not reset `stalled_turns` — only a completed beat does, otherwise showing a hook would drop the escalation back to `subtle` while the beat was still pending.

Alternatives considered: Store the escalation level on the beat (needs its own reset path and a new snapshot field); let the analyst choose `how` for beat moves (the scheduler owns pacing, per the existing clamp); expose beat text instead of a hook (violates I8).

---

## D-24  Mandatory moves and the deferred list

Decision: Every move `allowed_moves()` returns is mandatory. The analyst plays it in `plot_moves` or lists it in `deferred` as `{id, reason}`. `clamp_brief` enforces this in code: a due move in neither list is appended with its scheduled `how` and `note`, and a move deferred `MAX_DEFERS` (2) consecutive turns is forced back in regardless of what the model returned.

`make_fallback_brief` takes the allowed list and plays the due moves in code, so a timeout or 429 never drops them.

Alternatives considered: Trust the model to honour the instruction (the handoff reports that nothing currently requires it); drop deferred moves entirely (loses the analyst's ability to hold a move for a scene that is mid-beat); let the fallback brief stay empty (silently loses pacing on every provider hiccup).

---

## D-25  Narrator reply shape and field meanings

Decision: Every narrator reply has three parts: the consequence of the action in one to three sentences, at most one new fact or hook, and a closing line stating the live decision in plain words. This lives in `DEFAULT_SYSTEM` (E1), so it applies whether or not style enforcement is on.

The brief's fields are defined for the narrator in the prompt, not left implicit (E2). `narrator_context.brief_fields()` renders each field's meaning with this turn's values and is appended to the brief block, so it is present even with `style_enabled` off:

- `end=decision` — the closing line states the choice facing the player.
- `end=question` — the closing line is a direct question from an NPC or the world.
- `end=beat` — the closing line is an event, with the decision implied.
- `push` — at most `round((1 - push) * 100)` percent of sentences may be pure atmosphere.
- `clarify` — ask that one short in-character question and advance nothing.
- `intent=ask` — answer directly in at most 80 words, with no new events.

The "end on a decision" rule previously lived only in the `style_enabled` block, so it vanished when style was off. It is now unconditional.

Alternatives considered: Keep the field meanings in the analyst prompt only (the narrator never sees the analyst prompt); hard-code the meanings in the brief JSON (unreadable to the model); rely on the model inferring `end` from the value name (the handoff reports this is exactly what fails).

---

## D-26  Post-check instead of regeneration

Decision: Streaming makes silent regeneration impossible, so the narrator's reply is checked after the fact rather than rewritten. `dm.compute_shape_ok()` verifies the closing line matches the requested `end` (a question mark for `question`, a non-empty closing line otherwise). `overlong` is computed against the brief's word target times `overlong_ratio` (default 1.5), not against `style_words_max`, so it works with style enforcement off.

Both flags are stored in `state.json` and exposed by `game_state()`, and the UI offers a one-click retry-shorter that regenerates with the budget multiplied by `SHORTER_RATIO` (0.7). The reduced budget is recorded as `word_budget` so the check applies to the retried reply too.

**Status: the backend is shipped; the UI is not.** `dm.compute_shape_ok()` exists and is called after every reply, `overlong` is measured against the brief's budget times `overlong_ratio` (so it fires with `style_words_max = 0`), and `shape_ok`, `last_words` and `word_budget` are written to `state.json` and returned by `game_state()`. `dm._shorter_brief()` rebuilds the brief at `SHORTER_RATIO` and `POST /api/retry` accepts `{"shorter": true}`.

What is still missing is the button: `web/app.js` and `web/index.html` do not yet show a "retry shorter" control when `overlong` is true or `shape_ok` is false. That is FIX3, tracked separately. Until it lands the capability is reachable only by calling the endpoint directly.

Alternatives considered: Regenerate silently when the shape is wrong (impossible mid-stream, and doubles cost); block the reply until it passes (breaks streaming); only check when `style_enabled` (the handoff reports this is why the check never fired).

---

## D-27  OOC notes default on and recent answers in context

Decision: `notes_routing` defaults to `true` for every campaign (D2). The handoff reports that OOC answers were never visible to the narrator afterwards and that routing was off by default, so the player had to opt in to get an answer at all and the answer was then forgotten.

The last `notes_in_context` (campaign, default 5) OOC answers are rendered by `narrator_context.render_notes()` into the volatile last-message area as things the player was told out of character — treat as true, never mention (D3). Canon promotion is unchanged: a note only becomes canon on explicit player request (D-06).

The OOC prompt is grounded in the steering state (D4): `handle_ooc_note` accepts `steering_text` and renders it alongside the premise, summary and ledger, under a rule never to invent a fact and to offer a canon proposal when something is not established.

Alternatives considered: Keep routing opt-in (the handoff reports this is why the path was invisible); replay every OOC exchange every turn (unbounded prompt growth); promote OOC answers to canon automatically (D-06 forbids it).

---

## D-28  Fallback brief carries no steering key; only pressure moves start the cooldown

Decision: Two small corrections to the WP1–WP5 behaviour, recorded together because both are about state that was being written twice or written by the wrong actor.

**The fallback brief carries no `steering` key.** `make_fallback_brief` used to copy the previous steering text into `brief["steering"]`. The narrator already receives steering in its own block (`narrator_context.assemble_context` takes `steering=` and renders it under `CURRENT STEERING`), so the brief carried it a second time and in a shape no normal brief has. Nothing read the key. It is removed, and the fallback brief now matches `BRIEF_SCHEMA` exactly. C3 still holds: the previous target survives because this path never writes `steering.json` — only the record pass does (D-21). `steering_text` stays in the signature so call sites are unchanged, and is deliberately unused.

**Only pressure moves set `last_overt_turn`.** `apply_played_moves` used to set the overt cooldown whenever any played move had `how == "overt"`, including beat hooks. That contradicts D-23: hook escalation is derived from `stalled_turns` and deliberately overrides the cooldown, so a beat move must not start a cooldown that suppresses pressure moves. The check now ignores ids carrying the `beat.` prefix. Pressure steps and `<pid>.deadline` moves still set it.

Alternatives considered: Keep the steering key and have the narrator read it (two sources for the same text, and a schema that differs between normal and fallback briefs); drop `steering_text` from the signature (touches every call site in `dm.py` for no behavioural gain); suppress the cooldown for beat moves by clearing `last_overt_turn` instead of not setting it (would erase a cooldown a pressure move legitimately started).


## D-29  Retry-shorter visibility with no requested shape

Supersedes D-26 where it described the retry-shorter UI and the no-brief shape check.

Decision: The shipped UI offers retry-shorter when `overlong` is true or `shape_ok` is false. `compute_shape_ok()` returns true when the reply is non-empty and there is no requested `end`: without an analyst brief there is no shape contract to violate. When `end=question`, the closing line must end in `?`; `decision` and `beat` require a non-empty reply/closing line. The overlong check remains independent and uses the brief word target multiplied by `overlong_ratio` (default 1.5). Retry-shorter uses `SHORTER_RATIO` (0.7).

Alternatives considered: Treat a missing `end` as a failed shape (shows a retry control on every normal turn when the analyst is off); disable the overlong check when no shape is requested (unrelated checks should remain independent).

---

## D-30  Arc drift reset and retained replans

Decision: `runtime.off_arc_turns` counts consecutive off-arc turns and resets to zero on an on-arc turn. When the configured `drift_turns` threshold is reached, the record pass attempts one replan. The counter is reset before the updated arc is saved, whether planning succeeds or fails; a failed planner therefore retries only after another full threshold window. Successful replans append new beats and mark old pending beats `superseded`; no beat is deleted. New beat ids include act and revision and remain unique across all acts, including superseded beats. The initial planner is retried once on invalid output before failing without writing an invalid arc; a failed replan restores the prior beat statuses and logs `replan_failed`; next-act planning retries once and then supplies a single fallback beat. `act_max_turns=0` disables the forced act cap; otherwise the cap triggers a transition at the configured turn.

Alternatives considered: Reset only after a successful plan (retries every subsequent turn after failure); delete replaced beats (loses auditability and can collide with historical record ids); rewrite an existing beat id (breaks record references).

---

## D-31  Ledger aliases and merge semantics

Decision: A ledger entry may keep at most six aliases. References resolve against id, canonical name and aliases. When merging `from` into `into`, the target receives the source aliases and the source's former name as an alias, subject to the cap and excluding the target's own name. Pinned entries on either side are not changed. The audit reports likely duplicate entities and name/alias collisions with suggested merge ids.

Alternatives considered: Keep only the source aliases (the merged-away canonical name is then recreated on the next scribe pass); automatically merge from an audit suggestion (audit remains advisory and cannot mutate canon).

---

## D-32  Structured auxiliary prompt contracts

Decision: Analyst, record and planner prompts use explicit ROLE, INPUTS, TASK, OUTPUT, RULES, WHEN UNSURE and EXAMPLES sections. The analyst sees canon in authority order (ledger, premise, summary, recent play), with pinned entries marked; `keep_true` references ledger ids and aliases. The record schema bounds elapsed time to 1-120 minutes, steering strings to 160 characters, and restricts matched beats to supplied active-act ids. Planner prompts require unique arc-wide ids, 3-5 beats when an act is planned, empty beats for future acts, and spoiler-safe hooks of at most 200 characters. The ledger prompt instructs alias creation, duplicate merge reporting, six-alias cap and pinned-entry protection. The audit includes alias collisions.

Alternatives considered: Rely on model-inferred field semantics (ambiguous output); repeat field definitions across multiple prompts (drift); expose private beat text in hooks (violates I8).

---

## Reconciliation — Steering Requirements v2

| Requirement | Status | Implementation |
|---|---|---|
| A1–A6 (arc validation, drift reset, retained replan, no-brief shape, log units, planner fallback/clamps) | Covered | `arc.py`, `dm.py`, `decisions.md` |
| B1–B4 (merge aliases, alias lookup, ledger prompt, audit collisions) | Covered | `ledger.py`, `analyst.py`, `dm.py` |
| C1–C4 (analyst inputs, pin markers, ledger-id keep_true, bounded retry) | Covered | `analyst.py` |
| D1–D6 (OOC routing and note lifecycle) | Covered | `analyst.py`, `dm.py`, `narrator_context.py` |
| E1–E6 (narrator shape, field meanings, single word target, shorter retry) | Covered | `config.py`, `narrator_context.py`, `dm.py`, `web/` |
| F1–F5 (beat lifecycle, drift/replan, act cap, validation, spoiler-safe hooks) | Covered | `arc.py` |
| G1–G3 (aliases, merge behavior, audit collisions) | Covered | `ledger.py`, `dm.py` |
| Prompt rewrites (analyst, record, ledger, planner) | Covered | `analyst.py`, `arc.py`, `dm.py` |
| Docs, CI and playtest procedure | Covered | `README.md`, `.github/workflows/ci.yml`, `docs/playtest.md` |

---


## D-33  Arc state is inspectable for debugging

Supersedes D-10's prohibition on showing the arc in the UI and the secrecy clauses in D-21 and the original I8 implementation notes.

Decision: Arc data is not sensitive campaign state. When arc management is enabled, `/api/state` exposes the complete arc as `arc_debug`, and the UI renders it in a collapsed “Arc debug (spoilers)” panel. Players may inspect it; cheating is an accepted choice and visibility supports debugging. Campaign ZIP exports continue to omit arc internals by default and include them when `?include_arc=1` is requested. No additional owner-only authentication or sanitization is required beyond the app's existing global authentication configuration. Steering remains a separate compact summary, but it is not treated as a security boundary.

Alternatives considered: Hide arc data from all player-facing responses (rejected: prevents debugging); require a separate owner role for debug/export access (rejected: there is no owner/player role model and the arc is not sensitive).


## D-34  Clarification turns leave scheduled story moves untouched

Decision: When the analyst supplies a non-empty `clarify`, the narrator asks that one in-character question. The brief plays no plot moves, defers none, and cannot complete an act. The record pass does not run, the arc clock/progress and steering remain unchanged, and empty-pressure autofill is skipped. Due moves remain scheduled and are reconsidered on the next ordinary turn, including when a pressure deadline is already due. `intent=ask` retains its existing non-advancing behavior.

Alternatives considered: Play mandatory moves during a clarification (contradicts “advance nothing”); defer them (changes their deferral streak and can force play on a clarification); consume or cancel them (loses scheduled story state).

## D-35  Arc scheduling works without the analyst

Decision: `analyst_enabled` controls only the optional model call. When `arc_enabled` is true and the analyst is off, code builds a fallback brief containing all currently due moves (up to the existing brief cap), so mandatory arc moves still reach the narrator and record pass. Retry reconstructs the same code-generated brief from the pre-turn arc snapshot. With arc disabled, no arc brief is built.

Alternatives considered: Require analyst_enabled whenever arc_enabled is true (unnecessarily couples independent settings); silently omit due moves (breaks mandatory scheduling).

## D-36  Active hook is an optional record-owned steering field

Decision: `active_hook` is an optional player-perceivable current lead supplied by the record pass, limited to 200 characters. Omission preserves the previous value; explicit null/empty clears it. It is a compact steering summary, not a secrecy boundary; the full arc remains inspectable under D-33.

Alternatives considered: Derive it from the first pending beat in code (may surface a stale or contextually irrelevant lead); remove the field (loses the existing Now-panel contract).

## D-37  Arc inspection needs debug mode

Amends D-33. Reconciles D-33 with the handoff's I8 and D-10 intent: arc data is still not sensitive and nothing is sanitized, but it must not be one click from the play UI.

Decision: a new global setting `arc_debug` (default `false`) is the debug mode. Only when it is on, and `arc_enabled` is on, does `/api/state` return the full arc as `arc_debug`, which is what makes the collapsed "Arc debug (spoilers)" panel appear. Campaign ZIP exports keep omitting arc internals by default; `?include_arc=1` is now honoured only when debug mode is on and is silently ignored otherwise. With debug mode off, `/api/state` carries only the non-spoiler arc chip and the player-facing steering fields, which restores D-10 for normal play.

The setting is global-scoped on purpose: it lives in the global settings page, so on a non-loopback host it sits behind the existing global-settings lock and web_password, and a campaign file cannot switch it on. It is a visibility gate for the UI and API, not access control; anyone who can edit settings or read the campaign folder can still see the arc, which is accepted. `active_hook` and the steering fields stay player-facing and are unaffected.

Alternatives considered: a per-campaign flag (a shared or imported campaign file could turn spoilers on); a `?debug=1` query parameter or a toggle on the play screen (one click from the spoilers, the problem this fixes); an environment variable only (not discoverable); owner-only auth (no role model exists, and the arc is not sensitive).
