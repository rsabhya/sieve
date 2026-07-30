"""The report. This is the whole point of the tool.

The column that matters is `used` — a file that was transmitted repeatedly and
never edited or referenced is a file the agent did not need. That is the
incidental leak, made visible.
"""

import time


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _fmt_dur(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


def sessions(conn, limit=20):
    return conn.execute(
        """SELECT id, started_at, last_seen, endpoint, n_requests, bytes_sent
           FROM sessions ORDER BY last_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def session_detail(conn, sid):
    rows = conn.execute(
        """SELECT file_path,
                  COUNT(*)        AS times,
                  MAX(matched)    AS best,
                  MAX(total)      AS total
           FROM detections WHERE session_id=?
           GROUP BY file_path ORDER BY times DESC, file_path""",
        (sid,),
    ).fetchall()
    used = {}
    for path, kind in conn.execute(
        "SELECT file_path, kind FROM usage WHERE session_id=?", (sid,)
    ):
        used.setdefault(path, set()).add(kind)
    return rows, used


def render_session(conn, sid, row=None, show_all=True):
    if row is None:
        row = conn.execute(
            """SELECT id, started_at, last_seen, endpoint, n_requests, bytes_sent
               FROM sessions WHERE id=?""",
            (sid,),
        ).fetchone()
    if row is None:
        return f"no session {sid}"

    _, started, last, endpoint, nreq, nbytes = row
    rows, used = session_detail(conn, sid)

    lines = []
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    lines.append(
        f"  Session {sid} · {when} · {_fmt_dur(last - started)} · {nreq} requests"
    )
    lines.append(
        f"  {_fmt_bytes(nbytes)} sent · {len(rows)} distinct files · {endpoint or '?'}"
    )
    lines.append("")

    if not rows:
        lines.append("  (no indexed file content detected in outbound requests)")
        return "\n".join(lines)

    unused = [r for r in rows if r[0] not in used]
    seen_used = [r for r in rows if r[0] in used]

    width = max(len(r[0]) for r in rows)
    width = min(max(width, 24), 52)

    def line(r, flag):
        path, times, best, total = r
        p = path if len(path) <= width else "…" + path[-(width - 1):]
        note = (
            ", ".join(sorted(used[path])) if path in used
            else "no observable use"
        )
        cov = f"{best}/{total}"
        return f"  {flag} {p:<{width}}  sent {times:>3}× · {cov:>7}  {note}"

    for r in unused:
        lines.append(line(r, "!"))
    if seen_used and show_all:
        for r in seen_used:
            lines.append(line(r, " "))

    lines.append("")
    lines.append(
        f"  {len(unused)} of {len(rows)} files transmitted with no sign of being used."
    )
    return "\n".join(lines)


SIGNALS = ("edited", "echoed", "mentioned")
SIGNAL_MARK = {"edited": "E", "echoed": "C", "mentioned": "~"}


def _signal_cell(kinds) -> str:
    """Three fixed columns so files line up and stay scannable.

    Collapsing these into one 'used' boolean threw away the only thing that
    mattered: an edit is proof, an echo is evidence, a path mention is barely
    a hint. They should never have shared a column.
    """
    return "".join(SIGNAL_MARK[s] if s in kinds else "." for s in SIGNALS)


def render_stats(conn):
    """Cross-session aggregates. This is the view that matters over weeks."""
    tot = conn.execute(
        """SELECT COUNT(*), SUM(body_bytes), SUM(in_tok), SUM(out_tok),
                  SUM(cache_w), SUM(cache_r)
           FROM requests"""
    ).fetchone()
    n_req, body_b, in_t, out_t, cw, cr = (x or 0 for x in tot)
    if not n_req:
        return "No requests recorded yet."

    L = ["", "TOKENS", "-" * 68]
    if in_t or cw or cr:
        billed = in_t + 1.25 * cw + 0.10 * cr
        L.append(f"  uncached input     {in_t:>12,}")
        L.append(f"  cache writes       {cw:>12,}  (billed 1.25x)")
        L.append(f"  cache reads        {cr:>12,}  (billed 0.10x)")
        L.append(f"  output             {out_t:>12,}")
        L.append(f"  input-equivalent   {billed:>12,.0f}  after cache multipliers")
        if cw + cr:
            L.append(f"  cache hit rate     {100 * cr / (cw + cr):>11.1f}%")
    else:
        L.append("  (no token data -- responses not captured)")

    by_signal = dict(
        conn.execute("SELECT kind, COUNT(DISTINCT file_path) FROM usage GROUP BY kind")
    )
    sent = conn.execute("SELECT COUNT(DISTINCT file_path) FROM detections").fetchone()[0]
    any_sig = conn.execute("SELECT COUNT(DISTINCT file_path) FROM usage").fetchone()[0]

    L += ["", "EVIDENCE OF USE", "-" * 68]
    L.append(f"  files sent                             {sent:>5}")
    L.append("")
    L.append(f"  E  edited     edit tool named it       {by_signal.get('edited', 0):>5}   proof")
    L.append(f"  C  echoed     content in output        {by_signal.get('echoed', 0):>5}   evidence")
    L.append(f"  ~  mentioned  path in output text      {by_signal.get('mentioned', 0):>5}   weak, false positives")
    L.append(f"  .  no signal                           {sent - any_sig:>5}   unknown, not proof of non-use")

    L += ["", "MOST-TRANSMITTED FILES  (across all sessions)", "-" * 68]
    top = conn.execute(
        """SELECT file_path, COUNT(*) AS sends, COUNT(DISTINCT session_id) AS sessions
           FROM detections GROUP BY file_path
           ORDER BY sends DESC LIMIT 15"""
    ).fetchall()
    sig_map = {}
    for path, kind in conn.execute("SELECT file_path, kind FROM usage"):
        sig_map.setdefault(path, set()).add(kind)

    w = min(max((len(r[0]) for r in top), default=20), 44)
    L.append(f"  {'EC~':<5}{'file':<{w}}{'sends':>7}{'sessions':>10}")
    for path, sends, sess in top:
        p = path if len(path) <= w else "…" + path[-(w - 1):]
        L.append(f"  {_signal_cell(sig_map.get(path, set())):<5}{p:<{w}}{sends:>7}{sess:>10}")

    L += [
        "",
        "  E = edited (proof)   C = echoed (evidence)   ~ = mentioned (weak)",
        "  '...' means no signal fired. That is NOT proof the file was unused.",
        "  Always-on context like CLAUDE.md is invisible to all three signals",
        "  by construction and will always show '...'.",
        "",
    ]
    return "\n".join(L)


def render_overview(conn, limit=20, detail=False):
    ss = sessions(conn, limit)
    if not ss:
        return (
            "No sessions recorded yet.\n"
            "Run your agent under `sieve watch -- <command>` first."
        )
    out = []
    for row in ss:
        out.append(render_session(conn, row[0], row=row, show_all=detail))
        out.append("")
    tot_req = sum(r[4] for r in ss)
    tot_b = sum(r[5] for r in ss)
    out.append(f"{len(ss)} sessions · {tot_req} requests · {_fmt_bytes(tot_b)} total")
    return "\n".join(out)
