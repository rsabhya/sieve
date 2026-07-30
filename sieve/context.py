"""Static analysis of a repo's agent context, with no API calls at all.

The premise from ablate.py was one file at a time. Real repos have a CLAUDE.md,
an AGENTS.md, a pile of skills, cursor rules, and MCP definitions -- and the
thing that costs money is not any one file but the total always-on budget
across all of them.

Redundancy across those files is detectable without running anything. If the
same rule appears in CLAUDE.md and in three skill descriptions, you are paying
for it four times per turn and at most one copy is doing work. That is the
cheapest possible win: no runs, no tokens, no eval suite.

Loading semantics decide cost:
  always  -- enters context every turn of every session
  trigger -- skill descriptions; always loaded so the model can decide to load
             the body. People forget these are always-on.
  ondemand-- skill bodies, slash commands; paid only when used
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import rules as rl
from .ablate import CHARS_PER_TOKEN, IMPERATIVE

# (glob, layer, label)
CONTEXT_SOURCES = [
    ("CLAUDE.md", "always", "project instructions"),
    ("CLAUDE.local.md", "always", "local instructions"),
    ("AGENTS.md", "always", "agent instructions"),
    (".cursorrules", "always", "cursor rules"),
    (".cursor/rules/*.md", "always", "cursor rules"),
    (".cursor/rules/*.mdc", "always", "cursor rules"),
    (".github/copilot-instructions.md", "always", "copilot instructions"),
    ("**/CLAUDE.md", "always", "nested instructions"),
    (".claude/skills/*/SKILL.md", "trigger", "skill"),
    ("skills/*/SKILL.md", "trigger", "skill"),
    (".claude/commands/*.md", "ondemand", "slash command"),
    (".claude/agents/*.md", "ondemand", "subagent"),
]

_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Pairs that signal a contradiction when the same subject appears with both.
NEGATORS = (("always", "never"), ("must", "must not"), ("do", "do not"), ("use", "avoid"))


@dataclass
class ContextFile:
    path: str
    layer: str
    kind: str
    text: str

    @property
    def est_tokens(self) -> int:
        return int(len(self.text) / CHARS_PER_TOKEN)

    @property
    def billed_always(self) -> int:
        """Skill bodies are lazy; their description block is not."""
        if self.layer == "always":
            return self.est_tokens
        if self.layer == "trigger":
            return int(len(_frontmatter(self.text)) / CHARS_PER_TOKEN)
        return 0


def _frontmatter(text: str) -> str:
    """SKILL.md frontmatter (name + description) is what's always loaded."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            return text[: end + 3]
    return text[:400]


def normalize_rule(line: str) -> str:
    line = _LIST.sub("", line)
    line = _PUNCT.sub(" ", line.lower())
    return _WS.sub(" ", line).strip()


def discover(root: Path) -> list:
    root = Path(root).resolve()
    seen, out = set(), []
    for pattern, layer, kind in CONTEXT_SOURCES:
        for p in sorted(root.glob(pattern)):
            if not p.is_file() or p in seen:
                continue
            if any(part in (".git", "node_modules", ".venv") for part in p.parts):
                continue
            seen.add(p)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append(ContextFile(str(p.relative_to(root)), layer, kind, text))
    return out


def find_duplicate_rules(files: list, min_words: int = 4):
    """Imperative lines appearing in more than one context file."""
    index = defaultdict(set)
    original = {}
    for f in files:
        for raw in f.text.splitlines():
            if not IMPERATIVE.match(raw):
                continue
            norm = normalize_rule(raw)
            if len(norm.split()) < min_words:
                continue
            index[norm].add(f.path)
            original.setdefault(norm, raw.strip())
    dups = [(original[k], sorted(v)) for k, v in index.items() if len(v) > 1]
    return sorted(dups, key=lambda x: -len(x[1]))


