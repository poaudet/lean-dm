# Lean-DM: Remaining Work

This file tracks follow-up work left after the steering/arc review fixes in this branch. The former code findings (record validation, clarification behavior, pressure scheduling, analyst prompt limits, planner constraints, flag combinations, arc debug visibility/export policy, active-hook contract, and OOC prompt examples) have been addressed with regression coverage. See `decisions.md` for the associated product decisions.

## Release verification still outstanding

These are validation gaps, not known implementation failures. Record the environment, command/scenario, and result when each is completed.

### 1. Run browser tests in a supported browser environment

The browser tests were skipped locally because Chrome/Chromium was unavailable.

**Remaining work:** Run the browser/UI test suite in CI or another environment with a supported Chrome/Chromium installation. Investigate and fix any failures introduced by the Arc debug panel or Now-panel changes.

**Acceptance:** Browser tests execute (not skip) and pass; report the browser/version and test result.

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

- Keep follow-up changes focused and add regression tests for behavior changes.
- Preserve `/undo` and `/retry` rollback semantics, append-only prompt-prefix behavior, fallback-on-LLM-failure behavior, and the documented arc/analyst configuration contracts.
- Do not add dependencies or change dependency versions without approval.
- Update `decisions.md` when a follow-up requires a product or contract choice.
- Report local test results, CI results, browser/manual verification, and deployment checks separately; one does not substitute for another.
