"""Assemble the narrator prompt from campaign state and the current action."""

import json
import pathlib

from config import cfg
from style_guard import compute_style_guard

# E3: used when no brief supplies a target.
DEFAULT_WORDS = 140

# E4: the retry-shorter action multiplies the budget by this.
SHORTER_RATIO = 0.7


def merge_same_role(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    return out


def word_target(brief=None):
    """E3: the single word target for this reply.

    brief.narration.words wins. With no brief, the configured style maximum applies,
    then DEFAULT_WORDS.
    """
    if isinstance(brief, dict):
        narr = brief.get("narration")
        if isinstance(narr, dict):
            try:
                return max(50, min(500, int(narr.get("words"))))
            except (TypeError, ValueError):
                pass
    return cfg.style_words_max if cfg.style_words_max > 0 else DEFAULT_WORDS


def shorter_words(words):
    """E4: the reduced budget used by the retry-shorter action."""
    try:
        n = int(float(words))
    except (TypeError, ValueError):
        n = DEFAULT_WORDS
    return max(50, int(n * SHORTER_RATIO))


def brief_fields(brief):
    """E2: define each brief field for the narrator, with this turn's values."""
    narr = brief.get("narration") if isinstance(brief.get("narration"), dict) else {}
    end = str(narr.get("end", "decision")).strip().lower()
    try:
        push = max(0.0, min(1.0, float(narr.get("push", 0.6))))
    except (TypeError, ValueError):
        push = 0.6
    clarify = brief.get("clarify")
    intent = str(brief.get("intent", "act")).strip().lower()

    end_meanings = {
        "decision": "the closing line states the choice facing the player",
        "question": "the closing line is a direct question from an NPC or the world",
        "beat": "the closing line is an event, with the decision implied",
    }
    lines = [
        f"- end={end}: {end_meanings.get(end, end_meanings['decision'])}.",
        f"- push={round(push, 2)}: at most {round((1 - push) * 100)} percent of sentences "
        "may be pure atmosphere.",
    ]
    if clarify:
        lines.append(
            f"- clarify: ask this one short in-character question and advance nothing: "
            f"\"{clarify}\"")
    if intent == "ask":
        lines.append(
            "- intent=ask: answer directly in at most 80 words. Add no new events.")
    return "\n".join(lines)


def render_notes(notes, limit=None):
    """D3: the last K OOC answers, as things the player was told out of character."""
    if not isinstance(notes, list) or not notes:
        return ""
    if limit is None:
        limit = cfg.notes_in_context
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 5
    if not limit:
        return ""
    lines = []
    for n in notes[-limit:]:
        if not isinstance(n, dict):
            continue
        a = str(n.get("a", "")).strip()
        if not a:
            continue
        lines.append(f"- You told the player (out of character): {a}")
    return "\n".join(lines)


def assemble_context(history, brief=None, *, folded_count, premise, summary, canon, steering="",
                     notes=""):
    """Assemble a prompt using a snapshot of campaign state."""
    parts = [cfg.system_prompt]
    if premise:
        parts.append("PREMISE (fixed canon):\n" + premise)
    target = word_target(brief)
    if cfg.style_enabled:
        style_parts = []
        preset = cfg.style_preset
        if preset and preset != "none":
            preset_file = pathlib.Path(__file__).parent / "styles" / f"{preset}.md"
            if preset_file.exists():
                style_parts.append(preset_file.read_text(encoding="utf-8").strip())
        # E3: the brief owns the word target, so the style block drops its own line
        # rather than stating a second number.
        if not brief and (cfg.style_words_min > 0 or cfg.style_words_max > 0):
            if cfg.style_words_min > 0 and cfg.style_words_max > 0:
                style_parts.append(f"Target length: {cfg.style_words_min}–{cfg.style_words_max} words per reply.")
            elif cfg.style_words_max > 0:
                style_parts.append(f"Target length: at most {cfg.style_words_max} words per reply.")
            else:
                style_parts.append(f"Target length: at least {cfg.style_words_min} words per reply.")
        if cfg.style_push > 0:
            style_parts.append(f"Style push: {cfg.style_push}/100.")
        if cfg.style_notes and cfg.style_notes.strip():
            style_parts.append("Style notes:\n" + cfg.style_notes.strip())
        style_parts.append(
            "Rules: Every reply must end on a concrete new development or decision point, "
            "avoid recapping the player's action, and keep atmosphere to the minority of sentences."
        )
        parts.append("STYLE:\n" + "\n\n".join(style_parts))
    if folded_count and summary:
        parts.append("STORY SO FAR (older play, already summarized; everything after it is verbatim below):\n" + summary)
    # E3: exactly one word target in the assembled prompt.
    parts.append(f"Word target: {target} words for this reply.")
    tail = history[min(folded_count, len(history) - 1):]
    if tail and tail[0]["role"] == "assistant":         # only if history parity was ever disturbed
        tail = tail[1:]
    msgs = merge_same_role(tail)
    has_player_action = bool(msgs and msgs[-1]["role"] == "user")
    guard = compute_style_guard(history) if cfg.style_guard and has_player_action else []
    has_extra_instructions = bool(guard or brief or steering or notes)
    if canon and has_player_action:   # volatile part goes LAST to keep the prefix cacheable
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nCANON LEDGER — authoritative current state. If it conflicts with your recollection "
                    "of earlier turns, the ledger wins. Never mention it to the player.\n" + canon}
        suffix = ("\n--- end of ledger" if has_extra_instructions else
                  "\n\n--- end of ledger: now narrate the outcome of the player's action above.")
        msgs[-1]["content"] += suffix
    if steering and has_player_action:   # A5: after the ledger, before the brief (I3)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nCURRENT STEERING — where the story is pushing. Player-facing; "
                    "never mention this block.\n" + steering}
    if notes and has_player_action:      # D3: OOC answers, also volatile (I3)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nALREADY TOLD OUT OF CHARACTER — treat as true, never mention it:\n" + notes}
    if guard:
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nAVOID REPEATING:\n" + "\n".join(f"- {g}" for g in guard)}
    if brief and has_player_action:
        brief_text = json.dumps(brief, ensure_ascii=False, indent=1)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nDIRECTOR'S BRIEF (never mention it to the player):\n" + brief_text +
                    "\n\nWhat the brief fields mean for this reply:\n" + brief_fields(brief)}
    if has_player_action and has_extra_instructions:
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\nNow narrate the outcome of the player's action above."}
    return [{"role": "system", "content": "\n\n".join(parts)}] + msgs