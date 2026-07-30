"""Ablation planning for agent context files.

The premise: CLAUDE.md, skill files, and rule files are paid for on every single
turn of every session, forever, but nobody knows which parts earn their keep.
Influence is not observable (see report.py), so the only honest answer is an
experiment -- remove a section, re-run a task suite, see whether anything
changed.

This module does the deterministic half: split the file into candidate
sections, weigh them, and lay out a greedy leave-one-out plan with a cost
estimate. Running the plan needs your agent, your tasks, and your API budget.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# Rough and deliberately conservative. Real counts need the provider's
# tokenizer; this is for ranking sections and sizing an experiment, not billing.
CHARS_PER_TOKEN = 3.6

# Lines that read as instructions rather than prose. A section made mostly of
# these is a rules block, which can be checked with assertions instead of an
# LLM judge -- much cheaper and far less noisy.
IMPERATIVE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+\.\s+)?"
    r"(?:always|never|do not|don't|avoid|prefer|use|run|ensure|make sure|"
    r"must|should|only|before|after|when)\b",
    re.IGNORECASE,
)


@dataclass
class Section:
    heading: str
    start_line: int
    end_line: int
    body: str
    imperative_lines: int = 0
    total_lines: int = 0

    @property
    def chars(self) -> int:
        return len(self.body)

    @property
    def est_tokens(self) -> int:
        return int(self.chars / CHARS_PER_TOKEN)

    @property
    def rule_density(self) -> float:
        return self.imperative_lines / self.total_lines if self.total_lines else 0.0

    @property
    def check_strategy(self) -> str:
        """How you'd verify this section still matters after removing it."""
        if self.rule_density >= 0.4:
            return "assertion"   # checkable rules; write a predicate per rule
        if self.total_lines <= 3:
            return "skip"        # too small to be worth a run
        return "judge"           # prose/context; needs output comparison


@dataclass
class Plan:
    path: str
    sections: list = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(s.est_tokens for s in self.sections)

    def candidates(self, min_tokens: int = 40):
        return [
            s for s in self.sections
            if s.est_tokens >= min_tokens and s.check_strategy != "skip"
        ]


def split_sections(text: str) -> list:
    """Split on markdown headings; fall back to blank-line blocks."""
    lines = text.splitlines()
    marks = [i for i, l in enumerate(lines) if re.match(r"^#{1,6}\s+\S", l)]

    if not marks:
        blocks, cur, start = [], [], 0
        for i, l in enumerate(lines):
            if l.strip():
                if not cur:
                    start = i
                cur.append(l)
            elif cur:
                blocks.append((start, i - 1, cur))
                cur = []
        if cur:
            blocks.append((start, len(lines) - 1, cur))
        marks_out = []
        for s, e, blk in blocks:
            marks_out.append(_mk(blk[0][:60].strip(), s, e, "\n".join(blk)))
        return marks_out

    out = []
    if marks[0] > 0:
        pre = lines[: marks[0]]
        if any(l.strip() for l in pre):
            out.append(_mk("(preamble)", 0, marks[0] - 1, "\n".join(pre)))
    for idx, m in enumerate(marks):
        end = marks[idx + 1] - 1 if idx + 1 < len(marks) else len(lines) - 1
        body = "\n".join(lines[m : end + 1])
        out.append(_mk(lines[m].lstrip("#").strip(), m, end, body))
    return out


def _mk(heading, start, end, body) -> Section:
    content = [l for l in body.splitlines()[1:] if l.strip()]
    return Section(
        heading=heading or "(untitled)",
        start_line=start + 1,
        end_line=end + 1,
        body=body,
        imperative_lines=sum(1 for l in content if IMPERATIVE.match(l)),
        total_lines=len(content),
    )


def plan(path: Path) -> Plan:
    p = Path(path)
    return Plan(path=str(p), sections=split_sections(p.read_text(encoding="utf-8", errors="replace")))


def render_plan(pl: Plan, runs_per_condition: int = 3, tasks: int = 5) -> str:
    cands = pl.candidates()
    L = [
        "",
        f"{pl.path}  --  {pl.total_tokens:,} est. tokens, {len(pl.sections)} sections",
        "  Paid on every turn of every session. Sections below are ablation candidates.",
        "",
        f"  {'section':<34}{'tokens':>8}{'rules':>8}  check",
        "  " + "-" * 62,
    ]
    for s in sorted(pl.sections, key=lambda x: -x.est_tokens):
        mark = " " if s in cands else "·"
        h = s.heading if len(s.heading) <= 32 else s.heading[:31] + "…"
        L.append(
            f"  {mark}{h:<33}{s.est_tokens:>8}{s.rule_density:>7.0%}  {s.check_strategy}"
        )

    n_runs = len(cands) * runs_per_condition * tasks
    base = runs_per_condition * tasks
    L += [
        "",
        "  · = skipped (too small to be worth a run)",
        "",
        "GREEDY LEAVE-ONE-OUT PLAN",
        "  " + "-" * 62,
        f"  {len(cands)} candidate sections x {tasks} tasks x {runs_per_condition} runs",
        f"  = {n_runs} ablation runs + {base} baseline runs = {n_runs + base} total",
        "",
        "  assertion sections: write one predicate per rule, check it still holds",
        "                      without the instruction. If it does, the rule is",
        "                      already the model's default -- delete it.",
        "  judge sections:     compare outputs against baseline. Noisier, needs",
        "                      more runs, and costs tokens to save tokens.",
        "",
        "  Validity warning: your task suite must actually exercise these rules.",
        "  A suite that never deploys will 'prove' the deployment rules are dead",
        "  weight, and it will be wrong.",
        "",
    ]
    return "\n".join(L)
