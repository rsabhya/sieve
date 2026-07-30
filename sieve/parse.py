"""Pull file-bearing content out of a model API request body.

Deliberately schema-agnostic. Rather than hardcoding the Anthropic Messages
shape, the OpenAI chat shape, and the OpenAI Responses shape (and then breaking
when a fourth ships), we walk the JSON tree and collect every string long
enough to plausibly be file content, tagged with the key path it came from.

Two extra passes read structure where it exists, because it is much more
reliable than inference when present:

  - tool_use / function_call blocks name the file they are about
  - edit-shaped tool calls are direct evidence the file was *used*
"""

import hashlib
import json

# Minimum string length to consider as possible file content.
#
# This was 120 and it silently missed every small file — short config files and
# prompt fragments are often the most sensitive things in a repo, and a 3-line
# secret serializes to well under 120 bytes. 40 is low enough to catch those
# while still discarding schema noise ("assistant", "claude-sonnet-4-6", "text").
MIN_STR = 40

EDIT_TOOLS = {
    "edit", "write", "multiedit", "notebookedit", "str_replace",
    "str_replace_editor", "create_file", "apply_patch", "edit_file", "write_file",
}
PATH_KEYS = ("file_path", "filepath", "path", "filename", "file", "target_file", "notebook_path")


def parse_body(raw: bytes):
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def session_id(body: dict) -> str:
    """Stable per-conversation id.

    Every turn resends the whole history, so the first user message is a stable
    fingerprint for the conversation. Falls back to the system prompt.

    Known limitation: two sessions started against the same model with a
    byte-identical opening message will merge into one. Acceptable for a
    reporting tool; revisit if it ever gates anything.
    """
    seed = None
    for key in ("messages", "input"):
        seq = body.get(key)
        if isinstance(seq, list) and seq:
            seed = json.dumps(seq[0], sort_keys=True)[:4000]
            break
    if seed is None:
        seed = json.dumps(body.get("system", ""), sort_keys=True)[:4000]
    seed = f"{body.get('model', '?')}|{seed or 'unknown'}"
    return hashlib.blake2b(seed.encode("utf-8", "replace"), digest_size=6).hexdigest()


def _walk(node, trail, out_strings, out_blocks):
    if isinstance(node, dict):
        btype = node.get("type")
        name = node.get("name") or node.get("function", {}).get("name") if isinstance(
            node.get("function"), dict
        ) else node.get("name")
        if btype in ("tool_use", "function_call", "tool_call") or name:
            args = node.get("input") or node.get("arguments") or node.get("parameters")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            if isinstance(args, dict) and isinstance(name, str):
                for pk in PATH_KEYS:
                    v = args.get(pk)
                    if isinstance(v, str) and v.strip():
                        out_blocks.append((name, v.strip()))
                        break
        for k, v in node.items():
            _walk(v, trail + (str(k),), out_strings, out_blocks)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, trail + (str(i),), out_strings, out_blocks)
    elif isinstance(node, str):
        if len(node) >= MIN_STR:
            out_strings.append((".".join(trail[-3:]), node))


def extract(body: dict):
    """Returns (strings, tool_paths).

    strings:    [(key_trail, text)] every long string in the request
    tool_paths: [(tool_name, file_path)] structural file references
    """
    strings, blocks = [], []
    _walk(body, (), strings, blocks)
    return strings, blocks


def response_text(raw: bytes) -> str:
    """Pull the model's own output text out of a response.

    Used to check whether repo content is echoed back — if the model reproduces
    lines from a file in its answer, that file demonstrably shaped the output.
    That is weaker than proving influence, but unlike path-mentions it is real
    evidence rather than a naming convention.
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    out = []

    def absorb(obj):
        if isinstance(obj, dict):
            t = obj.get("type")
            if t == "text" and isinstance(obj.get("text"), str):
                out.append(obj["text"])
            elif t == "content_block_delta":
                d = obj.get("delta") or {}
                for k in ("text", "partial_json"):
                    if isinstance(d.get(k), str):
                        out.append(d[k])
            elif t == "input_json_delta" and isinstance(obj.get("partial_json"), str):
                out.append(obj["partial_json"])
            for key in ("content", "message", "delta", "choices"):
                if key in obj:
                    absorb(obj[key])
        elif isinstance(obj, list):
            for v in obj:
                absorb(v)
        elif isinstance(obj, str):
            out.append(obj)

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            absorb(json.loads(text))
        except ValueError:
            pass
        return "\n".join(out)

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            absorb(json.loads(payload))
        except ValueError:
            continue
    return "\n".join(out)


def response_usage(raw: bytes):
    """Extract (model, usage_dict) from a model API response.

    Handles three shapes:
      - plain JSON with a top-level `usage`
      - Anthropic SSE, where input/cache counts arrive in message_start and the
        final output_tokens arrives in message_delta
      - OpenAI SSE with stream_options.include_usage

    Normalizes OpenAI's field names onto Anthropic's so downstream code has one
    vocabulary. Returns (None, {}) when nothing usable is present.
    """
    if not raw:
        return None, {}
    text = raw.decode("utf-8", "replace")
    model, usage = None, {}

    def absorb(obj):
        nonlocal model, usage
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("model"), str):
            model = obj["model"]
        msg = obj.get("message")
        if isinstance(msg, dict):
            absorb(msg)
        u = obj.get("usage")
        if isinstance(u, dict):
            norm = {
                "input_tokens": u.get("input_tokens", u.get("prompt_tokens")),
                "output_tokens": u.get("output_tokens", u.get("completion_tokens")),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": u.get(
                    "cache_read_input_tokens",
                    (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                ),
            }
            for k, v in norm.items():
                if v is not None:
                    usage[k] = v

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            absorb(json.loads(text))
        except ValueError:
            pass
        return model, usage

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            absorb(json.loads(payload))
        except ValueError:
            continue
    return model, usage


def usage_evidence(tool_paths, known_paths, assistant_text: str):
    """Distinguish 'transmitted' from 'actually used'.

    Two signals, both conservative — they can miss real usage, which biases the
    report toward flagging things as unused. That is the wrong direction for a
    blocking tool and the right one for a report you want people to look at.
    """
    pairs = []
    for tool, path in tool_paths:
        if tool.lower() in EDIT_TOOLS:
            norm = _match_known(path, known_paths)
            if norm:
                pairs.append((norm, "edited"))
    if assistant_text:
        for kp in known_paths:
            if kp in assistant_text:
                pairs.append((kp, "mentioned"))
    return pairs


def _match_known(path: str, known_paths) -> str | None:
    """Map an absolute or partial path onto an indexed relative path."""
    p = path.replace("\\", "/").lstrip("./")
    if p in known_paths:
        return p
    for kp in known_paths:
        if p.endswith(kp) or kp.endswith(p):
            return kp
    return None


def assistant_text_of(body: dict) -> str:
    """Concatenate assistant-authored text so we can look for file mentions."""
    chunks = []
    for key in ("messages", "input"):
        seq = body.get(key)
        if not isinstance(seq, list):
            continue
        for msg in seq:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            c = msg.get("content")
            if isinstance(c, str):
                chunks.append(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        chunks.append(blk.get("text") or "")
    return "\n".join(chunks)
