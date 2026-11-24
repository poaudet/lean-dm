# Invariants

These hold for every change in this repository. Copied from section 4 of the
steering-requirements handoff (D-20).

- **I3:** volatile additions (ledger, guard, brief, steering, notes) go only in the
  last user message. Everything before it stays append-only so prefix caching works.
- **I6:** fallback never blocks play. Any new LLM call that fails degrades to
  code-built defaults.
- **I8:** the hidden arc never appears in history, premise, summary, ledger, audit,
  steering.json, any API or UI response, or default logs. Beat text, beat ids,
  pressure ids and antagonist data stay out of player-facing surfaces. Hooks are
  written to be spoiler-safe.
- **D-09:** difficulty bending is runtime state, not a plan revision.
- **D-10:** the UI shows only the non-spoiler arc chip and the Now panel
  (player-facing fields only).

Also:

- Files are the source of truth. State pointers are written last. All model output
  is clamped and validated in code.
- /undo and /retry must roll back every piece of state.
- Pinned ledger entries are never modified by automatic passes.
- Tests never call a real model: use a fake chat_fn.
