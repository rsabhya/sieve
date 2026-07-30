"""Structural comparison of imperative rules. No model, no download, no API.

Why not embeddings: sentence vectors put "always use pnpm" and "always use
npm" at ~0.95 cosine similarity. They are near-identical as language and
opposite as instructions. Embeddings compress precisely the token that carries
the meaning, so they are simultaneously too loose (false duplicates) and blind
to real conflicts.

Rules have a rigid shape -- [modality] [verb] [object] [qualifier] -- and the
discriminating content is the object. Parsing that shape directly is both
cheaper and more accurate than semantic similarity for this narrow domain.

Two mechanisms:

  duplicate   same polarity, high content-token overlap (Jaccard)
  conflict    same predicate with opposite polarity, OR same polarity applied
              to two members of a mutually exclusive set (tabs vs spaces)

The exclusive sets are the part that catches what the negator heuristic
missed. It is a curated list, which means it is incomplete by construction --
but it is also transparent, extendable in one line, and never hallucinates.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

NEGATORS = {
    "never", "not", "dont", "don't", "avoid", "no", "without", "except", "instead",
}
MODALS = {
    "always", "must", "should", "shall", "need", "needs", "required", "ensure",
    "make", "sure", "please", "remember", "do", "does", "will", "can", "may",
}
STOP = MODALS | NEGATORS | {
    "the", "a", "an", "to", "of", "in", "on", "at", "for", "with", "from", "by",
    "is", "are", "be", "been", "it", "this", "that", "these", "those", "and",
    "or", "but", "if", "when", "while", "as", "than", "then",
    "you", "your", "we", "our", "us", "only", "just", "also", "very", "more",
}
# NOTE: "any" and "all" are deliberately NOT stopwords. In English they are
# filler; in a TypeScript rule ("never use any") the word is the entire object.

# Keeps paths and versions intact (src/foo.py, node-18) while allowing edge
# punctuation to be trimmed afterwards -- otherwise "committing." and
# "committing" are different tokens and every paraphrase match fails.
_TOKEN = re.compile(r"[a-z0-9_./-]+")
_EDGE = re.compile(r"^[./-]+|[./-]+$")

# Members of one set are alternatives to each other. Asserting two different
# members with the same polarity is a conflict.
EXCLUSIVE_SETS = [
    {"tabs", "tab", "spaces", "space"},
    {"npm", "pnpm", "yarn", "bun"},
    {"jest", "vitest", "mocha", "jasmine"},
    {"rebase", "merge"},
    {"webpack", "vite", "rollup", "esbuild", "parcel"},
    {"eslint", "biome", "standard"},
    {"prettier", "biome"},
    {"default", "named"},
    {"javascript", "typescript"},
    {"yaml", "json", "toml"},
    {"pip", "poetry", "uv", "pipenv", "conda"},
    {"pytest", "unittest", "nose"},
    {"black", "ruff", "autopep8"},
    {"single", "double"},
    {"camelcase", "snake_case", "kebab-case"},
    {"async", "sync"},
    {"sql", "orm"},
]


@dataclass(frozen=True)
class Rule:
    raw: str
    path: str
    polarity: int          # +1 assert, -1 prohibit
    tokens: frozenset      # content tokens, stopwords removed

    def exclusive_members(self):
        out = []
        for i, group in enumerate(EXCLUSIVE_SETS):
            hit = self.tokens & group
            if hit:
                out.append((i, frozenset(hit)))
        return out


def parse_rule(line: str, path: str):
    toks = [_EDGE.sub("", t) for t in _TOKEN.findall(line.lower())]
    toks = [t for t in toks if t]
    if not toks:
        return None
    polarity = -1 if (set(toks) & NEGATORS) else 1
    content = frozenset(t for t in toks if t not in STOP and len(t) > 1)
    if len(content) < 2:
        return None
    return Rule(raw=line.strip(), path=path, polarity=polarity, tokens=content)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicates(rules, threshold: float = 0.6):
    """Same polarity, overlapping content. Catches paraphrase without a model."""
    out = []
    for i, r1 in enumerate(rules):
        for r2 in rules[i + 1:]:
            if r1.path == r2.path or r1.polarity != r2.polarity:
                continue
            # Different members of an exclusive set are NOT duplicates even
            # when the rest of the sentence matches -- that is the pnpm/npm trap.
            if _exclusive_conflict(r1, r2):
                continue
            score = jaccard(r1.tokens, r2.tokens)
            if score >= threshold:
                out.append((score, r1, r2))
    return sorted(out, key=lambda x: -x[0])


def _exclusive_conflict(r1: Rule, r2: Rule):
    for gi1, m1 in r1.exclusive_members():
        for gi2, m2 in r2.exclusive_members():
            if gi1 == gi2 and m1 != m2:
                return (gi1, m1, m2)
    return None


def find_conflicts(rules, threshold: float = 0.5):
    """Two shapes of disagreement.

    opposed  -- same predicate, opposite polarity ("always X" vs "never X")
    exclusive-- same polarity, incompatible objects ("use tabs" vs "use spaces")
    """
    out = []
    for i, r1 in enumerate(rules):
        for r2 in rules[i + 1:]:
            if r1.path == r2.path:
                continue
            ex = _exclusive_conflict(r1, r2)
            if ex and r1.polarity == r2.polarity:
                rest1 = r1.tokens - EXCLUSIVE_SETS[ex[0]]
                rest2 = r2.tokens - EXCLUSIVE_SETS[ex[0]]
                if jaccard(rest1, rest2) >= 0.3 or not (rest1 and rest2):
                    out.append(("exclusive", r1, r2))
                continue
            if r1.polarity != r2.polarity:
                if jaccard(r1.tokens, r2.tokens) >= threshold:
                    out.append(("opposed", r1, r2))
    return out


def collect_rules(files, imperative_re):
    rules = []
    for f in files:
        for line in f.text.splitlines():
            if not imperative_re.match(line):
                continue
            r = parse_rule(line, f.path)
            if r:
                rules.append(r)
    return rules
