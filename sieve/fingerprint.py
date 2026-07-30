"""Content fingerprinting.

Files are reduced to a set of shingle hashes: rolling windows of N normalized
lines. Matching on shingles rather than whole-file hashes means we still detect
a file when only part of it was sent, when it was reformatted, or when it
arrived wrapped in tool output decoration.

Normalization is the load-bearing part. The same bytes reach the wire in very
different shapes depending on how the agent read them:

    Read tool      "     1\timport os"        line-number + tab prefix
    cat            "import os"                bare
    grep -n        "src/foo.py:1:import os"   path:line: prefix
    grep -r        "src/foo.py:import os"     path: prefix

All four must normalize to the same thing or matching silently fails.
"""

import hashlib
import re

WINDOW = 4  # lines per shingle
_HASH_BYTES = 8

# "     1\t" or "  12\t" — Read tool output
_LINE_NUM_TAB = re.compile(r"^\s*\d+\t")
# "path/to/file.py:42:" or "path/to/file.py-42-" — grep with line numbers
_GREP_NUM = re.compile(r"^[^\s:]{1,200}[:-]\d+[:-]")
# "path/to/file.py:" — grep without line numbers
_GREP_PATH = re.compile(r"^[\w./\\-]{1,200}\.\w{1,8}:")
# "   1  import os" — cat -n / nl style
_LINE_NUM_SPACE = re.compile(r"^\s{0,6}\d{1,6}\s{2,}")


def normalize_lines(text: str) -> list[str]:
    """Strip tool decoration and whitespace noise. Drop blank lines."""
    out = []
    for raw in text.splitlines():
        line = _LINE_NUM_TAB.sub("", raw)
        if line == raw:
            line = _GREP_NUM.sub("", line)
        if line == raw:
            line = _GREP_PATH.sub("", line)
        if line == raw:
            line = _LINE_NUM_SPACE.sub("", line)
        line = line.strip()
        if line:
            out.append(line)
    return out


def _h(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", "replace"), digest_size=_HASH_BYTES).hexdigest()


def shingles(lines: list[str], window: int = WINDOW) -> set[str]:
    """Hash every sliding window of `window` lines.

    Files shorter than the window collapse to a single hash so that small
    config files and short prompt fragments are still detectable.
    """
    if not lines:
        return set()
    if len(lines) < window:
        return {_h("\n".join(lines))}
    return {_h("\n".join(lines[i : i + window])) for i in range(len(lines) - window + 1)}


def shingle_text(text: str, window: int = WINDOW) -> set[str]:
    return shingles(normalize_lines(text), window)
