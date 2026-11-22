"""A local-model regression for the real web turn and three-act pipeline."""
import json
from collections import defaultdict, deque

import analyst
import arc
import dm


class LocalModel:
    def __init__(self):
        self.calls = []
        self.narrations = 0
        self.mid_records = 0

    def __call__(self, payload, stream=False):
        self.calls.append(payload)
        model = payload["model"]
        system = payload["messages"][0]["content"]
        prompt = payload["messages"][-1]["content"]
        if model == "analyst":
            moves = []
            if "Search the alley" in prompt:
                moves = [{"id": "p1.s1", "how": "overt", "note": "A scout appears"}]
            content = json.dumps({"intent": "act", "assessment": "Proceed",
                                  "plot_moves": moves, "act_complete": False})
        elif model == "narrator":
            self.narrations += 1
            content = f"Narration {self.narrations}: the story continues."
        elif "master story planner" in system:
            content = json.dumps({
                "central_question": "Can the witness be saved?",
                "acts": [
                    {"n": 1, "status": "active", "goal": "Find the witness",
                     "beats": [
                         {"id": "a1b1", "text": "Find a clue", "status": "pending"},
                         {"id": "a1b2", "text": "Find the witness", "status": "pending"},
                     ], "outcomes": []},
                    {"n": 2, "status": "planned", "goal": "Escape", "beats": [], "outcomes": []},
                    {"n": 3, "status": "planned", "goal": "Save the witness", "beats": [], "outcomes": []},
                ],
                "pressures": [{
                    "id": "p1", "antagonist": "Hunters", "goal": "Capture the witness",
                    "status": "active", "steps": [
                        {"id": "p1.s1", "at_minutes": 5, "effect": "Scout appears", "status": "pending"},
                    ], "deadline_minutes": 200, "effective_deadline_minutes": 200,
                }],
            })
        elif "continuity recorder" in system:
            elapsed, beats, resolved = 5, [], False
            if "PLAYER: Search the alley" in prompt:
                self.mid_records += 1
                elapsed = (10, 12, 7)[self.mid_records - 1]
                beats = ["a1b1"]
            elif "PLAYER: Find the witness" in prompt:
                beats = ["a1b2"]
            elif "PLAYER: Escape the hunters" in prompt:
                beats = ["a2b1"]
            elif "PLAYER: Confront the hunters" in prompt:
                beats = ["a3b1"]
            elif "PLAYER: Secure the witness" in prompt:
                resolved = True
            content = json.dumps({"elapsed_minutes": elapsed, "beats_done": beats,
                                  "pressures": [], "act_complete": resolved,
                                  "central_question_resolved": resolved})
        elif "Summarize factual play outcomes" in system:
            act_n = int(prompt.split("ACT ", 1)[1][0])
            content = json.dumps([f"Act {act_n} outcome from play"])
        elif "story planner" in system:
            act_n = 2 if "ACT 2 GOAL" in prompt else 3
            content = json.dumps([{"id": f"a{act_n}b1", "text": "Next obstacle",
                                   "status": "pending"}])
        else:
            raise AssertionError(f"Unexpected local model call: {model}: {system[:80]}")
        return LocalResponse(content, stream)


class LocalResponse:
    def __init__(self, content, stream):
        self.content = content
        self.stream = stream

    def json(self):
        assert not self.stream
        return {"choices": [{"message": {"content": self.content}}]}

    def iter_lines(self):
        assert self.stream
        for piece in (self.content[:12], self.content[12:]):
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]})).encode()
        yield b"data: [DONE]"

    def close(self):
        pass


