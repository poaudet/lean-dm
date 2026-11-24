"""FIX4 (defect 3) - the analyst request is bounded by analyst_timeout_sec.

The analyst timeout fired and returned a fallback brief, but the worker thread's
HTTP call stayed alive for cfg.timeout_sec (600 s by default). The fix threads an
explicit timeout through chat_complete -> _raw_post.
"""
import dm


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals,
        "api_key": "test", "model": "test-model",
        "timeout_sec": 600, "analyst_timeout_sec": 25,
        **over,
    })


class _Resp:
    status_code = 200
    headers = {}
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def test_raw_post_uses_an_explicit_timeout(monkeypatch):
    _cfg(monkeypatch)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm._raw_post({"model": "m", "messages": []}, timeout=12.5)

    assert seen["timeout"] == (10, 12.5)


def test_raw_post_falls_back_to_the_read_timeout(monkeypatch):
    _cfg(monkeypatch, timeout_sec=600)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm._raw_post({"model": "m", "messages": []})

    assert seen["timeout"] == (10, 600)


def test_chat_complete_forwards_the_timeout(monkeypatch):
    _cfg(monkeypatch)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm.chat_complete([{"role": "user", "content": "x"}], model="m", timeout=9.0)

    assert seen["timeout"] == (10, 9.0)


def test_timeout_never_reaches_the_request_payload(monkeypatch):
    """**extra is splatted into the payload, so timeout must be a named parameter."""
    _cfg(monkeypatch)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["payload"] = json
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm.chat_complete([{"role": "user", "content": "x"}], model="m", timeout=9.0)

    assert "timeout" not in seen["payload"]
    assert seen["payload"]["model"] == "m"


def test_extra_kwargs_still_reach_the_payload(monkeypatch):
    _cfg(monkeypatch)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["payload"] = json
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm.chat_complete([{"role": "user", "content": "x"}], model="m",
                     timeout=5.0, temperature=0.7)

    assert seen["payload"]["temperature"] == 0.7
    assert seen["timeout"] == (10, 5.0)


def test_analyst_passes_its_remaining_budget(monkeypatch):
    """A fake _raw_post records the timeout it receives; it must be <= analyst_timeout_sec."""
    _cfg(monkeypatch, analyst_timeout_sec=25)
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen.append(timeout)
        return _Resp()

    monkeypatch.setattr(dm.requests, "post", fake_post)

    dm.chat_complete([{"role": "user", "content": "x"}], model="m", timeout=25)

    assert seen
    assert all(t[1] <= 25 for t in seen)
