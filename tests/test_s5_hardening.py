"""Hardening: malformed arcs, terminal arcs, runtime env overrides, stream filtering, proxy scheme."""
import json

import arc
import dm


def _arc(**over):
    base = {
        "version": 1, "central_question": "Q?", "act": 1, "clock": {"minutes": 90},
        "acts": [{"n": 1, "status": "active", "goal": "g", "beats": []}],
        "pressures": [{"id": "p1", "status": "active",
                       "steps": [{"id": "s1", "status": "pending", "at_minutes": "60", "effect": "x"}]}],
    }
    base.update(over)
    return base


def test_normalize_arc_coerces_and_drops_malformed():
    out = arc.normalize_arc(_arc(
        act="1", clock={"minutes": "5"},
        acts=["junk", {"n": "1", "beats": ["x", {"text": "ok"}]}, {"goal": "no n"}],
        pressures=[{"id": "p", "steps": [{"at_minutes": "oops"}, {"at_minutes": "30"}, "s"]}, 3],
    ))
    assert out["act"] == 1 and out["clock"]["minutes"] == 5
    assert [a["n"] for a in out["acts"]] == [1]
    assert out["acts"][0]["beats"][0]["id"] == "a1b1"
    assert [s["at_minutes"] for s in out["pressures"][0]["steps"]] == [30]
    assert len(out["pressures"]) == 1


def test_allowed_moves_accepts_string_minutes_after_normalize():
    moves = arc.allowed_moves(arc.normalize_arc(_arc()), "standard", 1)
    assert [m["id"] for m in moves] == ["s1"]


def test_get_allowed_moves_never_raises(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "arc_enabled": True})
    monkeypatch.setattr(dm.arc, "load_arc", lambda root: _arc())
    monkeypatch.setattr(dm.arc, "allowed_moves", lambda *a, **k: 1 / 0)
    assert dm.get_allowed_moves([]) == []


def test_arc_chip_survives_non_dict_acts():
    assert arc.get_arc_chip({"act": 1, "acts": ["x", None]}) == "Act 1 · active"


def test_terminal_arc_schedules_nothing():
    assert arc.allowed_moves({**arc.normalize_arc(_arc()), "terminal": True}, "standard", 1) == []


def test_plan_initial_arc_normalizes(monkeypatch):
    raw = json.dumps(_arc(acts=[{"n": "1", "goal": "g", "status": "active", "beats": [
        {"id": "a1b1", "text": "one", "hook": "lead one", "status": "pending"},
        {"id": "a1b2", "text": "two", "hook": "lead two", "status": "pending"},
        {"id": "a1b3", "text": "three", "hook": "lead three", "status": "pending"},
    ]}, "bad"]))
    out = arc.plan_initial_arc("p", "", "", [], lambda *a, **k: raw)
    assert out["acts"][0]["n"] == 1
    assert out["pressures"][0]["steps"][0]["at_minutes"] == 60


def test_prune_arc_snapshots(tmp_path):
    for i in (1, 2, 5):
        (arc.arc_snap_dir(tmp_path) / f"{i}.json").write_text("{}", encoding="utf-8")
    arc.prune_arc_snapshots(tmp_path, 3)
    assert sorted(p.name for p in arc.arc_snap_dir(tmp_path).iterdir()) == ["5.json"]


def test_runtime_env_overrides_existing_settings(monkeypatch):
    monkeypatch.setenv("DM_WEB_PASSWORD", "pw")
    monkeypatch.setenv("DM_HOST", "0.0.0.0")
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "web_password": "", "host": "127.0.0.1"})
    assert dm.cfg.web_password == "pw" and dm.cfg.host == "0.0.0.0"
    dm.validate_web_security()


def test_think_filter_splits_tags_across_chunks():
    f = dm.ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hi <thi", "nk>secret</th", "ink> there"]) + f.flush()
    assert out == "Hi  there"


def test_stream_reports_server_error(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "api_key": "k", "model": "m"})
    def boom(*a, **k):
        raise RuntimeError("kaput")
        yield
    monkeypatch.setattr(dm, "run_turn", boom)
    with dm.app.test_client() as client:
        body = client.post("/api/stream", json={"text": "hi"}).get_data(as_text=True)
    assert '"error": "kaput"' in body and "[DONE]" in body


def test_stream_unconfigured_is_json(monkeypatch):
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "api_key": "", "model": ""})
    with dm.app.test_client() as client:
        r = client.post("/api/stream", json={"text": "hi"})
    assert r.status_code == 400 and "error" in r.get_json()


def test_dropped_connection_at_recording_still_runs_arc_step(monkeypatch):
    dm.ROOT.mkdir(parents=True, exist_ok=True)
    dm.HISTORY.write_text("", encoding="utf-8")
    monkeypatch.setattr(dm.cfg, "_vals", {**dm.cfg._vals, "api_key": "k", "model": "m",
                                          "arc_enabled": True, "analyst_enabled": False,
                                          "ledger_enabled": False})
    monkeypatch.setattr(dm, "build_context", lambda *a, **k: [])
    monkeypatch.setattr(dm, "_generate_stream", lambda ctx: iter([{"chunk": "x"}, {"done": True, "dm": "x"}]))
    monkeypatch.setattr(dm, "start_maintain", lambda: None)
    calls = []
    monkeypatch.setattr(dm, "_arc_step", lambda *a: calls.append(a))
    assert dm.TURN_LOCK.acquire(blocking=False)
    gen = dm.run_turn("hello", stream=True, lock_acquired=True)
    for event in gen:
        if event.get("stage") == "recording":
            break
    gen.close()
    assert len(calls) == 1
    assert dm.TURN_LOCK.acquire(blocking=False)
    dm.TURN_LOCK.release()


def test_closing_narrate_line_without_ledger():
    ctx = dm.build_context([{"role": "user", "content": "go"}], brief={"intent": "act"})
    assert ctx[-1]["content"].endswith("Now narrate the outcome of the player's action above.")
