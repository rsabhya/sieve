"""Build the shingle index for a repo.

Two things matter for precision:

1. Skip anything that isn't source-shaped (binaries, lockfiles, vendored deps).
   Lockfiles in particular are enormous and near-identical across projects.

2. Drop boilerplate shingles. A window of four import lines appears in fifty
   files; if we keep it, every request that carries any of those fifty produces
   fifty detections. Any shingle appearing in more than MAX_FILE_SPREAD files
   is discarded as non-discriminating. This is crude IDF and it does most of
   the work of keeping the report readable.
"""

from collections import Counter, defaultdict
from pathlib import Path

from .fingerprint import shingle_text

MAX_BYTES = 2_000_000
MAX_FILE_SPREAD = 3

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".next", ".nuxt", ".cache", "vendor",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".sieve",
    "site-packages", ".gradle", "Pods", ".terraform",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".jar", ".war",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class",
    ".pyc", ".pyo", ".wasm", ".mp4", ".mp3", ".wav", ".mov", ".woff",
    ".woff2", ".ttf", ".eot", ".db", ".sqlite", ".sqlite3", ".parquet",
}
SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "uv.lock",
}


def _is_probably_text(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            chunk = f.read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


def walk_repo(root: Path):
    root = Path(root).resolve()
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES or p.name in SKIP_NAMES:
            continue
        try:
            if p.stat().st_size > MAX_BYTES or p.stat().st_size == 0:
                continue
        except OSError:
            continue
        if not _is_probably_text(p):
            continue
        yield p


def build(root: Path, verbose: bool = False):
    """Returns (entries, stats). entries: list of (relpath, size, hashes)."""
    root = Path(root).resolve()
    raw: dict[str, tuple[int, set[str]]] = {}
    spread: Counter = Counter()

    for p in walk_repo(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hs = shingle_text(text)
        if not hs:
            continue
        rel = str(p.relative_to(root))
        raw[rel] = (p.stat().st_size, hs)
        spread.update(hs)

    common = {h for h, n in spread.items() if n > MAX_FILE_SPREAD}

    entries, dropped_files = [], 0
    for rel, (size, hs) in raw.items():
        keep = hs - common
        if not keep:
            dropped_files += 1
            continue
        entries.append((rel, size, keep))

    stats = {
        "files_seen": len(raw),
        "files_indexed": len(entries),
        "files_all_boilerplate": dropped_files,
        "shingles_total": sum(len(e[2]) for e in entries),
        "shingles_dropped_common": len(common),
    }
    return entries, stats


def load_lookup(conn):
    """hash -> list[(file_path, n_shingle_for_that_file)]"""
    lookup = defaultdict(list)
    rows = conn.execute(
        "SELECT s.hash, f.path, f.n_shingle FROM shingles s JOIN files f ON f.id=s.file_id"
    )
    for h, path, n in rows:
        lookup[h].append((path, n))
    return lookup
