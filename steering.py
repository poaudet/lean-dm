"""steering.py — player-facing steering state (objective, open question, live lead).

Doctrine:
- Steering remains a compact, separate summary; the complete arc is inspectable through
  the explicit arc-debug state payload. No secrecy guarantee is built around arc data.
- A2: the record pass is the single writer; it runs after the reply is committed.
- A4: a failed update keeps the previous state, bumps the stall counter and marks the
  source as fallback. It never blocks the turn.
"""
from __future__ import annotations

import json
import logging

from storage import atomic_write

logger = logging.getLogger(__name__)

MAX_TEXT = 160
MAX_HOOK = 200
SOURCES = ("record", "fallback")


def steering_path(root):
    return root / "steering.json"


def steering_snap_dir(root):
    d = root / "steering_snap"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_steering():
    return {
        "objective": "",
        "open_question": "",
        "active_hook": None,
        "turns_since_progress": 0,
        "updated_msg": 0,
        "source": "fallback",
    }


def _int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _counter(state):
    return _int((state or {}).get("turns_since_progress", 0))


def load_steering(root):
    p = steering_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def save_steering(root, state):
    atomic_write(steering_path(root), json.dumps(state, indent=2, ensure_ascii=False))


def snapshot_steering(root, msg_idx):
    """Record the pre-turn state. A missing state is stored as null so undo can clear it."""
    state = load_steering(root)
    payload = json.dumps(state, indent=2, ensure_ascii=False) if state else "null"
    atomic_write(steering_snap_dir(root) / f"{_int(msg_idx)}.json", payload)


def restore_steering_snapshot(root, msg_idx):
    """Restore steering state from the snapshot taken before the given msg_idx."""
    snap = steering_snap_dir(root) / f"{_int(msg_idx)}.json"
    if not snap.exists():
        logger.warning("steering snapshot %s is missing; steering was not rewound", snap.name)
        return False
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        logger.warning("steering snapshot %s is unreadable: %s", snap.name, e)
        return False
    if data is None:
        steering_path(root).unlink(missing_ok=True)     # no state existed before this turn
        return True
    if not isinstance(data, dict):
        logger.warning("steering snapshot %s is not an object; steering was not rewound", snap.name)
        return False
    save_steering(root, data)
    return True


def prune_steering_snapshots(root, below):
    """Delete snapshots for msg indexes < below (undo can never reach them)."""
    d = root / "steering_snap"
    if not d.is_dir():
        return
    for f in d.glob("*.json"):
        if f.stem.isdigit() and int(f.stem) < below:
            f.unlink(missing_ok=True)


def clamp_steering(raw, previous=None, msg_idx=0, source="record"):
    """Validate a record-pass steering update. Junk falls back to the previous state."""
    prev = previous if isinstance(previous, dict) else default_steering()
    data = raw if isinstance(raw, dict) else {}
    objective = _text(data.get("objective"), MAX_TEXT) or _text(prev.get("objective"), MAX_TEXT)
    open_question = (
        _text(data.get("open_question"), MAX_TEXT)
        if "open_question" in data else _text(prev.get("open_question"), MAX_TEXT)
    )
    if "active_hook" in data:
        active_hook = _text(data.get("active_hook"), MAX_HOOK) or None
    else:
        active_hook = prev.get("active_hook") if isinstance(prev.get("active_hook"), str) else None
    progress = data.get("progress") is True
    if not progress and objective and objective != _text(prev.get("objective"), MAX_TEXT):
        progress = True                     # A2: a changed objective counts as progress
    return {
        "objective": objective,
        "open_question": open_question,
        "active_hook": active_hook,
        "turns_since_progress": 0 if progress else _counter(prev) + 1,
        "updated_msg": _int(msg_idx),
        "source": source if source in SOURCES else "record",
    }


def stalled_steering(previous, msg_idx):
    """A4: a failed update keeps the previous state, bumps the counter, marks the source."""
    prev = previous if isinstance(previous, dict) else default_steering()
    state = dict(prev)
    state["turns_since_progress"] = _counter(prev) + 1
    state["updated_msg"] = _int(msg_idx)
    state["source"] = "fallback"
    return state


def render_steering(state):
    """Render the compact steering summary for narrator and analyst prompts."""
    if not isinstance(state, dict):
        return ""
    lines = []
    objective = _text(state.get("objective"), MAX_TEXT)
    question = _text(state.get("open_question"), MAX_TEXT)
    hook = _text(state.get("active_hook"), MAX_HOOK)
    if objective:
        lines.append(f"Objective: {objective}")
    if question:
        lines.append(f"Open question: {question}")
    if hook:
        lines.append(f"Live lead: {hook}")
    return "\n".join(lines)


def player_fields(state):
    """The player-facing subset exposed by game_state() and the Now panel."""
    state = state if isinstance(state, dict) else {}
    return {
        "objective": _text(state.get("objective"), MAX_TEXT),
        "open_question": _text(state.get("open_question"), MAX_TEXT),
        "active_hook": _text(state.get("active_hook"), MAX_HOOK) or None,
        "turns_since_progress": _counter(state),
    }
