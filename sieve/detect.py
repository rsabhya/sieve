"""Match outbound request content against the repo index."""

from collections import Counter, defaultdict
from dataclasses import dataclass

from .fingerprint import shingle_text


@dataclass
class Detection:
    path: str
    matched: int
    total: int
    via: str

    @property
    def ratio(self) -> float:
        return self.matched / self.total if self.total else 0.0


def _threshold(total: int) -> int:
    """Small files need one hit; normal files need three.

    A single shingle surviving the boilerplate filter and colliding by chance
    is rare but not impossible. Three is enough to make coincidence unlikely
    without missing partial reads.
    """
    return 1 if total <= 3 else 3


def detect(strings, lookup) -> list[Detection]:
    """strings: [(key_trail, text)] from parse.extract"""
    hits: dict[str, set[str]] = defaultdict(set)
    totals: dict[str, int] = {}
    trails: dict[str, Counter] = defaultdict(Counter)

    for trail, text in strings:
        for h in shingle_text(text):
            for path, n_total in lookup.get(h, ()):
                hits[path].add(h)
                totals[path] = n_total
                trails[path][trail] += 1

    out = []
    for path, hs in hits.items():
        total = totals[path]
        if len(hs) >= _threshold(total):
            via = trails[path].most_common(1)[0][0] if trails[path] else "?"
            out.append(Detection(path, len(hs), total, via))
    out.sort(key=lambda d: (-d.matched, d.path))
    return out
