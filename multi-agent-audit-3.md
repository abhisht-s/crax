# CRAX Multi-Coding-Agent Investigation — Audit 3

Observational characterization only. No CRAX source was modified, no adapter was designed, and neither CLI was run against this repository.

Established context from Audits 1–2: if a new worker preserves `codex_exec_started` / `codex_exec_finished` / `codex_progress_event` / `codex_invocation_uncertain`, the existing result-dict, `final_message.md`, and `BEGIN_NEXT_CODEX_PROMPT`, most downstream CRAX code can stay. This audit asks whether Claude Code and Cursor CLI can actually feed that contract.

Evidence tags:

- **[Code]** CRAX `agent/`
- **[Docs]** official vendor docs
- **[CLI]** local `--help` / `--version`
- **[Live]** disposable `/tmp` probes
- **[U]** still unproven

Date of local characterization: 2026-09-20.

---

## 1. Sources consulted

### CRAX (this worktree)

- `AGENTS.md`
- `ARCHITECTURE.md`
- `POLICY_MODEL.md`
- `STATE_MACHINE.md`
- `DIAGNOSTICS_VS_PRODUCTION.md`
- `MULTI_SESSION_CONTEXT.md`
- `MULTI_SESSION_IMPLEMENTATION_PLAN.md`
- `agent/codex_terminal.py`
- `agent/codex_services.py`
- `agent/codex_invocation.py`
- `agent/codex_invocation_wrapper.py`
- `agent/run_services.py`
- `agent/local_controller.py`
- `agent/ledger.py`

Audits 1–2 treated as given: Codex `codex exec` + durable wrapper is command-generic; strongest coupling is events, `final_message`, profile sandbox/model, JSONL normalization, ChatGPT sentinels.

### Claude Code official

- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/permission-modes
- https://code.claude.com/docs/en/sandboxing
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/agent-sdk/permissions
- https://code.claude.com/docs/en/agent-sdk/python (ResultMessage shape)
- https://code.claude.com/docs/en/sessions

### Cursor official

- https://cursor.com/docs/cli/headless
- https://cursor.com/docs/cli/using
- https://cursor.com/docs/cli/overview
- https://cursor.com/docs/cli/reference/output-format
- https://cursor.com/docs/cli/reference/parameters
- https://cursor.com/docs/cli/reference/authentication
- https://cursor.com/docs/cli/reference/permissions
- https://cursor.com/docs/cli/changelog

### Local binaries (2026-09-20)

- `claude` 2.1.251 at `/Users/abhisht/.local/bin/claude`
- `agent` 2026.09.18-9a7762b at `/Users/abhisht/.local/bin/agent`
- `cursor` is **not** a usable headless binary here: it prints “No Cursor IDE installation found. Use `cursor agent` or `agent`.”

### Live probes

Used only disposable git repos under `/tmp/crax-audit3-*`. Neither binary was pointed at CRAX. Credentials and global config were not changed. Cursor was **not** given `--trust` / `--force` and was **not** allowed to start a billed agent turn.

---

## 2. Claude Code headless execution contract

Local version: **2.1.251**.

