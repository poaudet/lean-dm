"""conftest.py — pytest configuration for lean-dm tests.

Sets DM_CAMPAIGNS_DIR to a temporary directory for every test so that no test
ever touches the real campaigns/ directory.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def isolated_campaigns_dir(tmp_path, monkeypatch):
    """Redirect all dm.py path operations to a fresh temp directory."""
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    monkeypatch.setenv("DM_CAMPAIGNS_DIR", str(campaigns))
    # Also patch dm module-level globals so imports that happened before the env
    # was set also pick up the new directory.
    import dm
    monkeypatch.setattr(dm, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(dm, "ROOT", campaigns / dm.CAMPAIGN)
    # Recalculate derived path constants
    root = campaigns / dm.CAMPAIGN
    monkeypatch.setattr(dm, "ROOT", root)
    monkeypatch.setattr(dm, "HISTORY", root / "history.jsonl")
    monkeypatch.setattr(dm, "UNDONE", root / "undone.jsonl")
    monkeypatch.setattr(dm, "PREMISE", root / "premise.md")
    monkeypatch.setattr(dm, "SUMMARY", root / "summary.md")
    monkeypatch.setattr(dm, "LEDGER", root / "ledger.json")
    monkeypatch.setattr(dm, "AUDIT", root / "audit.md")
    monkeypatch.setattr(dm, "STATE", root / "state.json")
    yield campaigns
