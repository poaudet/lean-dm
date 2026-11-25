# Lean-DM: Remaining Work

This file tracks only the outstanding issues identified in the independent review of `handoff/steering-requirements-wp1` at `db39edc`. The steering handoff packages and prompt/documentation work already merged are not repeated here. Treat the items below as review findings to verify and resolve; do not assume a proposed fix is correct until its behavior is covered by tests.

## High priority

### 1. Prevent hidden arc information from entering player-visible steering (I8)

**Relevant code:** `arc.py` record context around lines 918–925; `steering.py` `clamp_steering()`; `dm.py` state/API responses and the Now panel.

The record model receives private arc details, including beat text and IDs, pressure information, and antagonist details. `clamp_steering()` currently normalizes and truncates strings but does not establish that `objective`, `open_question`, or `active_hook` are safe to show to the player. A model response can therefore copy hidden arc content into persisted steering and the Now panel.

**Remaining work:** Add deterministic validation/sanitization at the boundary before steering is persisted. Do not rely on the model prompt to keep secrets. Review other paths where pressure/deadline notes enter narrator context for the same leak class.

**Acceptance:** Add sentinel tests that deliberately return hidden beat text/IDs, pressure IDs/details, and antagonist names in record output. Assert none reach `steering.json`, state/API responses, or player-facing UI data. Keep legitimate spoiler-safe steering intact.

## Medium priority

### 2. Allow the record pass to clear a resolved open question

**Relevant code:** `steering.py`, `clamp_steering()` (currently around line 119).

An explicit empty or null `open_question` currently falls back to the previous value, so a resolved question can remain visible indefinitely.

**Remaining work:** Distinguish an omitted field (preserve the previous value) from an explicitly supplied empty/null value (clear it), consistent with the record-output contract.

**Acceptance:** Test that omission preserves the previous question and explicit empty/null clears it.

### 3. Strictly validate record booleans and beat IDs

**Relevant code:** `arc.py` record normalization and `run_record_pass()` (around lines 978–1006 and 1035–1036); `analyst.py` brief clamping (around line 288); `steering.py` progress handling.

Truthiness conversions such as `bool("false")` treat invalid string values as true. In addition, arbitrary non-empty `beats_done` values can reset stalled-turn counters, and `matched_beats` is not consistently restricted to IDs in the active act.

**Remaining work:** Validate booleans as actual booleans (or an explicitly documented accepted representation); reject or safely default malformed values. Filter matched/completed beat IDs against the active act. Reset drift/stall counters only when a newly completed, valid beat is recorded.

**Acceptance:** Cover string booleans, malformed values, unknown IDs, IDs from another act, duplicate IDs, and a valid newly completed beat. Assert invalid output cannot trigger act transitions or reset drift counters.

### 4. Make `clarify` intent non-advancing

**Relevant code:** `analyst.py` `clamp_brief()` (around lines 283–294); `arc.py` `run_record_pass()` (around lines 904–911).

The `ask` intent suppresses moves, but `clarify` does not. A clarify brief can therefore retain due moves and the record pass can continue to advance the story/clock, despite the requirement that clarification asks one short in-character question and advances nothing.

**Remaining work:** Define and implement the interaction between `clarify`, mandatory due moves, and clock advancement. Preserve mandatory-move guarantees for ordinary turns, but make the clarify behavior explicit and consistent across analyst and record passes.

**Acceptance:** With due moves present, a clarify turn asks one short in-character question, plays/defer no story moves, and does not advance story time or arc progress. Add a separate test for the chosen behavior when mandatory pressure deadlines coincide with clarify.

### 5. Keep auto-filled pressure steps within the deadline

**Relevant code:** `arc.py`, `fill_empty_pressures()` (around lines 1240–1251).

The function checks whether a pressure is sufficiently far from its deadline before filling it, but generated steps can still be scheduled after the deadline. The current incremental offset can also push later generated steps farther out than intended.

**Remaining work:** Calculate future step times against the effective deadline and the required minimum spacing. Do not create unreachable steps beyond the deadline; define a safe fallback when there is insufficient room for another step.

**Acceptance:** Add tests near the deadline, including a case with a current clock of 160 and deadline of 300, and assert every generated step is reachable and respects spacing.

### 6. Enforce the analyst prompt cap across the complete assembled prompt

**Relevant code:** `analyst.py`, `_cap_blocks()` and prompt assembly (around lines 577–604).

The current cap applies to selected context blocks, but the final prompt also adds recent play, act goal, steering, hooks, allowed moves, style guard, and the player action. The assembled prompt can exceed `analyst_prompt_cap`.

**Remaining work:** Apply the configured cap to the complete assembled prompt. Trim lower-priority/older context first while preserving the current player action and required structural instructions. Keep truncation deterministic.

