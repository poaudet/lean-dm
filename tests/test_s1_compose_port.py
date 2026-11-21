"""Sprint 1 / 07 - a DM_PORT in .env must not change the in-container listen port."""
import pathlib

import config

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yaml"


def test_env_file_port_is_imported_on_first_run_when_unpinned(monkeypatch, tmp_path):
    """Documents the mechanism: without a pinned env var the .env value wins."""
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.delenv("DM_PORT", raising=False)
    (tmp_path / ".env").write_text("DM_PORT=8080\n", encoding="utf-8")
    c = config.Config(tmp_path / "settings.json")
    assert c.port == 8080


def test_pinned_env_var_beats_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.setenv("DM_PORT", "7860")
    (tmp_path / ".env").write_text("DM_PORT=8080\n", encoding="utf-8")
    c = config.Config(tmp_path / "settings.json")
    assert c.port == 7860


def test_compose_pins_container_port_and_has_no_env_mount():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "DM_PORT=7860" in text
    assert ":/app/.env" not in text
    assert "SUMMARY_EVERY" not in text and "FACTS_CAP" not in text
