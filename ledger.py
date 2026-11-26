"""Canon ledger update and rendering rules."""

import re

from config import cfg

KINDS = ("player", "person", "place", "item", "promise", "clock", "rule", "thread")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48]

def _lst(x):
    return x if isinstance(x, list) else []


def _normalize_alias(alias):
    """Lowercase-normalize an alias."""
    return re.sub(r"\s+", " ", str(alias).strip().lower())


def _merge_aliases(existing, new_aliases, max_aliases=6):
    """Merge new aliases into an entry's alias list, capped at max_aliases.

    Aliases are lowercase-normalized, deduplicated, and must not equal the
    entry's own name.
    """
    name = str(existing.get("name", "")).strip().lower()
    aliases = set(existing.get("aliases", []))
    for a in new_aliases:
        a = _normalize_alias(a)
        if not a or a == name:
            continue
        aliases.add(a)
    # Cap at max_aliases; keep deterministic order (sorted)
    existing["aliases"] = sorted(aliases)[:max_aliases]
    return existing


def apply_ledger_ops(ledger, ops):
    by = {e["id"]: e for e in ledger}
    for u in _lst(ops.get("upsert")):
        if not isinstance(u, dict):
            continue
        eid = slug(u.get("id") or u.get("name") or "")
        if not eid or not str(u.get("state", "")).strip():
            continue

        # G1: If the upsert carries an alias that matches an existing entry's
        # name or alias, update that entry instead of creating a new one.
        target_id = eid
        new_aliases = u.get("aliases", [])
        if isinstance(new_aliases, list):
            for a in new_aliases:
                a = _normalize_alias(a)
                if not a:
                    continue
                for existing_id, existing in by.items():
                    if existing_id == eid:
                        continue
                    if str(existing.get("name", "")).strip().lower() == a:
                        target_id = existing_id
                        break
                    if a in [x.lower() for x in existing.get("aliases", [])]:
                        target_id = existing_id
                        break
                if target_id != eid:
                    break

        e = by.get(target_id) or {"id": target_id}
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
        # G1: merge aliases
        if isinstance(new_aliases, list):
            _merge_aliases(e, new_aliases)
        by[target_id] = e

    # G1: merge op — move aliases from one entry into another, remove source.
    # Skips pinned entries on either side.
    for m in _lst(ops.get("merge")):
        if not isinstance(m, dict):
            continue
        src_id = slug(m.get("from", ""))
        into_id = slug(m.get("into", ""))
        if not src_id or not into_id or src_id == into_id:
            continue
        src = by.get(src_id)
        into = by.get(into_id)
        if not src or not into:
            continue
        if src.get("pinned") or into.get("pinned"):
            continue
        # Move aliases from source into target
        src_aliases = [x.lower() for x in src.get("aliases", [])]
        if src_aliases:
            _merge_aliases(into, src_aliases)
        # B1: the merged-away source name becomes an alias of the target
        src_name = str(src.get("name") or "").strip().lower()
        if src_name and src_name != str(into.get("name", "")).strip().lower():
            _merge_aliases(into, [src_name])
        # Remove the source entry
        by.pop(src_id, None)

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
        name = e.get("name", "")
        aliases = e.get("aliases", [])
        if aliases:
            lines.append(f"- {name} (aka {', '.join(aliases)}): {e.get('state')}")
        else:
            lines.append(f"- {name}: {e.get('state')}")
    if closed:
        lines.append("[PAST / RESOLVED — do not revive]")
        lines += [f"- {e.get('name')}: {e.get('state')}" for e in closed]
    return "\n".join(lines)


def resolve_ledger_ref(ledger, ref):
    """Resolve a keep_true reference by id, name, or alias.

    Returns the matching entry dict or None.
    """
    ref = str(ref).strip().lower()
    if not ref:
        return None
    for e in ledger:
        if str(e.get("id", "")).strip().lower() == ref:
            return e
        if str(e.get("name", "")).strip().lower() == ref:
            return e
        for a in e.get("aliases", []):
            if str(a).strip().lower() == ref:
                return e
    return None
