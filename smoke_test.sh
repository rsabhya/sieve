#!/usr/bin/env bash
# Verify the whole chain -- proxy, interception, matching, report -- with no
# agent and no API key. curl stands in for the agent; a 401 from the API is
# fine, because sieve records the request on its way out, not the response.
set -u

PORT="${SIEVE_PORT:-8899}"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
TMP="$(mktemp -d)"
PROXY_PID=""
# `kill %1` needs job control, which is off in non-interactive scripts --
# that leaked a mitmdump holding the port. Track the real PID instead.
cleanup() {
    [ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null
    [ -n "$PROXY_PID" ] && pkill -P "$PROXY_PID" 2>/dev/null
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

command -v sieve    >/dev/null || { echo "FAIL: sieve not on PATH (activate your venv)"; exit 1; }
command -v mitmdump >/dev/null || { echo "FAIL: mitmdump not on PATH (pip install mitmproxy)"; exit 1; }

if lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "FAIL: port $PORT already in use. Free it with: lsof -ti:$PORT | xargs kill"
    exit 1
fi

mkdir -p "$TMP/src"
cat > "$TMP/src/very_secret.py" <<'EOF'
DECAY_HALFLIFE_SECONDS = 41.5
MIN_ALLOWANCE = 0.0035

def allowance(events, penalty_bps):
    total = sum(e.weight * e.success_rate for e in events)
    return max(total * (1 - penalty_bps / 10000), MIN_ALLOWANCE)
EOF

cd "$TMP" || exit 1
echo "1/5 indexing..."
sieve init >/dev/null || { echo "FAIL: sieve init"; exit 1; }

echo "2/5 starting proxy..."
sieve proxy --port "$PORT" >/dev/null 2>&1 &
PROXY_PID=$!
sleep 6
[ -f "$CA" ] || { echo "FAIL: no CA at $CA"; exit 1; }

echo "3/5 building a request that carries the secret file..."
python3 - "$TMP" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
src = (root / "src/very_secret.py").read_text()
numbered = "\n".join(f"{i:6d}\t{l}" for i, l in enumerate(src.splitlines(), 1))
body = {"model": "claude-sonnet-4-6", "max_tokens": 16, "messages": [
    {"role": "user", "content": "why is scoring wrong"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Read",
         "input": {"file_path": "src/very_secret.py"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": numbered}]}]}]}
(root / "payload.json").write_text(json.dumps(body))
PY

echo "4/5 sending through the proxy..."
curl -s -o /dev/null --max-time 25 \
  --proxy "http://127.0.0.1:$PORT" --cacert "$CA" \
  -X POST https://api.anthropic.com/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: not-a-real-key" \
  -H "anthropic-version: 2023-06-01" \
  --data @payload.json
sleep 2

echo "5/5 report:"
echo "----------------------------------------------------------------"
sieve report
echo "----------------------------------------------------------------"

if sieve report 2>/dev/null | grep -q "very_secret.py"; then
    echo "PASS - the file was detected in outbound traffic."
    echo "Your setup works. Point it at a real agent now."
else
    echo "FAIL - request not recorded or not matched."
    echo "Most likely: proxy did not start, or curl could not reach the API."
fi