**Acceptance:** Test a deliberately oversized combination of recent play, hooks, moves, and player input. Assert the final assembled prompt stays within the configured cap and preserves the current action and required instructions.

### 7. Give the planner explicit numeric constraints

**Relevant code:** `arc.py`, `PLANNER_SYSTEM` and planner input assembly (around lines 453–467).

The planner is told to respect supplied limits, but the prompt does not clearly supply the numeric constraints enforced by validation, including the 30-minute step gap, 14,400-minute maximum, and difficulty stretch cap. The pressure-step schema also needs explicit integer/non-negative requirements.

**Remaining work:** Put the actual configured constants and field constraints into planner instructions/inputs, using shared constants rather than duplicating magic numbers. Cover initial planning, replanning, and next-act planning consistently.

**Acceptance:** Fake-chat prompt tests assert the effective numeric limits and schema constraints are present in each planner prompt. Validation remains authoritative if model output violates them.

### 8. Define behavior when the analyst is disabled but the arc is enabled

**Relevant code:** `dm.py`, `get_allowed_moves()` call path (around lines 837–856); configuration and UI settings.

Arc and analyst flags are independently configurable, but due arc moves are currently fetched only inside the analyst-enabled branch. With `arc_enabled=true` and `analyst_enabled=false`, scheduled beat/pressure moves may not be injected.

**Remaining work:** Choose and implement one explicit contract: either enforce the dependency in configuration/UI/docs, or build a code-generated brief that still injects mandatory arc moves when the analyst is disabled. Keep the two flags' documented semantics aligned with runtime behavior.

**Acceptance:** Test all four combinations of arc/analyst enabled and disabled, especially arc on + analyst off with due moves.

### 9. Resolve the hidden-arc export/API policy

**Relevant code:** `dm.py`, `/api/export/campaign.zip?include_arc=1` (around lines 1351–1364); README export documentation.

The export endpoint can include `arc.json`, arc logs, records, briefs, and snapshots. This is an apparent exception to I8's rule that hidden arc data must not appear in API responses. The README documents the feature, but the authorization/owner-only policy and its relationship to I8 need to be explicit.

**Remaining work:** Decide whether this is an approved owner-only backup exception or whether hidden arc data must be removed from the API export. Document the decision and enforce the appropriate authorization boundary; do not silently weaken I8.

**Acceptance:** Add endpoint tests for authorized and unauthorized callers and for both `include_arc` modes. Document the approved policy in `decisions.md` and the README.

## Low priority / contract clarification

### 10. Clarify how `active_hook` is populated

**Relevant code:** `steering.py`; `RECORD_PROMPT` in `arc.py`; steering state and UI fields.

`active_hook` exists in steering state and player-facing fields, but the record output schema does not request it and no deterministic derivation currently appears to populate it. It is therefore likely to remain null.

**Remaining work:** Decide whether `active_hook` is required. If required, define its source and populate it safely; otherwise remove/deprecate the unused field and update the contract/docs.

**Acceptance:** A test proves the selected contract: either a spoiler-safe active hook is populated when one exists, or the field is intentionally absent/deprecated.

### 11. Add examples to the OOC prompt if the shared prompt contract still requires them

**Relevant code:** OOC system prompt in `dm.py`.

The OOC prompt does not have an `EXAMPLES` section, unlike the shared prompt format described in the original handoff.

**Remaining work:** Confirm whether that format is mandatory for every prompt. If so, add one concise good/bad example without changing OOC routing semantics.

**Acceptance:** A prompt-structure test checks the agreed sections, including examples if the contract remains universal.

## Repository health and verification still outstanding

These are validation gaps, not claims that the implementation is broken:

- Browser tests are skipped because Chrome/Chromium is not installed in the reviewed environment. Run them in an environment with a supported browser before release.
- No line or branch coverage percentage is currently available: `coverage`/`pytest-cov` is not installed or configured. Decide whether to add coverage tooling, then establish a baseline and focus on the steering/arc/analyst paths above.
- The documented manual playtest has not been run. Execute `docs/playtest.md`, including the scripted off-arc/replan scenario, and record the result.
- Strict mypy was run only for `campaign.py`, `storage.py`, and `style_guard.py`; broader typing coverage remains unverified.
- CI's Docker image build passed, but no live container/service healthcheck was performed. Run a deployment-level smoke/healthcheck in the target environment.

## Working rules for remaining items

- Keep changes focused; add regression tests for each behavior before or with its fix.
- Preserve the hidden-arc boundary, append-only prompt-prefix behavior, fallback-on-LLM-failure behavior, and full `/undo` and `/retry` rollback semantics.
- Do not add dependencies or change dependency versions without approval.
- Update `decisions.md` when a finding requires a product/contract choice, especially clarify semantics and arc export authorization.
- Run the relevant tests, the full test suite, Ruff, and the applicable type checks. Report browser/manual/deployment checks separately when the environment cannot run them.
