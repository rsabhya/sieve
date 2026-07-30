"""mitmproxy addon. Loaded by `sieve watch`; not run directly.

Read-only by design. It never modifies a request. If this addon throws, the
agent should keep working — every hook is wrapped, because a monitoring tool
that breaks your editor gets uninstalled the same day.
"""

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sieve import index as ix
from sieve import parse, store
from sieve.detect import detect

WATCHED_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "api.x.ai",
    "openrouter.ai",
    "api.deepseek.com",
    "api.mistral.ai",
    # GitHub Copilot: chat goes to the first, older completions via the second.
    "api.githubcopilot.com",
    "copilot-proxy.githubusercontent.com",
)
WATCHED_PATH_HINTS = (
    "/messages",
    "/completions",   # covers /chat/completions and Copilot ghost-text
    "/responses",
    "/generateContent",
)


class Sieve:
    def __init__(self):
        self.root = Path(os.environ.get("SIEVE_ROOT", os.getcwd())).resolve()
        extra = os.environ.get("SIEVE_HOSTS", "")
        self.hosts = tuple(h.strip() for h in extra.split(",") if h.strip()) or WATCHED_HOSTS
        self.conn = store.connect(self.root)
        self.lookup = ix.load_lookup(self.conn)
        self.known = {r[0] for r in self.conn.execute("SELECT path FROM files")}
        self.seen = 0
        self.errors = 0
        n = sum(len(v) for v in self.lookup.values())
        print(
            f"[sieve] watching {self.root}  ({len(self.known)} files, {n} shingles)",
            file=sys.stderr,
        )
        if not self.known:
            print("[sieve] index is empty — run `sieve init` first", file=sys.stderr)

    def _watched(self, flow) -> bool:
        host = flow.request.pretty_host or ""
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return False
        return any(hint in flow.request.path for hint in WATCHED_PATH_HINTS)

    def request(self, flow):
        try:
            self._handle(flow)
        except Exception:
            self.errors += 1
            if self.errors <= 3:
                traceback.print_exc(file=sys.stderr)

    def _handle(self, flow):
        if not self._watched(flow):
            return
        raw = flow.request.raw_content or b""
        if not raw:
            return
        body = parse.parse_body(flow.request.content or raw)
        if body is None:
            return

        sid = parse.session_id(body)
        strings, tool_paths = parse.extract(body)
        dets = detect(strings, self.lookup)
        atext = parse.assistant_text_of(body)
        evidence = parse.usage_evidence(tool_paths, self.known, atext)

        rid = store.record_request(
            self.conn, sid, flow.request.pretty_host, flow.request.path,
            len(raw), flow.request.pretty_host,
        )
        store.record_detections(self.conn, rid, sid, dets)
        store.record_usage(self.conn, sid, evidence)
        self.conn.commit()

        self.seen += 1
        flow.metadata["sieve_rid"] = rid
        flow.metadata["sieve_sid"] = sid
        if dets:
            top = ", ".join(f"{d.path}" for d in dets[:4])
            more = f" +{len(dets) - 4}" if len(dets) > 4 else ""
            print(
                f"[sieve] {sid} req#{self.seen} {len(raw) // 1024}KB -> {top}{more}",
                file=sys.stderr,
            )

    # Cap accumulation so a huge response can't balloon memory. Token usage
    # lives in message_start and message_delta, both well inside this.
    MAX_CAPTURE = 8 * 1024 * 1024

    def responseheaders(self, flow):
        """Pass response bodies through as they arrive instead of buffering.

        mitmproxy buffers the whole response before forwarding by default,
        which destroys streaming completely -- measured 3.01s to first byte
        versus 0.05s direct. The agent looks frozen for the whole generation.

        Setting flow.response.stream to a callable gives us both: each chunk is
        returned unmodified and forwarded immediately, while we keep a copy for
        the token and echo parsing that happens in response().
        """
        try:
            if not self._watched(flow):
                return
            chunks = []
            size = 0

            def collect(data: bytes) -> bytes:
                nonlocal size
                if data:
                    if size < self.MAX_CAPTURE:
                        chunks.append(data)
                        size += len(data)
                else:
                    flow.metadata["sieve_body"] = b"".join(chunks)
                return data

            flow.response.stream = collect
        except Exception:
            self.errors += 1

    def response(self, flow):
        """Token counts and echo evidence both live in the response."""
        try:
            rid = flow.metadata.get("sieve_rid")
            sid = flow.metadata.get("sieve_sid")
            if rid is None:
                return
            # With streaming on, flow.response.content is empty -- use the copy
            # accumulated by the stream callback.
            raw = flow.metadata.get("sieve_body")
            if raw is None:
                raw = flow.response.content or b""
            model, usage = parse.response_usage(raw)
            if usage:
                store.record_usage_tokens(self.conn, rid, model, usage)

            # Content the model reproduced in its own output is the strongest
            # observable evidence a file actually shaped the answer.
            text = parse.response_text(raw)
            if text and sid:
                echoed = detect([("response", text)], self.lookup)
                if echoed:
                    store.record_usage(
                        self.conn, sid, [(d.path, "echoed") for d in echoed]
                    )
            self.conn.commit()
        except Exception:
            self.errors += 1
            if self.errors <= 3:
                traceback.print_exc(file=sys.stderr)

    def done(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


addons = [Sieve()]
