"""Campaign-owned paths and synchronization primitives."""
from __future__ import annotations

import pathlib
import threading
from os import PathLike


class Campaign:
    """Filesystem paths and locks for one active campaign."""
    def __init__(self, campaigns_dir: str | PathLike[str], name: str) -> None:
        self.name = name
        self.campaigns_dir = pathlib.Path(campaigns_dir)
        self.root = self.campaigns_dir / name
        self.history = self.root / "history.jsonl"
        self.undone = self.root / "undone.jsonl"
        self.premise = self.root / "premise.md"
        self.summary = self.root / "summary.md"
        self.ledger = self.root / "ledger.json"
        self.audit = self.root / "audit.md"
        self.state = self.root / "state.json"
        self.turn_lock = threading.Lock()
        self.maintenance_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.job_lock = threading.Lock()
