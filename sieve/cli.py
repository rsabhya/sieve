"""sieve — see what your coding agent actually sends."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import ablate
from . import context as ctx
from . import index as ix
from . import report, store

PROXY_PORT_DEFAULT = 8899
CA_PEM = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def _root(args) -> Path:
    return Path(getattr(args, "root", None) or os.getcwd()).resolve()


def cmd_init(args):
    root = _root(args)
    print(f"indexing {root} ...")
    t0 = time.time()
    entries, stats = ix.build(root)
    conn = store.connect(root)
    n_files, n_sh = store.replace_index(conn, entries)
    conn.close()
    print(
        f"  {stats['files_seen']} text files scanned\n"
        f"  {n_files} indexed ({stats['files_all_boilerplate']} skipped as all-boilerplate)\n"
        f"  {n_sh} discriminating shingles "
        f"({stats['shingles_dropped_common']} common ones dropped)\n"
        f"  {time.time() - t0:.1f}s -> {store.db_path(root)}"
    )
    if n_files == 0:
        print("\nNothing indexed. Is this a source repo?", file=sys.stderr)
        return 1
    return 0


def _ensure_ca(port: int) -> bool:
    if CA_PEM.exists():
        return True
    print("generating mitmproxy CA (first run only) ...")
    p = subprocess.Popen(
        ["mitmdump", "--listen-port", str(port), "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        time.sleep(0.2)
        if CA_PEM.exists():
            break
    p.send_signal(signal.SIGINT)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    return CA_PEM.exists()


def cmd_watch(args):
    root = _root(args)
    if not store.db_path(root).exists():
        print("No index. Run `sieve init` first.", file=sys.stderr)
        return 1
    if not shutil.which("mitmdump"):
        print("mitmdump not found. pip install mitmproxy", file=sys.stderr)
        return 1
    if not args.command:
        print("Nothing to run. Usage: sieve watch -- claude", file=sys.stderr)
        return 1
    if not _ensure_ca(args.port):
        print("Could not generate the mitmproxy CA cert.", file=sys.stderr)
        return 1

    addon = str(Path(__file__).resolve().parent / "addon.py")
    env = dict(os.environ, SIEVE_ROOT=str(root))
    if args.hosts:
        env["SIEVE_HOSTS"] = args.hosts

    # Interactive agents (Claude Code, Codex) draw a full-screen TUI. Anything
    # else writing to the same terminal lands on top of their rendering and
    # corrupts it -- the proxy's output has to go to a file, not the tty.
    log_path = root / ".sieve" / "proxy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if args.verbose:
        log_file = None
        proxy_out = None
    else:
        log_file = open(log_path, "a")
        proxy_out = log_file

    proxy = subprocess.Popen(
        ["mitmdump", "--listen-port", str(args.port), "-s", addon, "-q",
         "--set", "flow_detail=0"],
        env=env,
        stdout=proxy_out,
        stderr=subprocess.STDOUT if proxy_out else None,
    )
    time.sleep(2.0)
    if proxy.poll() is not None:
        print("proxy failed to start", file=sys.stderr)
        if log_file:
            log_file.close()
            print(f"see {log_path}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}"
    child_env = dict(
        os.environ,
        HTTP_PROXY=url, HTTPS_PROXY=url, http_proxy=url, https_proxy=url,
        NODE_EXTRA_CA_CERTS=str(CA_PEM),      # Node agents (Claude Code, Codex)
        SSL_CERT_FILE=str(CA_PEM),            # Python / curl
        REQUESTS_CA_BUNDLE=str(CA_PEM),
        CURL_CA_BUNDLE=str(CA_PEM),
        NO_PROXY="localhost,127.0.0.1",
    )

    print(f"[sieve] proxy on {url}  ·  log: {log_path}")
    print(f"[sieve] running: {' '.join(args.command)}")
    print("[sieve] live view in another terminal:  tail -f "
          f"{log_path}\n")
    rc = 0
    try:
        rc = subprocess.call(args.command, env=child_env)
    except KeyboardInterrupt:
        rc = 130
    except FileNotFoundError:
        print(f"command not found: {args.command[0]}", file=sys.stderr)
        rc = 127
    finally:
        proxy.send_signal(signal.SIGINT)
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        if log_file:
            log_file.close()

    print("\n[sieve] session ended. `sieve report` to see what left.")
    return rc


def cmd_proxy(args):
    """Standalone proxy for agents that aren't a CLI you can wrap.

    Copilot, Cursor's built-in agent, and JetBrains plugins all live inside an
    editor process that sieve did not launch, so there is nothing to inject env
    vars into. Run the proxy on its own and point the editor at it.
    """
    root = _root(args)
    if not store.db_path(root).exists():
        print("No index. Run `sieve init` first.", file=sys.stderr)
        return 1
    if not shutil.which("mitmdump"):
        print("mitmdump not found. pip install mitmproxy", file=sys.stderr)
        return 1
    if not _ensure_ca(args.port):
        print("Could not generate the mitmproxy CA cert.", file=sys.stderr)
        return 1

    addon = str(Path(__file__).resolve().parent / "addon.py")
    env = dict(os.environ, SIEVE_ROOT=str(root))
    if args.hosts:
        env["SIEVE_HOSTS"] = args.hosts

    print(f"""
