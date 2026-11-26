# Lean-DM: Remaining Work

This file tracks follow-up work left after the steering/arc review fixes in this branch. The former code findings (record validation, clarification behavior, pressure scheduling, analyst prompt limits, planner constraints, flag combinations, arc debug visibility/export policy, active-hook contract, and OOC prompt examples) have been addressed with regression coverage. See `decisions.md` for the associated product decisions.

## Release verification still outstanding

### 1. Arc visibility and debug access (resolved by D-33)

The product decision is that arc data is not sensitive: players may inspect it, and the collapsed Arc debug panel plus `/api/state` payload are intentional. Do not add sanitization or owner-only access controls to hide arc data. Steering remains a separate compact summary for prompt/UI clarity, not a secrecy boundary.

**Acceptance:** `arc_debug` exposes the full active arc when enabled, the UI renders it in a collapsed spoiler-labelled panel, and it is absent when arc management is disabled.

### 2. Execute the documented manual playtest

The documented playtest has not yet been run.

**Relevant guide:** `docs/playtest.md`.

**Remaining work:** Execute the full playtest, including the scripted off-arc/replan scenario. Pay particular attention to clarification turns, pressure deadlines, Arc debug visibility, undo, and retry behavior.

**Acceptance:** Record the date, environment, scenarios completed, and any defects or deviations. All required scenarios pass or have tracked follow-up issues.

### 3. Establish a coverage baseline

No line or branch coverage percentage is currently available; `coverage`/`pytest-cov` was not installed or configured in the reviewed environment.

**Remaining work:** Decide whether coverage tooling should be added. Do not add or change dependencies without approval. If approved and configured, establish a baseline and identify untested branches in steering, arc recording/scheduling, and analyst brief/prompt handling.

**Acceptance:** Document the coverage command and baseline, or record an explicit decision not to add coverage tooling.

### 4. Expand strict typing verification

Strict mypy was run only for `campaign.py`, `storage.py`, and `style_guard.py`.

**Remaining work:** Agree on the intended typing scope, then run strict mypy across the remaining application modules. Fix newly exposed issues without broad unrelated cleanup.

**Acceptance:** Record the exact command and exit status for the agreed module set, and track any intentionally excluded modules with reasons.

### 5. Perform a live service/deployment smoke check

The Docker image build passed locally/through prior CI, but no live container/service healthcheck was performed.

**Remaining work:** Start the built image in a representative target environment and verify startup, configured health endpoint, static UI delivery, and one basic state/API request. Do not expose credentials or production data in logs or reports.

**Acceptance:** Record image identifier, environment, healthcheck result, and smoke-test result. Any deployment-only failures are tracked separately from local test results.

### 6. Obtain CI results for the implementation branch

Draft PR creation was previously rejected by the GitHub integration with HTTP 403 (`Resource not accessible by integration`), so no CI run was available for the implementation commit.

**Remaining work:** Open a draft PR from the implementation branch against `handoff/steering-requirements-wp1` when repository permissions allow, then inspect all check runs and status contexts. Treat queued/in-progress checks as pending, not passing.

**Acceptance:** Record the PR URL and quote the final CI check conclusions. Resolve any failing checks before marking the PR ready.

## Working rules

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

### 9. Arc export policy (resolved by D-33)

`/api/export/campaign.zip` intentionally omits arc internals by default and includes them with `?include_arc=1`. Arc data is not sensitive, so no additional owner-only authorization is required; the app's existing global authentication configuration remains in force.

**Acceptance:** Keep regression coverage for both export modes and document the opt-in flag in the README.

## Low priority / contract clarification

### 10. Populate the optional active hook (resolved)

The record output schema now requests `active_hook` as an optional current player-perceivable lead, bounded to 200 characters. `clamp_steering()` persists it when supplied and clears it on explicit null/empty.

**Acceptance:** Record prompt and steering clamp tests cover the field.

### 11. OOC prompt examples (resolved)

`OOC_SYSTEM` now includes one concise good/bad example and follows the shared prompt section contract.

**Acceptance:** Prompt-structure test checks the `EXAMPLES` section.

## Repository health and verification still outstanding

These are validation gaps, not claims that the implementation is broken:

- Browser tests are skipped because Chrome/Chromium is not installed in the reviewed environment. Run them in an environment with a supported browser before release.
- No line or branch coverage percentage is currently available: `coverage`/`pytest-cov` is not installed or configured. Decide whether to add coverage tooling, then establish a baseline and focus on the steering/arc/analyst paths above.
- The documented manual playtest has not been run. Execute `docs/playtest.md`, including the scripted off-arc/replan scenario, and record the result.
- Strict mypy was run only for `campaign.py`, `storage.py`, and `style_guard.py`; broader typing coverage remains unverified.
- CI's Docker image build passed, but no live container/service healthcheck was performed. Run a deployment-level smoke/healthcheck in the target environment.

## Working rules for remaining items

- Keep changes focused; add regression tests for each behavior before or with its fix.
- Preserve the explicit arc-debug/export contract, append-only prompt-prefix behavior, fallback-on-LLM-failure behavior, and full `/undo` and `/retry` rollback semantics.
- Do not add dependencies or change dependency versions without approval.
- Update `decisions.md` when a finding requires a product/contract choice, especially clarify semantics and arc visibility/export behavior.
- Run the relevant tests, the full test suite, Ruff, and the applicable type checks. Report browser/manual/deployment checks separately when the environment cannot run them.
