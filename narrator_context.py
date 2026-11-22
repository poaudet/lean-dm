"""Assemble the narrator prompt from campaign state and the current action."""

import json
import pathlib

from config import cfg
from style_guard import compute_style_guard


def merge_same_role(msgs):
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    return out

def assemble_context(history, brief=None, *, folded_count, premise, summary, canon, steering=""):
    """Assemble a prompt using a snapshot of campaign state."""
    parts = [cfg.system_prompt]
    if premise:
        parts.append("PREMISE (fixed canon):\n" + premise)
    if cfg.style_enabled:
        style_parts = []
        preset = cfg.style_preset
        if preset and preset != "none":
            preset_file = pathlib.Path(__file__).parent / "styles" / f"{preset}.md"
            if preset_file.exists():
                style_parts.append(preset_file.read_text(encoding="utf-8").strip())
        if cfg.style_words_min > 0 or cfg.style_words_max > 0:
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
    tail = history[min(folded_count, len(history) - 1):]
    if tail and tail[0]["role"] == "assistant":         # only if history parity was ever disturbed
        tail = tail[1:]
    msgs = merge_same_role(tail)
    has_player_action = bool(msgs and msgs[-1]["role"] == "user")
    guard = compute_style_guard(history) if cfg.style_guard and has_player_action else []
    has_extra_instructions = bool(guard or brief or steering)
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
    if guard:
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nAVOID REPEATING:\n" + "\n".join(f"- {g}" for g in guard)}
    if brief and has_player_action:
        brief_text = json.dumps(brief, ensure_ascii=False, indent=1)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\n---\nDIRECTOR'S BRIEF (never mention it to the player):\n" + brief_text}
    if has_player_action and has_extra_instructions:
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] +
                    "\n\nNow narrate the outcome of the player's action above."}
    return [{"role": "system", "content": "\n\n".join(parts)}] + msgs
