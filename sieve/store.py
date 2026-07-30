"""SQLite persistence. One database per watched repo, at .sieve/sieve.db"""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    size      INTEGER NOT NULL,
    n_shingle INTEGER NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shingles (
    hash    TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_shingles_hash ON shingles(hash);
CREATE INDEX IF NOT EXISTS ix_shingles_file ON shingles(file_id);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    last_seen  REAL NOT NULL,
    endpoint   TEXT,
    n_requests INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    host       TEXT,
    path       TEXT,
    body_bytes INTEGER NOT NULL,
    model      TEXT,
    in_tok     INTEGER,
    out_tok    INTEGER,
    cache_w    INTEGER,
    cache_r    INTEGER
);
CREATE TABLE IF NOT EXISTS detections (
    id         INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    file_path  TEXT NOT NULL,
    matched    INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    via        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_det_session ON detections(session_id);

-- Evidence that a file was actually *used*, not merely transmitted.
CREATE TABLE IF NOT EXISTS usage (
    session_id TEXT NOT NULL,
    file_path  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    UNIQUE(session_id, file_path, kind)
);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does NOT
# add columns to a table that already exists, so an older database silently
# keeps its old shape and then fails at write time with "no such column".
# Every additive schema change must be listed here.
MIGRATIONS = {
    "requests": [
        ("model", "TEXT"),
        ("in_tok", "INTEGER"),
        ("out_tok", "INTEGER"),
        ("cache_w", "INTEGER"),
        ("cache_r", "INTEGER"),
    ],
}


def _migrate(conn):
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # table not created yet; the schema script handles it
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def db_path(root: Path) -> Path:
    return Path(root) / ".sieve" / "sieve.db"


def connect(root: Path) -> sqlite3.Connection:
    p = db_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def replace_index(conn, entries):
    """entries: iterable of (relpath, size, set_of_hashes)"""
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM shingles")
    now = time.time()
    n_files = n_sh = 0
    for path, size, hashes in entries:
        cur = conn.execute(
            "INSERT INTO files(path,size,n_shingle,indexed_at) VALUES (?,?,?,?)",
            (path, size, len(hashes), now),
        )
        fid = cur.lastrowid
        conn.executemany(
            "INSERT INTO shingles(hash,file_id) VALUES (?,?)", ((h, fid) for h in hashes)
        )
        n_files += 1
        n_sh += len(hashes)
    conn.commit()
    return n_files, n_sh


def record_request(conn, session_id, host, path, body_bytes, endpoint_label):
    now = time.time()
    conn.execute(
        """INSERT INTO sessions(id,started_at,last_seen,endpoint,n_requests,bytes_sent)
           VALUES (?,?,?,?,1,?)
           ON CONFLICT(id) DO UPDATE SET
             last_seen=excluded.last_seen,
             n_requests=sessions.n_requests+1,
             bytes_sent=sessions.bytes_sent+excluded.bytes_sent""",
        (session_id, now, now, endpoint_label, body_bytes),
    )
    cur = conn.execute(
        "INSERT INTO requests(session_id,ts,host,path,body_bytes) VALUES (?,?,?,?,?)",
        (session_id, now, host, path, body_bytes),
    )
    return cur.lastrowid


def record_detections(conn, request_id, session_id, dets):
    conn.executemany(
        """INSERT INTO detections(request_id,session_id,file_path,matched,total,via)
           VALUES (?,?,?,?,?,?)""",
        ((request_id, session_id, d.path, d.matched, d.total, d.via) for d in dets),
    )


def record_usage_tokens(conn, request_id, model, usage: dict):
    conn.execute(
        """UPDATE requests SET model=?, in_tok=?, out_tok=?, cache_w=?, cache_r=?
           WHERE id=?""",
        (
            model,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"),
            request_id,
        ),
    )


def record_usage(conn, session_id, pairs):
    conn.executemany(
        "INSERT OR IGNORE INTO usage(session_id,file_path,kind) VALUES (?,?,?)",
        ((session_id, p, k) for p, k in pairs),
    )
