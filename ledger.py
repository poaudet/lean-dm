"""Canon ledger update and rendering rules."""

import re

from config import cfg

KINDS = ("player", "person", "place", "item", "promise", "clock", "rule", "thread")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48]

def _lst(x):
    return x if isinstance(x, list) else []

def apply_ledger_ops(ledger, ops):
    by = {e["id"]: e for e in ledger}
    for u in _lst(ops.get("upsert")):
        if not isinstance(u, dict):
            continue
        eid = slug(u.get("id") or u.get("name") or "")
        if not eid or not str(u.get("state", "")).strip():
            continue
        e = by.get(eid) or {"id": eid}
        if e.get("pinned"):                             # D-12: skip pinned entries
            continue
        kind = str(u.get("kind", "")).strip().lower()
        e["kind"] = kind if kind in KINDS else e.get("kind", "thread")
        e["name"] = str(u.get("name") or e.get("name") or eid).strip()
        e["state"] = str(u["state"]).strip()
        src = str(u.get("src", "")).strip().lower()
        e["src"] = src if src in ("player", "dm", "note") else e.get("src", "dm")
        try:
            e["msg"] = int(u.get("msg"))
        except (TypeError, ValueError):
            e.setdefault("msg", None)
        e["status"] = "active"                          # an upsert reopens a closed entry
        by[eid] = e
    for i in _lst(ops.get("close")):
        s_id = slug(i)
        if s_id in by and not by[s_id].get("pinned"):
            by[s_id]["status"] = "closed"
    for i in _lst(ops.get("delete")):
        s_id = slug(i)
        if s_id in by and not by[s_id].get("pinned"):
            by.pop(s_id, None)
    out = list(by.values())
    if len(out) > cfg.ledger_max:                       # over the cap: oldest CLOSED entries go first
        closed = sorted((e for e in out if e.get("status") == "closed" and not e.get("pinned")), key=lambda e: e.get("msg") or 0)
        drop = {e["id"] for e in closed[: len(out) - cfg.ledger_max]}
        out = [e for e in out if e["id"] not in drop]
    return out

def render_ledger(ledger):
    if not ledger:
        return ""
    order = {k: i for i, k in enumerate(KINDS)}
    act = sorted((e for e in ledger if e.get("status") != "closed"),
                 key=lambda e: (order.get(e.get("kind"), 99), str(e.get("name", ""))))
    closed = sorted((e for e in ledger if e.get("status") == "closed"),
                    key=lambda e: e.get("msg") or 0)[-cfg.ledger_closed_max:] if cfg.ledger_closed_max else []
    lines, cur = [], None
    for e in act:
        if e.get("kind") != cur:
            cur = e.get("kind")
            lines.append(f"[{str(cur).upper()}]")
        lines.append(f"- {e.get('name')}: {e.get('state')}")
    if closed:
        lines.append("[PAST / RESOLVED — do not revive]")
        lines += [f"- {e.get('name')}: {e.get('state')}" for e in closed]
    return "\n".join(lines)

