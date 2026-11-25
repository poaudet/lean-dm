# Steering and arc playtest

Use a disposable campaign with `steering_enabled=true`. For the arc scenario also set
`arc_enabled=true`, `drift_turns=3`, and `act_max_turns=0`. Keep the Now panel visible.

## Steering run (five exchanges)

1. Ask the character to investigate a concrete lead. After the reply, compare the Now
   panel's objective and open question with the closing line: the objective should state
   the current goal and the question should be the decision the closing line leaves open.
2. Follow the lead without resolving the question. Confirm the panel updates only when
   the record pass has committed.
3. Send `// remind me what the guard said about the key`. Confirm the response is OOC,
   the note is not added to in-character history, and the latest OOC answer is available
   to the next narrator turn.
4. Take an in-character action, then use `/undo`. Confirm the transcript and Now panel
   return to their pre-turn state.
5. Use `/retry`, then use the UI's retry-shorter action. Confirm each replaces the same
   last reply and the shorter retry uses a reduced word budget.

## Arc drift run

1. Start a fresh disposable campaign with the arc enabled and `drift_turns=3`.
2. Complete three consecutive exchanges that clearly pursue an unrelated side activity.
   The fake/controlled record response for each must set `on_arc=false` and
   `matched_beats=[]`.
3. Configure the planner stub to return a valid 3-5 beat replan on the third exchange.
4. Inspect `arc.json` and `arc_log.jsonl`: exactly one `replan` entry should exist for
   that threshold crossing, `off_arc_turns` should be zero, prior pending beats should
   remain as `superseded`, and new beat ids should be unique across the full arc.
5. Play one further off-arc exchange. Confirm no second replan occurs until three new
   consecutive off-arc turns have accumulated.

Do not copy `arc.json`, planner output, beat text, pressure ids or antagonist details
into player-facing transcripts or screenshots.
