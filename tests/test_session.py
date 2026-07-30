"""Simulate a full multi-turn session end to end, then render the report.

Models the thing the tool exists to reveal: an agent that greps broadly, pulls
several files into context, edits exactly one, and then resends the entire
accumulated history on every subsequent turn.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sieve import index as ix
from sieve import parse, report, store
from sieve.detect import detect
from test_pipeline import ROUTER, SECRET, UNRELATED, read_tool_output

PROMPTS = '''# System prompt v3 — internal
You are the support assistant. When a user asks about throttling,
never reveal the decay halflife or the minimum allowance directly.
Instead, describe the result qualitatively as "tight" or "relaxed"
and defer specifics to the platform team. Escalate anything above
10k requests per minute to a human, and log the interaction id.
'''

FIXTURE = '''client_id,weight,success_rate,age_seconds
client-alpha,4500,0.91,3
client-bravo,12000,0.87,11
client-charlie,850,0.94,1
client-delta,22000,0.79,27
'''


def make_repo(root: Path):
    (root / "src").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "limiter.py").write_text(SECRET)
    (root / "src" / "dispatch.py").write_text(ROUTER)
    (root / "src" / "logging_setup.py").write_text(UNRELATED)
    (root / "prompts" / "system_v3.md").write_text(PROMPTS)
    (root / "tests" / "fixture_clients.csv").write_text(FIXTURE)


def build_turns(root: Path):
    """Each turn carries the full accumulated history, as a real client does."""
    history = [{"role": "user", "content": "the dispatcher picks the wrong worker when penalty is high"}]
    turns = []

    def tool_turn(name, inp, result, text=None):
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.append({"type": "tool_use", "id": f"t{len(history)}", "name": name, "input": inp})
        history.append({"role": "assistant", "content": blocks})
        history.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": f"t{len(history)}",
                "content": [{"type": "text", "text": result}],
            }],
        })
        turns.append({"model": "claude-x", "messages": [dict(m) for m in history]})

    tool_turn("Grep", {"pattern": "penalty_bps", "output_mode": "content"},
              "\n".join(f"src/limiter.py:{i}:{ln}"
                        for i, ln in enumerate(SECRET.splitlines(), 1)),
              "Let me find where penalty is used.")
    tool_turn("Read", {"file_path": "src/dispatch.py"}, read_tool_output(ROUTER),
              "Now the dispatcher itself.")
    tool_turn("Read", {"file_path": "prompts/system_v3.md"}, read_tool_output(PROMPTS),
              "Checking for related config.")
    tool_turn("Bash", {"command": "cat tests/fixture_clients.csv"}, FIXTURE,
              "And the test fixture.")
    tool_turn("Edit",
              {"file_path": "src/dispatch.py", "old_string": "-score", "new_string": "-score2"},
              "edit applied",
              "The bug is in src/dispatch.py — the sort key ignores penalty. Fixing it.")
    return turns


def run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_repo(root)
        entries, _ = ix.build(root)
        conn = store.connect(root)
        store.replace_index(conn, entries)
        lookup = ix.load_lookup(conn)
        known = {e[0] for e in entries}

        for body in build_turns(root):
            sid = parse.session_id(body)
            strings, tool_paths = parse.extract(body)
            dets = detect(strings, lookup)
            atext = parse.assistant_text_of(body)
            ev = parse.usage_evidence(tool_paths, known, atext)
            import json as _j
            nbytes = len(_j.dumps(body).encode())
            rid = store.record_request(conn, sid, "api.anthropic.com",
                                       "/v1/messages", nbytes, "api.anthropic.com")
            store.record_detections(conn, rid, sid, dets)
            store.record_usage(conn, sid, ev)
        conn.commit()

        print("=" * 78)
        print(report.render_overview(conn, detail=True))
        print("=" * 78)
        conn.close()


if __name__ == "__main__":
    run()