| Topic | Fact |
|---|---|
| Binary | `claude` **[CLI]** |
| Headless form | `claude -p` / `--print` **[Docs][CLI]** |
| Prompt | argv string, and/or stdin (piped stdin capped at 10MB) **[Docs]** |
| Working directory | process cwd. There is **no** Codex-style `-C`. Extra roots via `--add-dir` **[Docs][Live]** (`cwd` in `system/init` was the fixture path) |
| Output | `--output-format text \| json \| stream-json` (print mode only) **[Docs][CLI]** |
| stream-json | newline-delimited JSON; last line is a `type:"result"` object **[Docs][Live]** |
| Final assistant text | `result` field on the terminal result object; also `jq -r '.result'` on `--output-format json` **[Docs][Live]** |
| Native final-message file | **No** `--output-last-message` equivalent **[CLI][Docs]** |
| Exit codes | 0 success; nonzero failure. Invalid flags → stderr before start. In-run failures (e.g. auth) print the failure as the result on **stdout** **[Docs][Live]** |
| Permission modes | `default`/`manual`, `acceptEdits`, `auto`, `bypassPermissions`, `dontAsk`, `plan` **[Docs][CLI]** |
| `-p` default mode | Manual/`default`, not Pro interactive `auto` **[Docs]** |
| `--allowedTools` | pre-approves; does **not** remove other tools. `--tools` restricts availability **[Docs][Live]** |
| `--bare` | skip auto-discovery of hooks, skills, plugins, MCP, auto memory, CLAUDE.md; sets `CLAUDE_CODE_SIMPLE`; Anthropic auth is **only** `ANTHROPIC_API_KEY` or `apiKeyHelper` (no OAuth/keychain) **[Docs][CLI]** |
| `--restricted` | removes command/code tools (Bash/PowerShell/REPL) and WebFetch unless named in `--tools`; confines file tools to working dirs; ignores user/project settings **[Docs][CLI]** |
| `--safe-mode` | disables customizations but **keeps** auth, model, built-in tools, and permissions (unlike `--bare`) **[Docs][CLI]** |
| `--setting-sources` | `user,project,local` allowlist **[CLI]** |
| Sandbox | OS-enforced Bash Seatbelt (macOS), **separate** from permission mode. Default write scope: cwd + session temp. Network is proxied/allowlisted. Not on by default for `-p` unless settings enable it **[Docs]** |
| `--bg` | **rejected** with `-p` **[Docs]** |
| SIGTERM | exit 143; no result recorded; Bash tree killed; SessionEnd hooks only **[Docs]** |
| SIGINT | ends the in-progress turn instead **[Docs]** |
| Background children during `-p` | background Bash killed ~5s after result; **subagents keep the process open** until done, with a 10-minute idle ceiling (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`) **[Docs]** |
| Session | `--continue` / `--resume <id\|path>`; `--no-session-persistence` (print only); `--fork-session` **[Docs][CLI]** |
| Model | `--model` alias (`sonnet`, `opus`, `haiku`, `fable`) or full id; `--fallback-model` **[Docs][CLI]** |
| MCP | auto-loaded from project/user unless `--bare` / `--strict-mcp-config` / `--restricted` **[Docs]** |
| IDE | `--ide` optional; **not** required **[CLI]** |
| `--permission-prompts none` | documented as v2.1.259+; **absent** from this 2.1.251 `--help` **[Docs][CLI]** |

Exact candidate argv for an unattended read-only inspect (documented + live-init-confirmed, not a successful authenticated run):

```text
claude --bare --restricted -p "<prompt>" \
  --tools Read \
  --permission-mode dontAsk \
  --output-format stream-json --verbose \
  --no-session-persistence
```

`--bare` alone is **not** enough to drop Edit/Bash: live `system/init` with `--bare --allowedTools Read` still advertised `tools: ["Bash","Edit","Read"]`. `--tools Read` reduced that to `["Read"]`. `--restricted --bare` still advertised `["Edit","Read"]` — Edit remains, so `--restricted` is **not** CRAX read-only.

---

## 3. Cursor CLI headless execution contract

Local version: **2026.09.18-9a7762b**. Headless executable is **`agent`**, not `cursor`.

| Topic | Fact |
|---|---|
| Binary | `agent` **[CLI]**. `cursor` on this Mac is an IDE launcher stub. |
| Headless form | `agent -p` / `--print` **[Docs][CLI]** |
| Prompt | argv `prompt...` **[CLI]** |
| Workspace | cwd, or `--workspace <path-or-name>`; extra roots `--add-dir`; optional `--worktree` under `~/.cursor/worktrees/` **[Docs][CLI]** |
| Output | `--output-format text \| json \| stream-json`; default `text` **[Docs][CLI]** |
| stream-json | NDJSON. Types: `system`/`init`, `user`, `assistant`, `tool_call` (`started`/`completed`), terminal `result` **[Docs]** |
| Partial streaming | `--stream-partial-output` (stream-json only). Three assistant-event shapes; only `timestamp_ms` present and `model_call_id` absent is new text **[Docs]** |
| Final text | `result` field on terminal `{type:"result", subtype:"success"}`. `text` format emits **only** the last assistant message after the last tool call **[Docs]** |
| Native final-message file | **No** **[CLI][Docs]** |
| Failure stream | non-zero exit; stream may **end without** a terminal `result`; error on **stderr** **[Docs]** |
| `--print` tools | help text: “Has access to **all tools, including write and shell**” **[CLI]** |
| `--force` / `--yolo` | auto-approve unless explicitly denied; changelog: also covers **trust and MCP** approvals **[CLI][Docs]** |
| Headless writes | Headless doc: without `--force`, changes are “only proposed, not applied”. Using-page: “Cursor has **full write access** in non-interactive mode.” These **conflict**. Treat as **[U]** until an authenticated write probe. |
| Modes | default agent; `--mode=plan` (read-only planning); `--mode=ask` (read-only Q&A) **[Docs][CLI]** |
| Sandbox | `--sandbox enabled\|disabled`. This is **command isolation**, default workspace read/write, network default-deny. Separate `agent sandbox enable/disable` persists config **[Docs][CLI]** |
| Trust | untrusted workspace in `-p` **fails immediately** unless `--trust`, `--yolo`, or `-f` **[Docs][Live]** |
| Auth | `agent login` (browser) or `CURSOR_API_KEY` / `--api-key`. `agent status` / `whoami` **[Docs][CLI]** |
| This machine | logged in as `abhisht1809@gmail.com`, Pro+, no `CURSOR_API_KEY` in the environment **[Live]** |
| IDE required? | **No** for `agent`. `cursor` without the IDE app fails. **[CLI]** |
| Session | `--resume [chatId]`, `--continue`, `agent resume`, `agent ls`, `agent create-chat` **[CLI]** |
| Persist/daemon | `agent persist` survives terminal disconnect; **not** default `-p` **[CLI]** |
| Stdin hang | changelog: `-p` no longer blocks when spawned with an open stdin pipe **[Docs]** |
| MCP | auto-detects `.cursor/mcp.json` and `~/.cursor/mcp.json`; `--approve-mcps` auto-approves servers **[Docs][CLI]** |
| Rules | `.cursor/rules`, plus project-root `AGENTS.md` and `CLAUDE.md` **[Docs]** |
| Models | `--model`, `--list-models`; default `auto` on fresh installs **[Docs][CLI]** |

Exact candidate argv (docs only; authenticated run not performed):

```text
# fail-closed if workspace untrusted (observed)
agent -p --mode=ask --output-format stream-json "<prompt>"

# unattended inspect, docs-only, billed, not executed here
agent -p --mode=ask --trust --output-format stream-json "<prompt>"
```

Passing `--trust` is required for overnight use in a new repo. `--yolo`/`-f` also skip trust, which couples “approve everything” to “trust this tree.”

---

## 4. CRAX-contract compatibility matrix

CRAX today launches:

```text
codex exec [--json] -C <repo> (-s <sandbox> | --dangerously-bypass-approvals-and-sandbox)
  [-m <model>] --output-last-message <path> <prompt>
```

behind a generic wrapper: `command[]`, `cwd`, inherited env, stdout/stderr files, `start_new_session=True`, wait-for-exit, process-group kill **[Code]**.

| Requirement | Claude | Cursor |
|---|---|---|
| 1. Command in a specific repo | **can satisfy with normalization** (set wrapper `cwd`; no `-C`) | **can satisfy with normalization** (`cwd` or `--workspace`) |
| 2. Durable process-exit evidence | **likely compatible unchanged** if the CLI stays in-process | **likely compatible unchanged** for default `-p` |
| 3. stdout/stderr capture | **directly satisfies** | **directly satisfies** |
| 4. Optional stream progress | **can satisfy with normalization** (NDJSON → existing kinds) | **can satisfy with normalization** (cleaner typed NDJSON) |
| 5. One final assistant message before `codex_exec_finished` | **can satisfy with normalization** (synthesize `final_message.md` from `result`) | **can satisfy with normalization** (same, but **only if** a `result` event exists) |
| 6. Killable / reconcilable lifecycle | **can satisfy**, with **unknown** subagent/Bash-grandchild risk | **likely compatible unchanged** for `-p`; `persist`/worker/cloud are separate |
| 7. Honest success/failure | **unsafe/unknown** until classifier is specified: live auth failure is `exit=1` **and** `subtype:"success"` + `is_error:true` + `result:"Not logged in · Please run /login"` | **can satisfy with normalization** if CRAX treats missing `result` + nonzero as failure **[Docs]**; authenticated paths **[U]** |
| 8. Immutable per-run backend | product/profile issue, not a CLI gap | same |
| 9a. read-only intent | **can satisfy with normalization** (`--tools Read` + `dontAsk` + `--bare`; git still the objective check) | **unsafe/unknown**: `--mode=ask` is documented read-only, but `-p` “has access to all tools including write”; unproven live |
| 9b. workspace-write | **unsafe/unknown**: `acceptEdits` auto-approves edits + some fs commands, **not** general shell/network | **unsafe/unknown**: `--force --sandbox enabled` is the closest pair; `--force` also skips trust/MCP |
| 9c. danger-full-access | **can satisfy with normalization** as *approval bypass* (`--dangerously-skip-permissions`), **not** as host isolation | **can satisfy with normalization** as `--yolo --sandbox disabled`; also not host isolation |
| 10. No silent overnight hang | **can satisfy** for logged-out auth **[Live]**; permission-prompt and stdin-pipe hangs **[U]** on 2.1.251 | **directly satisfies** untrusted-workspace fail-fast **[Live]**; `--trust` then billed-run hangs **[U]** |

Nothing here is a hard “the CLI cannot be spawned.” The incompatibilities are **semantic**: permission names, final-message file, and honest `ok`.

---

## 5. Final-message extraction feasibility

Producer-independent `final_message.md` is **feasible**, but only if CRAX synthesizes the file **after** process exit and **before** `codex_exec_finished`, which is already how Codex is wired: spawn → wait → read artifact → emit `final_message_available` → `process_exited` → services persist `codex_exec_finished` **[Code]**.

| Question | Claude | Cursor |
|---|---|---|
| Native final-message file? | No | No |
| Where is the final text? | `result` on the terminal JSON object (json or last stream-json line) **[Docs][Live]** | `result` on terminal event; `text` format is last assistant segment only **[Docs]** |
| Distinguishable from commentary/tools? | Yes: stream has `assistant` vs `type:"result"`. Auth failure also emits a synthetic `assistant` with `error:"authentication_failed"` **[Live]** | Yes: `assistant` vs `tool_call` vs `result`. Docs: skip assistant events and read `result` if you only want the answer **[Docs]** |
| Deterministic single final response? | Yes, **if** CRAX uses the terminal `result` object, not the last `assistant` line | Yes on success. **On failure, there may be no `result` event** **[Docs]** |
| Safe to synthesize `final_message.md`? | Yes as a file. **Not** safe to treat presence of text as success | Same, with a worse miss: success JSON vs stderr-only failure |
| Failure cases | **no final result**: documented for some SDK error subtypes (`result` is `None` on `error_*`) **[Docs]**; **auth text on stdout**: observed **[Live]**; **`subtype:"success"` + `is_error:true`**: observed; **malformed/truncated JSON**: possible; **multiple assistant messages**: normal, must ignore them for the artifact | **no result on failure**; **auth/trust on stderr** (trust probe) **[Live]**; **`result` concatenates all assistant text segments** (docs example glues them) **[Docs]**; truncated NDJSON **[U]** |

**Trap for CRAX:** copying `result` into `final_message.md` whenever JSON parses would have labeled a logged-out Claude run as a completed assistant answer (“Not logged in · Please run /login”) even though `exit_code=1` and `is_error=true`. `_reason_code` already fails nonzero exits **[Code]**, so that path would not become `codex_exec_completed` — but ChatGPT-facing text must still not be that string.

---

## 6. Progress normalization mapping

Preserve raw provider JSON (or a hash + bounded keys, as Codex already does in `_safe_json_metadata`) in `metadata` for diagnostics **[Code]**. Do not implement the mapper yet.

### Claude stream-json → existing kinds

| CRAX kind | Claude source | Mapping |
|---|---|---|
| `process_started` | first `system/init` | **direct** |
| `assistant_commentary` | `type:"assistant"` text blocks with `parent_tool_use_id` null | **direct** (filter synthetic `is_api_error_message`) |
| `command_started` / `command_finished` | Bash `tool_use` / `tool_result` (not observed live; documented agent loop) | **heuristic** |
| `tool_event` | other `tool_use` / `tool_result` | **heuristic** |
| `file_change_summary` | Edit/Write tool payloads | **heuristic** |
| `final_message_available` | terminal `type:"result"` after CRAX writes the file | **direct** (CRAX-side) |
| `process_exited` | wrapper wait, not the CLI | **direct** (already wrapper) |
| `error` | `is_error:true`, `terminal_reason:"api_error"`, assistant `error:"authentication_failed"`, `system/api_retry` exhaustion | **direct** for auth **[Live]**; others **heuristic** |
| `blocked` | `permission_denied` system messages (v2.1.259+); `dontAsk` denials | **unknown** on 2.1.251 (flag missing) |
| fallback `codex_json_event` | hook events, plugin_install, stream_event deltas, unknown types | **direct** (today’s fallback path) |

Live observed lines only: `system/init`, synthetic `assistant`, `result`. Tool events were not seen because the run never authenticated.

### Cursor stream-json → existing kinds

| CRAX kind | Cursor source | Mapping |
|---|---|---|
| `process_started` | `system/init` | **direct** |
| `assistant_commentary` | `type:"assistant"` (skip partial duplicates if `--stream-partial-output`) | **direct** |
| `command_started` / `command_finished` | `tool_call` with `function` / shell-shaped tools | **heuristic** |
| `tool_event` | `tool_call.started` / `.completed` | **direct** |
| `file_change_summary` | `writeToolCall` | **direct** |
| `final_message_available` | `type:"result"` + synthesized file | **direct** |
| `process_exited` | wrapper | **direct** |
| `error` | nonzero + stderr; `is_error:true` if present | **direct** on trust fail **[Live]**; billed failures **unknown** |
| `blocked` | trust required; command approval without `--force` | **heuristic** (trust is process-fatal, not a stream event) |
| fallback | unknown tool_call keys | **direct** |

---

## 7. Permission / sandbox mapping

CRAX presets are **product semantics**. Codex maps them onto `-s read-only|workspace-write` or `--dangerously-bypass-approvals-and-sandbox` **[Code]**. Claude and Cursor do **not** have those three names.

### Claude

| CRAX preset | Closest flags | What they actually enforce | Classification |
|---|---|---|---|
| **read-only** | `--permission-mode dontAsk --tools Read` (optionally Grep/Glob) plus `--bare`/`--restricted`; git still checks writes | Tool **availability** via `--tools`; unmatched tools **denied** in `dontAsk`. `--allowedTools` alone does **not** remove Bash/Edit **[Live]**. `--restricted` still left **Edit** enabled **[Live]**. Sandbox, if on, is Bash isolation, not “no edits.” | **safe enough to characterize further** as the first slice, **if** `--tools` omits Edit/Write/Bash **and** CRAX keeps the post-run git check |
| **workspace-write** | `--permission-mode acceptEdits` + `--bare`; optional `sandbox.enabled` | Auto-approves file edits in cwd/`--add-dir` and some fs commands (`mkdir`/`mv`/`cp`/…). Other shell and network still need allow rules. Project `permissions.allow` can widen. | **unknown pending live probe**; not 1:1 |
| **danger-full-access** | `--dangerously-skip-permissions` (= `bypassPermissions`) | Skips permission prompts and protected-path checks. Still circuit-breaks `rm -rf /` and `~`. Docs: isolated containers only. In `-p`, leftover promptable calls are **denied** rather than hung **[Docs]**. Not a VM. | **unsafe for unattended use** on the operator Mac without an outer jail |

Sandbox vs approval: **sandbox is real OS isolation for Bash**; permission modes are **approval policy**. They compose. `auto` is a classifier, not isolation. `acceptEdits` is **not** “shell too.”

Project/user config can silently widen: `.claude/settings.json` hooks, `.mcp.json`, `permissions.allow`, `sandbox.filesystem.allowWrite`, `permissions.defaultMode`. `--bare` is the documented control; live `--bare` still listed a user-level PostHog plugin in `system/init` (mcp_servers stayed `[]`). `--restricted` cleared that plugin list **[Live]**.

### Cursor

| CRAX preset | Closest flags | What they actually enforce | Classification |
|---|---|---|---|
| **read-only** | `--mode=ask` **without** `--force` | Documented as no edits. Help text still says `-p` has write+shell. Untrusted trees fail closed without `--trust` **[Live]**. Overnight requires `--trust`, which is an operator attestation, not a sandbox. | **unknown pending live probe**; initially restrict to this **only after** an authenticated ask-mode write test |
| **workspace-write** | `-p --force --sandbox enabled --trust` | `--force` auto-approves commands (and trust/MCP). Sandbox = workspace read/write for **commands**, network default-deny. File-edit policy vs shell policy is not Codex-shaped. | **unsafe for unattended use** until write/shell scope is live-proven |
| **danger-full-access** | `-p --yolo --sandbox disabled --trust` | Changelog: `--yolo` auto-approves trust, MCP, and commands. Sandbox off = allowlist/host shell. | **unsafe for unattended use** |

**Recommendation from this audit (not an implementation decision):** any first backend should be **read-only-only** until an authenticated probe shows the worker cannot write. CRAX’s git check is the objective backstop, but it cannot see non-git writes outside the repo.

---

## 8. Project-local config loading risks

This is a real bypass path for CRAX’s execution profile.

### Claude (default `-p`, **no** `--bare`)

Loads the same context as interactive: working-directory and `~/.claude` hooks, skills, plugins, MCP, auto memory, CLAUDE.md **[Docs]**. A `-p` session **does not** show a workspace-trust dialog and **does not** prompt per MCP server; it will run project `.claude/settings.json` hooks and `.mcp.json` even in an untrusted folder **[Docs]**.

Disable/reduce:

| Flag | Effect | Quality cost |
|---|---|---|
| `--bare` | skip auto-discovery; future default for `-p` | loses project CLAUDE.md/skills/MCP; intended for CI |
| `--restricted` | ignore user/project settings; drop command tools | still has Edit **[Live]** |
| `--safe-mode` | disable customizations; keep permissions | troubleshooting, not a permission jail |
| `--setting-sources` | limit user/project/local | unused combination **[U]** |
| `--strict-mcp-config --mcp-config …` | exclusive MCP list | empty list = no MCP |

`--bare` changes expected quality (no repo CLAUDE.md). That is acceptable for CRAX if ChatGPT’s prompt already carries the task. Loading project MCP/hooks can add network and filesystem powers **not** represented in the dashboard sandbox dropdown.

### Cursor

Auto-loads `.cursor/rules`, root `AGENTS.md` and `CLAUDE.md`, `.cursor/mcp.json`, `~/.cursor/mcp.json`, `~/.cursor/cli-config.json` / `/.cursor/cli.json` permissions, `.cursor/sandbox.json` / `~/.cursor/sandbox.json`, worktree setup from `.cursor/worktrees.json` (after trust) **[Docs]**. Skills load in headless **[Docs]**. No documented `--bare`. `--plugin-dir` adds more.

There is **no** first-class “ignore project config” switch comparable to Claude `--bare`. CRAX would have to assume repo Cursor config is in play, or invent an isolation cwd/worktree.

---

## 9. Auth / rate-limit / hang behavior

### Claude (2.1.251)

| Condition | Exit | Stream | Hang? | Notes |
|---|---|---|---|---|
| Logged out, no `ANTHROPIC_API_KEY` | **1** **[Live]** | stdout JSON `is_error:true`, `terminal_reason:"api_error"`, `result:"Not logged in · Please run /login"`; stream also has assistant `error:"authentication_failed"` | **No** (~0.7s) **[Live]** | stderr empty |
| Invalid CLI flag | **1** **[Live]** | none | **No** | stderr `error: unknown option '…'` |
| `--bare` + subscription OAuth | documented **not** to read keychain; needs API key **[Docs]** | | | using `--bare` on this machine would **not** reuse `claude auth login` even after login |
| Permission prompt in `-p` without host | documented: denied, not waited **[Docs]** | | | `--permission-prompts none` **not in this version** |
| `dontAsk` | deny instead of prompt **[Docs]** | | | |
| `auto` classifier fallback to prompt | **[U]** without v2.1.259 `permission-prompts none` | | **unknown** | |
| Rate limit / overload | `system/api_retry` then error categories `rate_limit`, `overloaded` **[Docs]** | retries internally | bounded retries, not infinite hang **[Docs]** | |
| Stdin inherited open pipe | docs: `-p` reads stdin | | **unknown** vs Cursor’s documented fix | CRAX wrapper does **not** set `stdin=DEVNULL` **[Code]** |
| Subagent still running | process stays up to 10 min idle ceiling **[Docs]** | | **possible long wait** | not a TTY prompt |

`claude auth status` this machine: `loggedIn: false`, `authMethod: "none"` **[Live]**. `ANTHROPIC_API_KEY` unset.

### Cursor (2026.09.18-9a7762b)

| Condition | Exit | Stream | Hang? | Notes |
|---|---|---|---|---|
| Untrusted workspace, `-p` without `--trust` | **1** **[Live]** | empty stdout; stderr “Workspace Trust Required” naming `--trust` / `--yolo` / `-f` | **No** (~0.5s) | did not call the model |
| Logged out / missing key | documented “Not authenticated”; run `agent login` or set key **[Docs]** | | **unknown** whether `-p` exits or opens browser | `agent login` opens a browser unless `NO_OPEN_BROWSER=1` |
| This machine | authenticated Pro+ **[Live]** | | | billed run skipped |
| `--force`/`--yolo` | skips trust **and** approvals **[Docs]** | | avoids prompts by auto-approving | too wide for CRAX read-only |
| Command approval without `--force` | interactive y/n **[Docs]** | | **unsafe** for overnight if `-p` still prompts | headless write doc vs “full write access” unresolved |
| Failure JSON | no well-formed JSON object on failure **[Docs]** | stderr | | |
| Open stdin pipe | changelog: **fixed**, `-p` does not block **[Docs]** | | | better than Claude docs |
| Long headless HTTP stall | changelog: wedged uploads retried instead of silent hang **[Docs]** | | | |

Expired auth, invalid `CURSOR_API_KEY`, quota, model unavailable: **not live-probed**. Docs do not give a full exit-code table.

---

## 10. Durable-wrapper / process-group compatibility

CRAX wrapper **[Code]**:

- `command` list from intent
- `cwd`
- inherited environment (`PYTHONUNBUFFERED=1` added for wrapper python)
- stdout/stderr files
- `subprocess.Popen` **without** `stdin=DEVNULL`
- wrapper `start_new_session=True` so kill is `killpg`
- wrapper forwards SIGINT/SIGTERM to the **direct child pid**, not the child’s group
- waits for child exit
- no TTY required

| Backend | Verdict | Why |
|---|---|---|
| Claude `-p` | **likely compatible**, with wrapper caveats | No `--bg` with `-p`. SIGTERM documented. Risk: inherited stdin; background subagents; Seatbelt grandchildren; optional `claude daemon` is for `--bg` sessions, not this mode. |
| Cursor `-p` | **likely compatible unchanged** for default `-p` | Print mode is a foreground CLI. `agent persist` / `agent worker` / cloud `&` are different products and must not be used. Stdin-pipe hang is documented as fixed. |
| Either `--worktree` | **requires wrapper changes or a policy ban** | Extra checkout under `~/.claude/worktrees` or `~/.cursor/worktrees`; CRAX git evidence is the **session repo**, not that worktree. |

Buffered output: both stream-json forms are line-oriented. Wrapper writes stdout as binary append; CRAX already tails JSONL **[Code]**. `--verbose` is required for Claude stream-json in the official example.

---

## 11. Model selection

**Claude:** aliases `sonnet` / `opus` / `haiku` / `fable` or a full id; `--fallback-model` comma list. Logged-out init still reported `model: "claude-opus-5[1m]"` **[Live]**. Availability depends on plan/provider **[Docs]**.

**Cursor:** `--model` takes ids such as `gpt-5`, `sonnet-4-thinking`, and parameterized forms `'claude-opus-4-8[context=1m,effort=high,fast=false]'` **[CLI]**. `agent --list-models` on this Pro+ account returned a **large, account-specific** catalog including `auto` (listed as default) and many Claude/GPT/Grok/Gemini/Composer variants **[Live]**. Fresh installs default to Auto **[Docs]**.

CRAX today **allowlists** Codex models (`gpt-5`, `gpt-5-codex`, default) **[Code]**. The CLIs themselves accept opaque strings.

**Observational answer:** the CLIs do **not** require a CRAX allowlist for the process to start. CRAX should still use a **backend allowlist** (or `auto` plus a documented id) because (a) spend/availability is plan-dependent, (b) Cursor ids churn (`--list-models` is the live source of truth), (c) an unconstrained string is not a sandbox hole but it is an overnight-cost and “model missing” failure mode. A fully opaque store is technically possible; it is not sufficient for **correctness** of unattended runs.

---

## 12. Native-session continuation

Both CLIs can resume: Claude `--continue`/`--resume`; Cursor `--continue`/`--resume`/`agent resume`.

CRAX **does not need** provider-native continuation to preserve current product behavior. Continuity is ChatGPT chat + repo + ledger; each Codex step is a **new** `codex exec` **[Code][Docs]**.

Using native resume would:

- be **unnecessary** for the current loop
- **improve** in-worker multi-turn memory (the model would see its own prior tool history)
- **hurt** restart/replay: hidden transcript state outside invocation artifacts; `--continue` is “latest in this directory,” which is racy with four sessions; Claude `--continue` can attach to a **finished background** session (v2.1.257+) **[Docs]**

`--no-session-persistence` (Claude print mode) is the closer analogue to today’s fire-and-forget Codex exec.

---

## 13. Live characterization (separated from docs)

### Performed

Disposable git repos with `hello.txt` = `hello-from-audit3`. Non-mutating prompts. stdin = `DEVNULL`. Timeouts 12–20s. Fixtures not the CRAX tree.

**Claude, logged out, `--bare -p --permission-mode dontAsk --output-format json|stream-json --no-session-persistence`**

- exit **1**, **0.73s**, no hang
- stdout JSON; stderr empty
- `is_error: true`, `subtype: "success"`, `terminal_reason: "api_error"`, `result: "Not logged in · Please run /login"`, `apiKeySource: "none"`
- stream-json sequence: `system/init` → synthetic `assistant` (`error: "authentication_failed"`) → `result`
- `--bare --allowedTools Read` init tools: `Bash, Edit, Read` (allowedTools did not restrict)
- `--tools Read` init tools: `Read` only
- `--restricted --bare` init tools: `Edit, Read`; `plugins: []` (PostHog disappeared)
- `--bare` without `--restricted` still listed plugin `posthog@claude-plugins-official` from `~/.claude/plugins/cache`
- cwd in init: `/private/tmp/crax-audit3-claude-…`
- git tree unchanged
- unknown flag: exit 1, stderr only

**Cursor, logged in, `-p --mode=ask --output-format json`, no `--trust`/`--force`**

- exit **1**, **0.51s**, no hang, **no API turn**
- stdout empty
- stderr: “Workspace Trust Required” for that temp path; proceed via interactive `agent` or `--trust`/`--yolo`/`-f`
- git tree unchanged

### Not performed (cost / login / escalation)

- any authenticated Claude run (`ANTHROPIC_API_KEY` unset; `--bare` would ignore OAuth anyway)
- any Cursor run with `--trust` or `--force` (would bill Pro+)
- write attempts, sandbox probes, SIGTERM of a long job, stdin-pipe hang on Claude, MCP/tool widening, model-unavailable, rate limit

---

## 14. Claude vs Cursor — technical risk comparison

Not a product pick. Lower **implementation/safety risk** as a first worker:

| Axis | Lower risk | Why |
|---|---|---|
| Headless reliability | **Claude** slightly | Auth miss fails closed with structured stdout. Cursor trust miss fails closed on stderr. Both good. Claude’s `--bare`/`--restricted`/`--tools` give more control of what even starts. |
| Stream quality | **Cursor** | Small typed NDJSON (`system`/`assistant`/`tool_call`/`result`) vs Claude’s SDK message soup. |
| Final-message determinism | **tie, Claude has a sharper trap** | Both have a terminal `result`. Claude can emit a plausible `result` string on auth failure with `subtype:"success"`. Cursor may emit **no** JSON on failure. |
| Permission clarity | **Claude** | Named modes + `--tools` vs Cursor’s `--force`/`--yolo`/`--mode=ask`/`sandbox` overlay and contradictory write docs. |
| Unattended safety | **Claude** for permission-deny; **Cursor** for trust-fail | Cursor overnight **requires** `--trust`, which is easy to accidentally pair with `--yolo`. |
| Auth predictability | **Claude** on this machine (live) | Cursor login-success path unproven in `-p`. |
| Wrapper compatibility | **Cursor** slightly | Documented stdin-pipe fix; no 10-minute subagent wait in the docs. |
| Config-loading risk | **Claude** | `--bare`/`--restricted` exist. Cursor always loads project rules/MCP/AGENTS.md. |
| Implementation effort | **Claude** for safety flags; **Cursor** for JSON schema | First mapper is easier for Cursor; first **safe** profile is easier for Claude. |
| Unknowns needing live tests | both | Authenticated read-only write-proof is the blocker for each. |

**Technically lower-risk first target: Claude Code**, because CRAX’s hard constraints are unattended permission semantics and “do not silently hang,” and Claude exposes `dontAsk` + `--tools` + `--bare` + structured auth failure. Cursor’s stream schema is cleaner, but `--print`+`--force`+`--trust` is a coupled privilege bundle that does not map onto CRAX’s three presets without live proof.

---

## 15. Exact unknowns that still need live probing

Only after operator approval of spend, and only in a disposable repo:

1. **Authenticated Claude read-only:** `--tools Read --permission-mode dontAsk --bare` — does it write files? does git stay clean if prompted to edit?
2. **Authenticated Claude `acceptEdits`:** shell/network still prompt or hang? sandbox on/off difference?
3. **Claude stdin:** wrapper-style inherited open pipe (not DEVNULL) — hang or proceed?
4. **Claude SIGTERM** of a live `-p` with a child Bash: does `killpg` on the wrapper session reap Seatbelt children? exit 143 and no result, as documented?
5. **Claude `--bare` vs plugins:** does listed PostHog plugin actually register tools when authenticated?
6. **Claude `dontAsk` + Edit still present (`--restricted`):** does Edit run or get denied?
7. **Authenticated Cursor `--mode=ask --trust`:** any writes? any shell? any hang on MCP?
8. **Cursor `-p` without `--force` but with `--trust`:** proposes vs applies edits (doc conflict).
9. **Cursor `--sandbox enabled`:** does it constrain file tools or only Shell? network?
10. **Cursor invalid/expired auth in `-p`:** exit vs browser vs hang (`NO_OPEN_BROWSER`).
11. **Both:** rate limit, quota, model-not-found, truncated NDJSON, multiple `result` lines.
12. **Both:** grandchild processes after wrapper `killpg`.

---

## 16. Three architecture questions Audit 4 must answer

1. **Honest completion:** How should CRAX classify success when the worker exits 0/1, stream `result` exists, `is_error`/`subtype` disagree, or stderr-only failures have no JSON — without putting auth/trust text into `final_message.md` or marking an empty artifact as `codex_exec_completed`?

2. **Preset enforcement:** What is the **minimum flag set** (and required “ignore project config” mode) such that dashboard `read-only` / `workspace-write` / `danger-full-access` cannot be widened by `.claude/`, `.cursor/`, MCP, or user settings — and is the first shipped backend **read-only-only** until that is proven?

3. **Wrapper process contract:** Must CRAX change the durable wrapper (stdin=`DEVNULL`, kill process group of the CLI not just pid, ban `--worktree`/`--bg`/`persist`, cap wait for Claude subagents) **before** a second backend is allowed, or can Claude/Cursor ride the current command-generic wrapper if those flags are simply never passed?

No adapter interface is proposed here. No implementation plan. No CRAX source files were changed for this audit.
