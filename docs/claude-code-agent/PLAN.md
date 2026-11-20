# Claude Code as a coding agent: plan

Goal: a run can use Claude Code (`claude -p`) instead of Codex to execute the
prompts ChatGPT writes. Each run uses one agent for its whole life.

The seam already exists in code: `agent/coding_agent_provider.py` (provider
contract), `agent/coding_agents.py` (`resolve_provider`), and the run execution
profile's `coding_agent` field (`claude_code` is an allowed value in
`agent/ledger.py`). Every execution goes through
`agent/coding_agent_services.py::execute_coding_agent_service`.

## Steps

1. Claude Code adapter, stream reading, reason codes, wrapper change. Tested
   with a fake `claude` CLI.
2. `resolve_provider("claude_code")` returns the adapter; per-agent model lists
   in `RunExecutionProfile` and the ledger read-back (`ledger.py` profile
   validation).
3. Agent picker: dashboard dropdown, controller start request, CLI
   `--coding-agent codex|claude_code`.
4. Resume/continuation through the pre-chosen session id.
5. Environment check for `claude`; `AGENTS.md` handling for repos without
   `CLAUDE.md`; an "Executor: Claude Code" line in the ChatGPT feedback; let
   the provider build its own validation result instead of
   `cli.py::_codex_exec_validation_result`.
6. Live check, run by the user: one read-only and one workspace-write run on a
   throwaway repo, confirming each permission level blocks what it should.

## Step 1 status (2026-11-20): done, fake CLI only

Implemented:

- `agent/claude_code_stream.py`: parses `--output-format stream-json` lines;
  `write_final_message_from_stream` copies the final `result` text into
  `final_message.md`, only for `subtype == "success"` and `is_error` false.
- `agent/codex_invocation_wrapper.py`: when the intent has
  `final_message_source: "claude_stream_json_result"`, the wrapper writes the
  final message before `exit.json` and records `final_message_extraction` in
  `exit.json`. Intents without the field (all Codex runs) are unchanged.
- `agent/codex_invocation.py::build_intent_payload` and
  `agent/codex_terminal.py::run_json_streaming_command`: pass
  `final_message_source` through (written only when set). Progress titles take
  an agent label; Codex wording is unchanged.
- `agent/claude_code_provider.py`: `ClaudeCodeProvider` (execute, observe,
  normalize_progress, session_details, classify, reason_code, error_message,
  validation_result, cancel) and `build_claude_command`.

Command shape (code-confirmed):

```
claude -p --output-format stream-json --verbose --permission-prompts none \
  --setting-sources project,local --strict-mcp-config <permission flags> \
  [--model <alias>] (--session-id <uuid> | --resume <uuid>) -- <prompt>
```

Permission levels (intended equivalents of Codex's sandbox levels; checked
only against the fake CLI, not the live one):

