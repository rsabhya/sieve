# sieve

**A flight recorder for coding agents.** It doesn't block anything. It tells you which of your files went over the wire, how many times, and whether the agent ever actually used them.

```
  Session ee220890bd92 · 2026-07-28 23:17 · 5 requests
  14.7KB sent · 4 distinct files · api.anthropic.com

  ! src/signals.py                    sent   5× ·   16/16  never referenced in output
  ! prompts/system_v3.md              sent   3× ·     3/3  never referenced in output
  ! tests/fixture_counterparties.csv  sent   2× ·     2/2  never referenced in output
    src/router.py                     sent   4× ·     6/6  edited, mentioned

  3 of 4 files transmitted with no sign of being used.
```

The `!` rows are the point. Those files were transmitted repeatedly and never edited or mentioned — the agent grepped them up, carried them along, and shipped them upstream on every subsequent turn. That's incidental leakage, and right now nothing else shows it to you.

## Install

```bash
pip install -e .
```

## Use

```bash
cd your-repo
sieve init                 # fingerprint the repo
sieve watch -- claude      # CLI agents: wrap the process
sieve proxy                # editor agents: run standalone, point editor at it
sieve stats                # tokens, cost, evidence
sieve report               # per-session detail

# live view while working, second terminal:
#   tail -f .sieve/proxy.log
```

Works with anything that speaks HTTPS to a model endpoint — Claude Code, Codex, Cursor, Aider. `sieve watch` sets `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` for the child process only; nothing else on your system is affected.

## Commands

| Command | What it does | Proxy needed |
|---|---|---|
| `sieve init` | Fingerprint the repo | no |
| `sieve context` | Audit all agent context files: always-on token budget, duplicate and conflicting rules | no |
| `sieve ablate <file>` | Plan an ablation of a context file: sections, weights, run estimate | no |
| `sieve watch -- <cmd>` | Run a CLI agent behind the recording proxy | yes |
| `sieve proxy` | Standalone proxy for editor-embedded agents | yes |
| `sieve report` | Per-session view of what was sent | no |
| `sieve stats` | Cross-session token and evidence aggregates | no |
| `sieve status` | Index and capture summary | no |
| `sieve reset` | Clear recorded sessions, keep the index | no |

`sieve context` needs no network setup at all and is the fastest thing to try first.

## Evidence, not usage

Files are tagged with what was actually observed:

```
EC~  file                                   sends  sessions
E.~  src/core/parser.py                        32         1
.C.  src/core/helpers.py                       22         1
...  CLAUDE.md                                 20         1
..~  README.md                                 19         1
```

`E` edited (proof) · `C` content echoed in output (evidence) · `~` path mentioned (weak) · `.` no signal

A file showing `...` is **unmarked, not unused**. Always-on context like CLAUDE.md is invisible to all three signals by construction.

## Security

**Sieve performs TLS interception.** It generates a local certificate authority via mitmproxy, and the agent process is told to trust it so request bodies can be read.

- The CA private key lives in `~/.mitmproxy/`. Anyone holding it can intercept TLS for any process trusting it. Protect it like an SSH key.
- Trust is scoped to the child process through environment variables. Sieve does not install the CA into your system or browser trust store, and neither should you.
- Nothing leaves your machine. Everything goes to a local `.sieve/sieve.db`. Sieve makes no outbound requests of its own.
- Keep `.sieve/` out of git — it contains file paths and token counts from your sessions.

## Agent compatibility

| Agent | Works | How |
|---|---|---|
| Claude Code | yes | `sieve watch -- claude` |
| Codex CLI, Aider, OpenCode | yes | `sieve watch -- <cmd>` |
| Cursor (built-in agent) | partial | `sieve proxy`, then point Cursor's proxy setting at it |
| GitHub Copilot | partial | `sieve proxy` + VS Code `http.proxy` + `NODE_EXTRA_CA_CERTS` |
| Copilot in Visual Studio (not VS Code) | no | VS doesn't honour proxy env vars or self-signed CAs |

`sieve watch` wraps a child process and injects proxy env vars — that only works for CLI agents. Editor-embedded agents were never launched by sieve, so there's nothing to inject into. Use `sieve proxy` for those: it runs standalone and prints the per-editor setup.

Two Claude Code caveats: streaming can misbehave behind some proxies, and MCP servers registered via `claude mcp add` need `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` in their own env block — they don't inherit from the parent.

Copilot reads certs from the OS trust store and `NODE_EXTRA_CA_CERTS`, but GitHub documents that it may reject custom certificates in some configurations. Verify before trusting an empty report.

## How detection works

Files are reduced to **shingles** — hashes of sliding 4-line windows over normalized text. Matching on windows rather than whole files means partial reads, reformatting, and renamed identifiers still register.

Normalization is what makes it hold up. The same bytes reach the wire in four different shapes depending on how the agent read them:

| Source | On the wire |
|---|---|
| Read tool | `     1\timport os` |
| `cat` | `import os` |
| `grep -n` | `src/foo.py:1:import os` |
| `cat -n` | `   1  import os` |

All four normalize to the same shingle. Verified in `tests/test_pipeline.py`.

Shingles appearing in more than 3 files are dropped as boilerplate — otherwise a window of four import lines matches fifty files and the report becomes noise.

Request parsing is schema-agnostic: it walks the JSON tree rather than hardcoding provider shapes, so Anthropic Messages, OpenAI chat completions, and the OpenAI Responses API all work without special cases. Tested against all three.

## Tests

```bash
python3 tests/test_pipeline.py   # 9 detection cases across 3 API shapes
python3 tests/test_session.py    # multi-turn session -> rendered report
```

## Known limits

- **`used` is a heuristic.** It looks for edit-shaped tool calls and file mentions in assistant text. Conservative by design — it will call things unused that were quietly used, biasing the report toward flagging. Fine for a report, not fine if it ever gates anything.
- **Sessions merge** if two conversations start with a byte-identical message against the same model.
- **Paraphrase is invisible.** If the agent describes your algorithm without reproducing lines, no shingle matches. Nothing here fixes that.
- **Read-only.** No blocking, no redaction, no sandbox. Deliberately.

## Status

Working alpha, tested end to end against real TLS interception. MIT.
