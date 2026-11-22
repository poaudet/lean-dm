"""conftest.py — pytest configuration for lean-dm tests.

Sets DM_CAMPAIGNS_DIR to a temporary directory for every test so that no test
ever touches the real campaigns/ directory.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_campaigns_dir(tmp_path, monkeypatch):
    """Redirect all dm.py path operations to a fresh temp directory."""
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    monkeypatch.setenv("DM_CAMPAIGNS_DIR", str(campaigns))
    import dm
    dm.POST_COUNTS.clear()
    dm.AUTH_FAILURES.clear()
    previous = dm.CAMPAIGN_CONTEXT
    dm.bind_campaign(campaigns, dm.CAMPAIGN)
    yield campaigns
    dm.bind_campaign(previous)