| sandbox | flags |
|---|---|
| read-only | `--permission-mode dontAsk`, settings allow Read/Grep/Glob, deny Bash/Edit/Write/NotebookEdit/WebFetch/WebSearch |
| workspace-write | `--permission-mode acceptEdits`, settings `sandbox.enabled`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands: false` |
| danger-full-access | `--permission-mode auto` (auto mode; changed 2026-11-20, was `--dangerously-skip-permissions`) |

Reason codes: `claude_code_exec_completed`, `claude_code_validation_error`,
`claude_code_not_found`, `claude_code_timed_out`,
`claude_code_progress_persistence_failed`, `claude_code_exit_missing`,
`claude_code_result_<subtype>` / `claude_code_result_error`,
`claude_code_nonzero_exit`, `claude_code_final_message_<status>`. An uncertain
invocation keeps `codex_invocation_uncertain`, because the controller branches
on that exact code.

Progress: stream events map to the existing dashboard kinds. Like Codex, shell
commands are stored as argv0/length/hash only; thinking and tool output are
not stored.

Persisted names are kept: `codex_exec_started` / `codex_exec_finished` /
`codex_progress_event`, `codex_invocation_id`, `data/runs/<run>/codex/...`.
`coding_agent: "claude_code"` distinguishes the agent.

Not done in Step 1 (by design): production wiring (Step 2).

Tests: `tests/test_claude_code_provider.py` (21). Full suite: 1241 run; the
only failures are the same 4 that already failed before this work
(`test_chatgpt_ax_capture` supervise wiring, two `test_remote_access`
controller snapshot tests, one `test_web_static` contract test).

## Step 2 status (2026-11-20): done

Implemented:

- `agent/coding_agents.py::resolve_provider`: `codex` -> `CodexProvider`,
  `claude_code` -> `ClaudeCodeProvider`, `cursor` still refused with
  `coding_agent_unavailable`.
- `agent/ledger.py`: `ALLOWED_CLAUDE_CODE_MODEL_SELECTIONS` =
  `codex_default`, `opus`, `sonnet`, `haiku`;
  `ALLOWED_MODEL_SELECTIONS_BY_CODING_AGENT`; `allowed_model_selections(agent)`.
  The stored profile read-back (`_parse_execution_profile_metadata`) validates
  the model against the run's agent. `codex_default` is the stored token for
  "the agent's own default" for every agent (name kept for compatibility).
- `agent/run_services.py::RunExecutionProfile` validates the model per agent.
  Codex and `cursor` accept exactly the values they did before; an unknown
  agent still fails on the agent check.
- `agent/claude_code_provider.py` takes its model list from the ledger.

The start-request checks were Codex-only until Step 3.

Tests: `tests/test_claude_code_provider.py` adds production-wiring tests
(real resolver, profile model reaches `--model`) and per-agent model tests.
`tests/test_coding_agents.py`: tests that used `claude_code` as the example of
an unavailable agent now use `cursor`; the concurrent mixed-provider test now
expects Codex and Claude Code to succeed side by side.

Test isolation fix: `tests/test_coding_agents.py` and the new end-to-end tests
now run with `PATH` = fake-CLI folder + `/usr/bin` + `/bin` and assert that
`codex`/`claude` resolve only to the fakes. Before this fix, once
`claude_code` resolved, two test runs reached the user's real `claude` CLI
(2026-11-20 21:11, temp repos, read-only, prompt "Say exactly: mixed 1"; it
answered "mixed 1"). No real Codex session was found.

Full suite: 1250 run; only the same 4 already-failing tests fail.

## Step 3 status (2026-11-20): done (tested; dashboard not yet opened live)

Implemented:

- `agent/run_services.py`: `SELECTABLE_CODING_AGENTS` (`codex`, `claude_code`),
  `CODING_AGENT_LABELS`, `CODING_AGENT_EXECUTABLES`, `coding_agent_label()`.
  `execution_profile_options()` adds `coding_agent_options` and
  `model_options_by_agent`.
- Dashboard server (`agent/local_server.py`): the start request accepts an
  optional `coding_agent`. Unknown or unselectable values (including `cursor`)
  return 400 `invalid_coding_agent`. The model is checked against that
  agent's list: `invalid_codex_model` for Codex (unchanged) or
  `invalid_claude_code_model`. The options payload lists each agent with
  `available` (its CLI is on PATH) and per-agent model options.
- Controller (`agent/local_controller.py`): `start_run(coding_agent=...)` and
  `validate_local_controller_start_request(coding_agent=...)`. The run profile
  records the agent. `profile_source` is `system_default` only when sandbox,
  model and agent were all omitted. The profile summary now includes
  `coding_agent`.
- Dashboard (`agent/web_static/`):
  - A "Coding agent" select, filled from the server; an agent whose CLI is
    missing is shown "(CLI not found)" and disabled.
  - Changing the agent reloads the model list and relabels the permission
    preset, model, reasoning and approval fields.
  - The start request sends `coding_agent`.
  - The run summary shows "Coding agent".
- CLI: `start --coding-agent codex|claude_code [--sandbox ...] [--model ...]`
  records a run profile, checked before the run is created. Plain `start` is
  unchanged (no profile, Codex). `codex-run` executes a Claude Code run through
  its profile; its `--sandbox` must match the profile (existing rule). The exec
  result printer shows `coding_agent: claude_code` / `claude_path:` for Claude
  runs; Codex output is unchanged.

Tests: `tests/test_coding_agent_selection.py` (13): server request and
options, controller validation and recorded profile, CLI start/no-orphan-run,
`codex-run` of a Claude Code run end to end with a fake `claude` (auto-supervise
stubbed, so no ChatGPT UI), and dashboard contracts (every element id the
script collects exists in the HTML). `tests/test_web_static.py`: the
placeholder-option count is now 4.

Not verified: the dashboard has not been opened in a browser with these
changes.

Full suite: 1263 run; only the same 4 already-failing tests fail. No real CLI
sessions were created.

## Auto mode for Claude Code's autonomous preset (2026-11-20): done (fake CLI only)

User decision: Codex's autonomous preset is Full Access; Claude Code's is
auto mode.

- `agent/claude_code_provider.py`: `danger-full-access` ->
  `--permission-mode auto`, no OS sandbox (like Codex Full Access). With
  `--permission-prompts none`, anything the auto-mode classifier would ask
  about is refused. Claude Code no longer offers
  `--dangerously-skip-permissions`.
- CRAX still stores and treats the level as `danger-full-access`: the same
  governance and approval rules, the same `--confirm-full-access` and
  remote-confirmation requirements, and the same remote-device filter.
- Auto-mode rules: runs load only project/local settings, so Claude Code's
  default auto-mode rules apply. Personal `autoMode` rules in the user
  settings do not. `claude auto-mode config` on this machine returned rules,
  so auto mode is configured here.
- Dashboard: the options payload adds `sandbox_options_by_agent` and
  `agent_presentation`. With Claude Code selected, the preset reads
  "Auto Mode (Autonomous)", the button reads "Use Auto Mode", and the approval
  text reads "Claude Code default — Auto Mode decides instead of asking". The
  run summary shows Auto Mode for Claude runs. Codex texts are unchanged.
- Not verified live: that `-p` runs actually start in auto mode. The stream's
  `system/init` event reports `permissionMode`; it is kept in the progress
  metadata (`value_summary.permissionMode`), so the first live run will show it.

Tests: `tests/test_coding_agent_selection.py::AutoModePresetTests`, plus
end-to-end and command tests in `tests/test_claude_code_provider.py`. Full
suite: 1268 run; only the same 4 already-failing tests fail.

## Fix: "default" model ignored the user's Claude settings (2026-11-20)

Found live: run `df0d6363-...` (Auto Mode) ran on `claude-fable-5-2`, Claude
Code's built-in default. The user's `~/.claude/settings.json` sets
`"model": "opus"` and `"effortLevel": "xhigh"`, but runs pass
`--setting-sources project,local`, which skips that file.

Fix (`agent/claude_code_provider.py::user_claude_defaults`):

- When the run's model is the default, CRAX reads only `model` and
  `effortLevel` from `$CLAUDE_CONFIG_DIR/settings.json` (default
  `~/.claude/settings.json`) and passes `--model` / `--effort` explicitly.
- An explicitly chosen model wins over the settings model; the settings effort
  is still passed.
- Malformed or missing values are ignored and Claude Code's own default
  applies.
- The rest of the user's settings (hooks, plugins, permissions) stay skipped.
- The run result records `claude_model`, `claude_model_source`
  (`explicit` / `user_settings` / `claude_code_default`) and `claude_effort`.
- The dashboard's default model option reads "Your Claude default (opus,
  effort xhigh)", or "Claude Code built-in default" when none is set.
- Tests point `CLAUDE_CONFIG_DIR` at a temp folder, so they never read the real
  settings file.

## Model and effort picker for Claude Code (2026-11-20): done (tested; not yet opened in a browser)

User request: a proper model picker with full model versions, like Claude
Code's own `/model`, plus an effort selector, instead of the short names
`opus` / `sonnet` / `haiku`.

- `agent/claude_code_models.py`: the catalog. 13 model IDs, each with a label,
  description, group and supported efforts. IDs and effort support come from
  the claude-api reference (cached 2026-11-03).
  - Main group: Opus 5.5, Fable 5.2, Sonnet 5.5, Haiku 5.5.
  - More models: Fable 5.1, Fable 5, Opus 5, Opus 4.8, Opus 4.7, Opus 4.6,
    Sonnet 5, Sonnet 4.6, Haiku 4.5.
  - Restrictions: Opus 4.6 and Sonnet 4.6 have no `xhigh`; Haiku 4.5 takes no
    effort.
  - Mythos models are excluded (verified organizations only).
  - The short names were removed; no stored run used them.
- Run profile: `reasoning_effort` (an existing field) is now selectable for
  Claude Code: `codex_default`, `low`, `medium`, `high`, `xhigh`, `max`. A
  model/effort pair the model doesn't support is refused in the profile, the
  ledger read-back, the controller and the server (`invalid_reasoning_effort`).
  Codex reasoning stays locked to its default.
- Execution: `CodingAgentRequest.reasoning_effort` carries it.
  `codex_exec_started` records `reasoning_effort` only when it isn't the
  default (Codex metadata is unchanged).
  - The Claude adapter passes `--effort`: an explicit choice wins, otherwise
    the user's own `effortLevel`.
  - The user's effort is dropped when the model doesn't accept it (recorded as
    `claude_effort_source: model_has_no_such_effort`).
- Dashboard:
  - The model select shows the catalog labels, with "More models" as an
    `<optgroup>` and the description under it.
  - A Reasoning select appears for Claude Code, filtered to the model's
    efforts; it's hidden for Codex and for Haiku 4.5.
  - The start request sends `reasoning_effort`.
  - The run summary shows "Fable 5.2 (claude-fable-5-2)".
  - The picker logic was checked by running the real `app.js` functions under
    node with a fake DOM, not in a browser.
- CLI: `start --effort low|medium|high|xhigh|max`; `--model` takes the full IDs.

Full suite: 1280 run; only the same 4 already-failing tests fail. No real CLI
sessions were created.
