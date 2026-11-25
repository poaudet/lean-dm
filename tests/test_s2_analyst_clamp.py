"""Sprint 2 / 11 - the analyst cannot override the scheduler's overt/subtle decision."""
import analyst

SCHED = {
    "p1.s1": {"id": "p1.s1", "how": "subtle", "note": "Scout seen"},
    "p1.s2": {"id": "p1.s2", "how": "overt", "note": "Poisoner arrives"},
}


def test_scheduled_how_and_note_override_conflicting_model_values():
    raw = {"plot_moves": [
        {"id": "p1.s1", "how": "overt", "note": "Conflicting model note"},
        {"id": "p1.s1", "how": "overt", "note": "dup"},
        {"id": "zzz", "how": "overt", "note": "not allowed"},
    ]}
    out = analyst.clamp_brief(raw, SCHED)
    assert out["plot_moves"] == [
        {"id": "p1.s1", "how": "subtle", "note": "Scout seen"},
        {"id": "p1.s2", "how": "overt", "note": "Poisoner arrives"},
    ]


def test_scheduled_how_cannot_be_downgraded_by_model():
    out = analyst.clamp_brief({"plot_moves": [{"id": "p1.s2", "how": "offscreen", "note": "x"}]}, SCHED)
    assert out["plot_moves"][0] == {"id": "p1.s2", "how": "overt", "note": "Poisoner arrives"}


def test_set_of_ids_still_supported():
    out = analyst.clamp_brief({"plot_moves": [{"id": "a", "how": "overt"}, {"id": "b"}]}, {"a"})
    assert [m["id"] for m in out["plot_moves"]] == ["a"]
    assert out["plot_moves"][0]["how"] == "overt"


def test_empty_allowed_rejects_all():
    assert analyst.clamp_brief({"plot_moves": [{"id": "a"}]}, {})["plot_moves"] == []


def test_fallback_reason_is_persisted_only_as_diagnostic(tmp_path):
    brief = analyst.make_fallback_brief("timeout")
    analyst.save_brief(tmp_path, 0, "fallback", "model", 100, brief)

    entry = analyst.load_briefs(tmp_path)[0]
    assert entry["diagnostics"] == {"fallback_reason": "timeout"}
    assert "diagnostics" not in entry["brief"]
    assert "timeout" not in __import__("json").dumps(entry["brief"])
    assert analyst.make_fallback_brief("private model response").diagnostics == {
        "fallback_reason": "analyst_error"
    }


def test_analyst_exception_is_classified_without_persisting_exception_text(tmp_path):
    def fail(messages, **kwargs):
        raise RuntimeError("private response or credential")

    brief, source, model, ms = analyst.run_analyst(
        tmp_path, [], "action", [], [], fail
    )
    analyst.save_brief(tmp_path, 0, source, model, ms, brief)

    entry = analyst.load_briefs(tmp_path)[0]
    assert entry["diagnostics"] == {"fallback_reason": "analyst_error"}
    assert "private response or credential" not in str(entry)
