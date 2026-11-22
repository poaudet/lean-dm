"""Deterministic style repetition guard."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence


def compute_style_guard(history: Sequence[Mapping[str, str]]) -> list[str]:
    dm_msgs = [message["content"] for message in history if message.get("role") == "assistant"]
    if not dm_msgs:
        return []
    openers = []
    for message in dm_msgs[-8:]:
        words = message.strip().split()
        if words:
            opener = " ".join(words[:5])
            if opener and opener not in openers:
                openers.append(opener)
    counts: Counter[str] = Counter()
    for message in dm_msgs[-12:]:
        words = re.findall(r"\b[A-Za-z0-9'-]+\b", message.lower())
        for index in range(len(words) - 2):
            counts[f"{words[index]} {words[index + 1]} {words[index + 2]}"] += 1
    trigrams = [
        trigram for trigram, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= 3
    ]
    combined = list(openers)
    combined.extend(trigram for trigram in trigrams if trigram not in combined)
    return combined[:8]
