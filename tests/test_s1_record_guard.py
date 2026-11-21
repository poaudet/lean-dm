"""Sprint 1 / 05 - junk recorder output must not abort the record pass."""
import pytest

import arc


@pytest.mark.parametrize("value, expected", [
    (25, 25), ("30", 30), (12.7, 12),
    (0, 1), (-5, 1), (999, 120),
    ("soon", 5), (None, 5), (float("nan"), 5), (float("inf"), 5), ([], 5),
])
def test_clamp_elapsed(value, expected):
    assert arc._clamp_elapsed(value) == expected


def test_record_pass_survives_junk_values(tmp_path):
    root = tmp_path / "camp"
    root.mkdir()
    arc.save_arc(root, {
        "version": 1, "revision": 0, "central_question": "Q?", "act": 1,
        "clock": {"minutes": 100},
        "acts": [{"n": 1, "status": "active", "goal": "g",
                  "beats": [{"id": "a1b1", "text": "x", "status": "pending"}], "outcomes": []}],
        "pressures": [],
    })
    junk = '{"elapsed_minutes": "soon", "beats_done": null, "pressures": null, "act_complete": false}'

    arc.run_record_pass(root, "act", "reply", 1, lambda messages, **kw: junk)

    assert arc.load_arc(root)["clock"]["minutes"] == 105     # default 5 applied
    records = arc.load_records(root)
    assert records and records[0]["elapsed_minutes"] == 5