[sieve] proxy listening on 127.0.0.1:{args.port}
[sieve] watching {root}

Point your editor at it, then restart the editor.

  VS Code / Copilot — settings.json:
    "http.proxy": "http://127.0.0.1:{args.port}",
    "http.proxySupport": "on"
  and set NODE_EXTRA_CA_CERTS={CA_PEM} in the environment
  VS Code is launched from (not in settings.json).

  JetBrains:
    Settings > Appearance & Behavior > System Settings > HTTP Proxy
    Manual: 127.0.0.1 : {args.port}
    Then trust {CA_PEM} in Server Certificates.

  Anything else that honours env vars:
    export HTTPS_PROXY=http://127.0.0.1:{args.port}
    export NODE_EXTRA_CA_CERTS={CA_PEM}

Ctrl-C to stop, then `sieve report`.
""")
    try:
        return subprocess.call(
            ["mitmdump", "--listen-port", str(args.port), "-s", addon, "-q",
             "--set", "flow_detail=0"],
            env=env,
        )
    except KeyboardInterrupt:
        return 0


def cmd_report(args):
    root = _root(args)
    if not store.db_path(root).exists():
        print("No data. Run `sieve init` then `sieve watch -- <agent>`.", file=sys.stderr)
        return 1
    conn = store.connect(root)
    if args.session:
        print(report.render_session(conn, args.session, show_all=True))
    else:
        print(report.render_overview(conn, limit=args.limit, detail=args.all))
    conn.close()
    return 0


def cmd_ablate(args):
    target = Path(args.file)
    if not target.exists():
        print(f"not found: {target}", file=sys.stderr)
        return 1
    print(ablate.render_plan(ablate.plan(target), args.runs, args.tasks))
    return 0


def cmd_context(args):
    print(ctx.render(_root(args)))
    return 0


def cmd_stats(args):
    root = _root(args)
    if not store.db_path(root).exists():
        print("No data yet.", file=sys.stderr)
        return 1
    conn = store.connect(root)
    print(report.render_stats(conn))
    conn.close()
    return 0


def cmd_status(args):
    root = _root(args)
    if not store.db_path(root).exists():
        print("not initialized")
        return 1
    conn = store.connect(root)
    f = conn.execute("SELECT COUNT(*), SUM(n_shingle) FROM files").fetchone()
    s = conn.execute("SELECT COUNT(*), SUM(n_requests), SUM(bytes_sent) FROM sessions").fetchone()
    print(f"root      {root}")
    print(f"indexed   {f[0]} files, {f[1] or 0} shingles")
    print(f"recorded  {s[0]} sessions, {s[1] or 0} requests, {(s[2] or 0)/1e6:.1f} MB")
    conn.close()
    return 0


def cmd_reset(args):
    root = _root(args)
    conn = store.connect(root)
    for t in ("detections", "usage", "requests", "sessions"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    print("cleared recorded sessions (index kept)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sieve", description=__doc__)
    ap.add_argument("--root", help="repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="build the content index").set_defaults(fn=cmd_init)

    w = sub.add_parser("watch", help="run an agent behind the recording proxy")
    w.add_argument("--port", type=int, default=PROXY_PORT_DEFAULT)
    w.add_argument("--hosts", help="comma-separated hosts to watch")
    w.add_argument("--verbose", action="store_true",
                   help="print proxy output to the terminal (corrupts TUI agents)")
    w.add_argument("command", nargs=argparse.REMAINDER)
    w.set_defaults(fn=cmd_watch)

    p = sub.add_parser("proxy", help="standalone proxy for editor-based agents")
    p.add_argument("--port", type=int, default=PROXY_PORT_DEFAULT)
    p.add_argument("--hosts", help="comma-separated hosts to watch")
    p.set_defaults(fn=cmd_proxy)

    r = sub.add_parser("report", help="show what was sent")
    r.add_argument("-s", "--session", help="one session id")
    r.add_argument("-n", "--limit", type=int, default=20)
    r.add_argument("-a", "--all", action="store_true", help="include used files")
    r.set_defaults(fn=cmd_report)

    ab = sub.add_parser("ablate", help="plan an ablation of a context file")
    ab.add_argument("file", help="CLAUDE.md, a skill file, or any rules file")
    ab.add_argument("--runs", type=int, default=3, help="runs per condition")
    ab.add_argument("--tasks", type=int, default=5, help="tasks in your suite")
    ab.set_defaults(fn=cmd_ablate)

    sub.add_parser("context", help="static audit of all agent context files").set_defaults(fn=cmd_context)
    sub.add_parser("stats", help="cross-session token and precision aggregates").set_defaults(fn=cmd_stats)
    sub.add_parser("status", help="index and capture summary").set_defaults(fn=cmd_status)
    sub.add_parser("reset", help="clear recorded sessions").set_defaults(fn=cmd_reset)

    args = ap.parse_args(argv)
    if args.cmd == "watch" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
