"""Sprint 2 / 11 - the analyst cannot override the scheduler's overt/subtle decision."""
import analyst

SCHED = {
    "p1.s1": {"id": "p1.s1", "how": "subtle", "note": "Scout seen"},
    "p1.s2": {"id": "p1.s2", "how": "overt", "note": "Poisoner arrives"},
}


def test_how_cannot_be_upgraded_and_note_falls_back_to_schedule():
    raw = {"plot_moves": [
        {"id": "p1.s1", "how": "overt", "note": ""},
        {"id": "p1.s1", "how": "overt", "note": "dup"},
        {"id": "zzz", "how": "overt", "note": "not allowed"},
    ]}
    out = analyst.clamp_brief(raw, SCHED)
    assert out["plot_moves"] == [{"id": "p1.s1", "how": "subtle", "note": "Scout seen"}]


def test_how_can_be_downgraded():
    out = analyst.clamp_brief({"plot_moves": [{"id": "p1.s2", "how": "offscreen", "note": "x"}]}, SCHED)
    assert out["plot_moves"][0]["how"] == "offscreen"


def test_set_of_ids_still_supported():
    out = analyst.clamp_brief({"plot_moves": [{"id": "a", "how": "overt"}, {"id": "b"}]}, {"a"})
    assert [m["id"] for m in out["plot_moves"]] == ["a"]
    assert out["plot_moves"][0]["how"] == "overt"


def test_empty_allowed_rejects_all():
    assert analyst.clamp_brief({"plot_moves": [{"id": "a"}]}, {})["plot_moves"] == []
