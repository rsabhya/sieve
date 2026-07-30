"""End-to-end check on a synthetic repo and realistic request bodies."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sieve import index as ix
from sieve import parse, store
from sieve.detect import detect

SECRET = '''"""Token bucket rate limiter with exponential decay."""
import math
from decimal import Decimal

DECAY_HALFLIFE_SECONDS = 41.5
MIN_ALLOWANCE = Decimal("0.0035")


def decay_weight(age_seconds: float) -> float:
    return math.pow(0.5, age_seconds / DECAY_HALFLIFE_SECONDS)


def compute_allowance(events, penalty_bps, capacity_ratio):
    numerator = Decimal(0)
    denominator = Decimal(0)
    for event in events:
        w = Decimal(str(decay_weight(event.age_seconds)))
        numerator += w * event.weight * event.success_rate
        denominator += w * event.weight
    if denominator == 0:
        return MIN_ALLOWANCE
    base = numerator / denominator
    adjusted = base * (1 - Decimal(str(penalty_bps)) / 10000)
    return max(adjusted * Decimal(str(capacity_ratio)), MIN_ALLOWANCE)
'''

ROUTER = '''"""Request dispatcher. Boring, not secret."""
from .limiter import compute_allowance


def dispatch(request, workers):
    ranked = sorted(workers, key=lambda w: -compute_allowance(
        w.events, w.penalty_bps, w.capacity_ratio))
    for worker in ranked:
        if worker.accepts(request):
            return worker
    raise RuntimeError("no worker accepted request")
'''

UNRELATED = '''import os
import sys
import logging

logger = logging.getLogger(__name__)


def configure(level="INFO"):
    logging.basicConfig(level=level)
    return logger
'''


def make_repo(root: Path):
    (root / "src").mkdir(parents=True)
    (root / "src" / "limiter.py").write_text(SECRET)
    (root / "src" / "dispatch.py").write_text(ROUTER)
    (root / "src" / "logging_setup.py").write_text(UNRELATED)
    (root / "README.md").write_text("# demo repo\n\nNothing to see.\n")


def read_tool_output(text: str) -> str:
    """Mimic Claude Code's Read tool: line numbers + tab."""
    return "\n".join(f"{i:6d}\t{ln}" for i, ln in enumerate(text.splitlines(), 1))


def grep_output(path: str, text: str) -> str:
    return "\n".join(f"{path}:{i}:{ln}" for i, ln in enumerate(text.splitlines(), 1))


def anthropic_body(tool_name, tool_input, tool_result, assistant_text=""):
    return {
        "model": "claude-x",
        "system": [{"type": "text", "text": "You are a coding agent."}],
        "messages": [
            {"role": "user", "content": "fix the dispatch bug"},
            {
                "role": "assistant",
                "content": (
                    ([{"type": "text", "text": assistant_text}] if assistant_text else [])
                    + [{"type": "tool_use", "id": "toolu_1", "name": tool_name, "input": tool_input}]
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": tool_result}],
                    }
                ],
            },
        ],
    }


def openai_body(tool_result):
    return {
        "model": "gpt-x",
        "messages": [
            {"role": "user", "content": "fix the dispatch bug"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "src/limiter.py"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": tool_result},
        ],
    }


def responses_body(tool_result):
    return {
        "model": "gpt-x",
        "input": [
            {"role": "user", "content": "fix it"},
            {"type": "function_call", "call_id": "c1", "name": "cat", "arguments": '{"path":"src/limiter.py"}'},
            {"type": "function_call_output", "call_id": "c1", "output": tool_result},
        ],
    }


def run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_repo(root)

        entries, stats = ix.build(root)
        conn = store.connect(root)
        store.replace_index(conn, entries)
        lookup = ix.load_lookup(conn)
        known = {e[0] for e in entries}

        print(f"index: {stats['files_indexed']}/{stats['files_seen']} files, "
              f"{stats['shingles_total']} shingles, "
              f"{stats['shingles_dropped_common']} common dropped")
        print(f"indexed paths: {sorted(known)}\n")

        cases = [
            ("Read tool (numbered)", anthropic_body(
                "Read", {"file_path": str(root / "src/limiter.py")},
                read_tool_output(SECRET))),
            ("bash cat (bare)", anthropic_body(
                "Bash", {"command": "cat src/limiter.py"}, SECRET)),
            ("grep -rn (path:line:)", anthropic_body(
                "Grep", {"pattern": "decay"},
                grep_output("src/limiter.py", SECRET))),
            ("partial read (half file)", anthropic_body(
                "Read", {"file_path": "src/limiter.py"},
                read_tool_output("\n".join(SECRET.splitlines()[:14])))),
            ("reindented + renamed vars", anthropic_body(
                "Read", {"file_path": "src/limiter.py"},
                SECRET.replace("    ", "\t").replace("numerator", "num_acc"))),
            ("OpenAI chat completions", openai_body(SECRET)),
            ("OpenAI responses API", responses_body(read_tool_output(SECRET))),
            ("unrelated file only", anthropic_body(
                "Read", {"file_path": "src/logging_setup.py"},
                read_tool_output(UNRELATED))),
            ("no file content at all", anthropic_body(
                "Bash", {"command": "ls"}, "src\nREADME.md\n" * 30)),
        ]

        print(f"{'case':<30} {'detected':<40} verdict")
        print("-" * 92)
        failures = []
        for label, body in cases:
            strings, tool_paths = parse.extract(body)
            dets = detect(strings, lookup)
            found = {d.path for d in dets}
            desc = ", ".join(f"{d.path}({d.matched}/{d.total})" for d in dets) or "-"

            if label == "unrelated file only":
                ok = "src/limiter.py" not in found and "src/logging_setup.py" in found
            elif label == "no file content at all":
                ok = not found
            else:
                ok = "src/limiter.py" in found
            if not ok:
                failures.append(label)
            print(f"{label:<30} {desc:<40} {'PASS' if ok else 'FAIL'}")

        print()
        # session stability across turns
        s1 = parse.session_id(cases[0][1])
        s2 = parse.session_id(cases[1][1])
        s3 = parse.session_id(openai_body(SECRET))
        print(f"session id stable across same-convo turns: {s1 == s2}")
        print(f"session id differs across conversations:   {s1 != s3}")

        # usage evidence
        edit_body = anthropic_body(
            "Edit", {"file_path": "src/dispatch.py", "old_string": "a", "new_string": "b"},
            "ok", assistant_text="I'll update src/dispatch.py now")
        _, tps = parse.extract(edit_body)
        ev = parse.usage_evidence(tps, known, parse.assistant_text_of(edit_body))
        print(f"usage evidence: {sorted(set(ev))}")

        conn.close()
        return failures


if __name__ == "__main__":
    fails = run()
    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    sys.exit(1 if fails else 0)