def test_three_act_stream_undo_retry_and_terminal(monkeypatch, isolated_campaigns_dir):
    root = dm.ROOT
    dm.init_files()
    monkeypatch.setattr(dm.cfg, "_vals", {
        **dm.cfg._vals, "api_key": "local-only", "model": "narrator",
        "analyst_model": "analyst", "scribe_model": "scribe", "planner_model": "planner",
        "arc_enabled": True, "analyst_enabled": True, "ledger_enabled": False,
        "notes_routing": False, "style_guard": False, "audit_every_turns": 0,
        "difficulty": "standard",
    })
    local = LocalModel()
    monkeypatch.setattr(dm, "_raw_post", local)
    monkeypatch.setattr(dm, "POST_COUNTS", defaultdict(deque))

    def join_maintenance():
        if dm.MAINT_THREAD:
            dm.MAINT_THREAD.join(timeout=5)
            assert not dm.MAINT_THREAD.is_alive()

    def turn(client, text):
        response = client.post("/api/turn", json={"text": text})
        assert response.status_code == 200, response.get_data(as_text=True)
        join_maintenance()
        return response.get_json()["dm"]

    with dm.app.test_client() as client:
        response = client.post("/api/stream", json={"text": "Begin the search"}, buffered=True)
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert [e["stage"] for e in events if "stage" in e] == [
            "analyzing", "narrating", "recording",
        ]
        assert "".join(e["chunk"] for e in events if "chunk" in e) == "Narration 1: the story continues."
        assert response.get_data(as_text=True).endswith("data: [DONE]\n\n")
        join_maintenance()
        assert arc.load_arc(root)["clock"]["minutes"] == 5
        assert len(analyst.load_briefs(root)) == 1

        assert turn(client, "Search the alley") == "Narration 2: the story continues."
        state = arc.load_arc(root)
        assert state["clock"]["minutes"] == 15
        assert state["acts"][0]["beats"][0]["status"] == "done"
        assert state["pressures"][0]["steps"][0]["status"] == "done"
        assert state["runtime"]["last_overt_turn"] == 2
        analyst_prompt = next(c["messages"][-1]["content"] for c in local.calls
                              if c["model"] == "analyst" and "Search the alley" in c["messages"][-1]["content"])
        assert '"id": "p1.s1"' in analyst_prompt
        narrator_prompt = [c["messages"][-1]["content"] for c in local.calls
                           if c["model"] == "narrator"][-1]
        assert "DIRECTOR'S BRIEF" in narrator_prompt and '"id": "p1.s1"' in narrator_prompt
        response = client.post("/api/undo")
        assert response.status_code == 200 and response.get_json()["text"] == "Search the alley"
        state = arc.load_arc(root)
        assert state["clock"]["minutes"] == 5
        assert state["acts"][0]["beats"][0]["status"] == "pending"
        assert state["pressures"][0]["steps"][0]["status"] == "pending"
        assert "last_overt_turn" not in state.get("runtime", {})
        assert [r["msg"] for r in arc.load_records(root)] == [1]
        assert set(analyst.load_briefs(root)) == {0}
        assert len(dm.load_history()) == 2

        assert turn(client, "Search the alley") == "Narration 3: the story continues."
        assert arc.load_arc(root)["clock"]["minutes"] == 17
        analyst_calls = sum(c["model"] == "analyst" for c in local.calls)
        response = client.post("/api/retry")
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["dm"] == "Narration 4: the story continues."
        join_maintenance()
        assert sum(c["model"] == "analyst" for c in local.calls) == analyst_calls
        assert arc.load_arc(root)["clock"]["minutes"] == 12
        assert arc.load_arc(root)["pressures"][0]["steps"][0]["status"] == "done"
        assert [r["elapsed_minutes"] for r in arc.load_records(root)] == [5, 7]
        assert [m["content"] for m in dm.load_history()][-2:] == [
            "Search the alley", "Narration 4: the story continues.",
        ]
        undone = [json.loads(line) for line in dm.UNDONE.read_text().splitlines()]
        assert [m["content"] for m in undone] == [
            "Search the alley", "Narration 2: the story continues.",
            "Narration 3: the story continues.",
        ]

        turn(client, "Find the witness")
        state = arc.load_arc(root)
        assert state["act"] == 2
        assert [a["status"] for a in state["acts"]] == ["resolved", "active", "planned"]
        assert state["acts"][0]["outcomes"] == ["Act 1 outcome from play"]
        assert state["acts"][1]["beats"][0]["id"] == "a2b1"

        turn(client, "Escape the hunters")
        state = arc.load_arc(root)
        assert state["act"] == 3
        assert [a["status"] for a in state["acts"]] == ["resolved", "resolved", "active"]
        assert state["acts"][2]["beats"][0]["id"] == "a3b1"

        turn(client, "Confront the hunters")
        state = arc.load_arc(root)
        assert state["acts"][2]["beats"][0]["status"] == "done"
        assert state["acts"][2]["status"] == "active"
        assert not state.get("terminal", False)
        assert arc.load_records(root)[-1]["act_complete"] is False

        turn(client, "Secure the witness")
        state = arc.load_arc(root)
        assert state["terminal"] is True and state["central_question_resolved"] is True
        assert [a["status"] for a in state["acts"]] == ["resolved"] * 3
        assert state["acts"][2]["outcomes"] == ["Act 3 outcome from play"]
        assert state["clock"]["minutes"] == 32
        assert [r["msg"] for r in arc.load_records(root)] == [1, 3, 5, 7, 9, 11]
        log = [json.loads(line) for line in arc.arc_log_path(root).read_text().splitlines()]
        assert [(entry["act"], entry["at_msg"]) for entry in log] == [(1, 5), (2, 7), (3, 11)]
        assert all(entry["source"] == "analyst" for entry in analyst.load_briefs(root).values())
        summaries = [c["messages"][-1]["content"] for c in local.calls
                     if "Summarize factual play outcomes" in c["messages"][0]["content"]]
        assert len(summaries) == 3
        assert "Find the witness" in summaries[0] and '"msg": 5' in summaries[0]
        assert "Escape the hunters" in summaries[1] and '"msg": 7' in summaries[1]
        assert "Secure the witness" in summaries[2] and '"msg": 11' in summaries[2]