def find_contradictions(files: list):
    """Same predicate asserted both positively and negatively.

    MEASURED LIMITATION -- this has high precision and very low recall. It
    fires only when two rules share a normalized predicate and one carries a
    literal negator:

        "Always use tabs"  vs  "Never use tabs"        -> caught
        "Use tabs"         vs  "Use spaces"            -> MISSED (no negator)
        "Always use pnpm"  vs  "Never use anything
                                but npm"               -> MISSED (paraphrase)

    Real contradictions in real rule files are usually the second and third
    kinds, so expect this to stay silent most of the time. Catching those needs
    embeddings or a model pass, which would break the zero-API-call property
    that makes the rest of this module free. Treat a hit as a real finding and
    silence as no information.
    """
    by_subject = defaultdict(list)
    for f in files:
        for raw in f.text.splitlines():
            if not IMPERATIVE.match(raw):
                continue
            norm = normalize_rule(raw)
            words = norm.split()
            if len(words) < 4:
                continue
            subject = " ".join(w for w in words if w not in
                               {"always", "never", "not", "do", "must", "should", "avoid"})
            if subject:
                by_subject[subject].append((norm, f.path, raw.strip()))
    out = []
    for subject, entries in by_subject.items():
        if len(entries) < 2:
            continue
        pos = [e for e in entries if not any(n in e[0] for _, n in NEGATORS)]
        neg = [e for e in entries if any(n in e[0] for _, n in NEGATORS)]
        if pos and neg:
            out.append((pos[0], neg[0]))
    return out


def render(root: Path) -> str:
    files = discover(root)
    if not files:
        return "No agent context files found (CLAUDE.md, AGENTS.md, skills, cursor rules)."

    always = sum(f.billed_always for f in files)
    lazy = sum(f.est_tokens - f.billed_always for f in files)

    L = ["", "ALWAYS-ON CONTEXT BUDGET", "-" * 68]
    for f in sorted(files, key=lambda x: -x.billed_always):
        note = "" if f.layer == "always" else f"  ({f.layer})"
        p = f.path if len(f.path) <= 42 else "…" + f.path[-41:]
        L.append(f"  {p:<44}{f.billed_always:>7} tok{note}")
    L += [
        "  " + "-" * 66,
        f"  {'always-on total':<44}{always:>7} tok  paid EVERY turn",
        f"  {'lazy (skill bodies, commands)':<44}{lazy:>7} tok  paid on use",
    ]

    dups = find_duplicate_rules(files)
    L += ["", "DUPLICATED RULES  (same instruction in multiple files)", "-" * 68]
    if dups:
        waste = 0
        for rule, paths in dups[:12]:
            r = rule if len(rule) <= 58 else rule[:57] + "…"
            L.append(f"  {r}")
            L.append(f"      in {len(paths)}: {', '.join(paths[:3])}"
                     + (" …" if len(paths) > 3 else ""))
            waste += int(len(rule) / CHARS_PER_TOKEN) * (len(paths) - 1)
        L.append("")
        L.append(f"  ~{waste} tokens of redundant copies. Deleting duplicates is")
        L.append("  free -- it needs no experiment, only a decision about which")
        L.append("  file owns each rule.")
    else:
        L.append("  none found")

    parsed = rl.collect_rules(files, IMPERATIVE)
    near = rl.find_duplicates(parsed)
    if near:
        L += ["", "NEAR-DUPLICATE RULES  (paraphrase, structural match)", "-" * 68]
        for score, r1, r2 in near[:8]:
            L.append(f"  {score:.0%}  {r1.raw[:56]}   [{r1.path}]")
            L.append(f"       {r2.raw[:56]}   [{r2.path}]")
            L.append("")

    conflicts = rl.find_conflicts(parsed)
    if conflicts:
        L += ["", "CONFLICTING RULES", "-" * 68]
        for kind, r1, r2 in conflicts[:8]:
            L.append(f"  [{kind}]")
            L.append(f"    {r1.raw[:58]}   [{r1.path}]")
            L.append(f"    {r2.raw[:58]}   [{r2.path}]")
            L.append("")

    L += [
        "",
        "  Next, cheapest first:",
        "   1. delete duplicates            (no runs needed)",
        "   2. move rarely-needed sections into skills so they load on demand",
        "   3. bisect what remains          (halve, test, recurse)",
        "   4. `sieve ablate <file>` on whatever survives",
        "",
    ]
    return "\n".join(L)
