# CRAX Multi-Agent Scope of Work

**Status:** target architecture and staged implementation plan. **Not implemented.**
Current production remains Codex-only.

**Operator product rule (overrides Audit 4 extra gates):**

CRAX’s current abilities, policy, and loop stay as they are today.
The only product change is that a run may use **Codex, Claude Code, or
Cursor** as the coding worker. Those workers get the **same CRAX abilities
Codex already has**. Do not add worker-specific CRAX safety, feature flags,
restricted presets, live-probe enablement gates, or isolation modes that
Codex does not already have.

Audits 1–3 remain useful for *how the CLIs differ* (argv, stream,
final-message file). Audit 4’s extra product restrictions (Claude
read-only-only, Cursor disabled, mandatory `--bare`, experimental flags,
workspace-write hidden until proven) are **not** part of this plan.

**Primary sources for current Codex behavior:**

- `multi-agent-audit-1.md` — current Codex execution contract
- `multi-agent-audit-2.md` — downstream coupling
- `multi-agent-audit-3.md` — Claude / Cursor CLI shapes (mapping only)
- `muti-agent-audit-4.md` — seam/profile advice only; extra gates rejected
- `AGENTS.md`, `ARCHITECTURE.md`, `POLICY_MODEL.md`, `STATE_MACHINE.md`,
  `DIAGNOSTICS_VS_PRODUCTION.md`, `MULTI_SESSION_CONTEXT.md`,
  `MULTI_SESSION_IMPLEMENTATION_PLAN.md`

If this plan and current `agent/` code disagree, stop and audit before
implementing.

---

## 1. Mission

CRAX remains:

- ChatGPT Desktop as planner/reviewer
- CRAX as supervisor, durable state machine, transport, git/governance layer
- one coding worker per run

The coding worker is selectable per run at start:

- `codex` (default; omitted `coding_agent` is Codex)
- `claude_code`
- `cursor`

A run chooses exactly one worker at start. That worker is immutable for the
run, the same way sandbox and model are already immutable on the execution
profile. There is no mid-run switching and no silent spawn of a different
worker than the profile recorded.

Existing runs with no `coding_agent` field remain Codex via explicit schema
v1 compatibility.

We are replacing the hands, not the brain or the supervisor.

**Changing:**

- durable `coding_agent` on the execution profile
- a narrow adapter under the existing Codex execution service so Claude and
  Cursor can feed the **existing** result/event/git/ChatGPT contract
- dashboard/API worker selector and honest worker identity in copy

**Not changing:**

- ChatGPT as planner
- CRAX supervision, ledger, git governance, destination gate, conversation
  claims, multi-session registry
- CRAX permission presets: `read-only`, `workspace-write`,
  `danger-full-access` — including today’s confirmation / remote full-access
  gates, observation-heavy workspace-write policy, and post-run git checks
- live Codex argv/result contract as the reference path
- legacy `codex_*` event names, artifact directory, invocation ids, and
  `BEGIN_NEXT_CODEX_PROMPT` / `END_NEXT_CODEX_PROMPT`

**Not adding (rejected extra CRAX safety):**

- feature flags that hide Claude or Cursor
- Claude or Cursor limited to read-only
- hiding `workspace-write` or `danger-full-access` for non-Codex workers
- mandatory `--bare` / `--restricted` / “script-like isolation”
- live-probe gates before a worker is selectable
- wrapper rewrites as a new security layer
- new repo locks, worker serialization, or failover rules
- raw backend CLI flags in the dashboard

---

## 2. Core invariants

These are **existing CRAX/Codex rules**, applied to whichever worker the
profile names. They are not new product safety.

1. **One immutable coding worker per run.** Same freeze as today’s
   execution profile after `codex_exec_started`. Authority is the durable
   profile, not an API argument or snapshot.
2. **Spawn the worker the profile recorded.** A Claude run must not spawn
   Codex because `claude` is missing, a flag is off, or parse failed.
   Missing binary is today’s `found=False` behavior, for that worker.
3. **Uncertain invocation is never replayed.** Existing Stage 1 rule.
4. **Keep the current Codex-named contract.** `codex_exec_started` /
   `codex_exec_finished` / `codex_progress_event` /
   `codex_invocation_uncertain` / `extracted_codex_prompt_*` /
   `codex_invocation_id` / `data/runs/<run_id>/codex/<invocation_id>/`.
5. **Durable artifacts outrank RAM.** Existing restore rule. For workers
   that do not write `final_message.md`, recover it from durable stdout the
   same way restore already re-reads Codex’s file from disk.
6. **Adapters do not own planner/governance.** Same split as today’s Codex
   adapter vs git/ChatGPT.
7. **CRAX presets stay CRAX presets.** Persist `read-only` /
   `workspace-write` / `danger-full-access`. Map them to each CLI the way
   Codex already maps `-s` / `--dangerously-bypass-approvals-and-sandbox`.
   Do not invent a second permission model.
8. **Terminal/cancelled behavior stays as today.** Cancel the run’s wrapper
   group; do not start a replacement invocation for that id.
9. **Mixed workers are run-isolated.** No machine-global coding agent.
   Same-repo concurrent writers stay the existing operator choice.
10. **ChatGPT sentinel stays unchanged.** Parser keeps
    `BEGIN_NEXT_CODEX_PROMPT` / `END_NEXT_CODEX_PROMPT`. Prose should name
    the worker that actually ran.
11. **Existing success/failure fields stay honest.** Planner already stops
    on `validation_error`, `found is False`, `timed_out`, missing/nonzero
    `exit_code`. GPT feedback already requires a valid `final_message`.
    Auth/error text must not be labeled a successful assistant message —
    that is the current “no stdout fallback / honest result” contract, not
    a new classifier product.

---

## 3. Current architecture baseline

Deeper evidence: Audits 1–2. This section is only the seams a stage agent
must touch.

### 3.1 Product loop today

ChatGPT issues a prompt → coding worker executes in a repo (today: Codex)
→ governance records git evidence → planner chooses the next action → CRAX
pastes the completion report into that session’s chat → capture → extract
sentinel prompt → repeat.

`STATE_MACHINE.md`: run status `completed` is the **mid-loop worker-finished**
value, not loop-finished. Planner requires a valid latest
`codex_exec_finished` before handoff.

### 3.2 Call graph (code-confirmed)

```
LocalController.start_run
  -> start_local_controller_run          # claim, profile bind, destination, start event
  -> runtime.controller_state = starting_initial_codex
  -> LocalController._initial_worker
       -> default_initial_run_executor
            -> execute_initial_direct_codex_run_service
                 parse_prompt_contract / git snapshots / preflight
                 -> execute_codex_direct_service
                 -> apply_post_codex_governance_service
       -> LocalController._automatic_progress_loop
            -> run_supervision_step  (imported as run_supervision_step_service)
                 planner ASK_RUN_PROMPT
                 -> execute_extracted_codex_prompt_service
                      -> agent.cli._run_codex_exec_flow
                           same pre-git + execute_codex_direct_service + governance
```

CLI `codex-run` also enters at `execute_codex_direct_service`.

Shared spawn:

```
execute_codex_direct_service
  -> _resolve_execution_profile          # sandbox must match frozen profile
  -> latest_open_invocation:
       UNCERTAIN -> fail, never spawn
       LIVE/COMPLETE -> observe_existing_codex_invocation
       none -> add_event(codex_exec_started) THEN run_codex_exec(..., json_stream=True)
  -> add_event(codex_exec_finished) unless one exists for that invocation_id
```

Production `run_codex_exec` with `json_stream=True`:

```
run_json_streaming_command
  -> write intent.json
  -> spawn_invocation_wrapper            # python -m agent.codex_invocation_wrapper
  -> wrapper Popen(intent.command)
  -> tail_stdout_and_wait + normalize_codex_jsonl_event
  -> read final_message.md, emit process_exited
```

### 3.3 Files that matter

| Area | Current modules |
|---|---|
| Profile bind / lookup | `agent/run_services.py` (`RunExecutionProfile`, `select_run_execution_profile`, `get_run_execution_profile`, `_execution_profile_from_metadata`, `execution_profile_options`) |
| Profile persistence | `agent/ledger.py` (`bind_run_execution_profile`, `_reconstruct_execution_profile`, `_execution_profile_from_metadata_json`, `RUN_EXECUTION_PROFILE_SCHEMA_VERSION = 1`) |
| Shared execution service | `agent/codex_services.py` (`execute_codex_direct_service`, `reconcile_codex_invocation`, `_reason_code`, `_resolve_execution_profile`) |
| Spawn / argv / stream / observe | `agent/codex_terminal.py` (`run_codex_exec`, `run_json_streaming_command`, `normalize_codex_jsonl_event`, `_final_message_artifact_result`, `observe_existing_codex_invocation`, `terminate_codex_run`) |
| Durable wrapper / artifacts | `agent/codex_invocation.py`, `agent/codex_invocation_wrapper.py` (`run_wrapper`, `spawn_invocation_wrapper`, `artifact_paths_for`, `invocation_artifact_dir`, `build_intent_payload`, `classify_invocation`) |
| Initial coordinator | `agent/initial_codex_run_services.py` |
| Extracted coordinator | `agent/extracted_prompt_services.py` → `agent/cli.py::_run_codex_exec_flow` |
| Governance | `agent/governance_services.py::apply_post_codex_governance_service` |
| Planner | `agent/supervise.py::detect_next_supervise_action` (`_codex_result_stop`) |
| Supervision step | `agent/supervision_services.py::run_supervision_step` |
| ChatGPT transport | `agent/gpt_feedback.py::build_gpt_feedback_message`, `agent/chatgpt_services.py` |
| Sentinel parser | `agent/prompt_extraction.py`, `agent/chatgpt_ax_capture.py` |
| Controller / API | `agent/local_controller.py`, `agent/local_server.py` |
| Dashboard | `agent/web_static/index.html`, `app.js`, `style.css` |
| Kickoff copy (not generated by Python) | `kick_off_prompt_gpt.md`, `instructions_for_dsktp_app.md` |

### 3.4 Load-bearing current contracts

- Profile metadata exact keys: `{schema_version, sandbox, model, reasoning_effort, approval_policy, profile_source}`. Extra keys → `INVALID`.
- Two reconstructors currently share that exact-key rule (see §5).
- Controller-created runs fail closed if the profile is missing. Non-controller CLI runs with no profile still behave as Codex (`CODEX_DEFAULT_SELECTION`).
- Codex argv: `codex exec [--json] -C <repo> (-s <sandbox> | --dangerously-bypass-approvals-and-sandbox) [-m <model>] --output-last-message <final_message.md> <prompt>`.
- `found` today is `shutil.which("codex")`. `observe_existing_codex_invocation` always sets `"found": True` and `codex_path = shutil.which("codex")`.
- Planner stops on `validation_error`, `found is False`, `timed_out`, missing/nonzero `exit_code`. It does **not** inspect `final_message_status`. GPT feedback **does**.
- There is **no stdout fallback** for ChatGPT. Tests pin this.
- Progress writes are non-critical for restore, but a throwing progress callback marks the result `codex_progress_persistence_failed`.
- Wrapper child `Popen` does not set `stdin=DEVNULL`. Leave that Codex behavior alone unless a non-Codex adapter must set stdin via intent for that command to run.
- Dashboard JS still has `quota_wait` / `POST .../quota-resume` residue. **No Python route.** Out of scope.
- `POLICY_MODEL.md` still says the local dashboard does not expose `danger-full-access`. **Code does:** `LOCAL_CONTROLLER_ALLOWED_SANDBOXES` includes it; `agent/local_server.py` can keep it for remote when `--allow-remote-full-access` is on. Follow code.

---

## 4. Target architecture

Same loop, same policy, selectable worker.

```
start API / dashboard
  optional coding_agent (default omitted = Codex)
  same sandbox presets as today
  model options depend on the selected worker
        |
        v
select_run_execution_profile / bind_run_execution_profile
  v1 write: existing six keys, implied Codex
  v2 write: six keys + required coding_agent (non-Codex)
        |
        v
execute_codex_direct_service          # keep name; still the generic owner
  resolve profile (sandbox + coding_agent + model)
  open-invocation reconcile / uncertain fail-closed  # existing
  emit codex_exec_started
        |
        v
  worker adapter (dispatch after profile resolve, around run_codex_exec)
    executable, model, argv, cwd, CRAX preset → CLI flags
        |
        v
  shared durable wrapper             # unchanged command[] runner
        |
        v
  adapter.normalize_stream_line      # optional progress
  after proven exit:
    Codex: read final_message.md as today
    others: write final_message.md from durable stdout, then same read
        |
        v
  shared codex_exec_finished
  shared apply_post_codex_governance_service
  shared planner / ChatGPT sentinel / multi-session
```

Do **not** genericize `local_controller.py`.

### 4.1 Execution profile

`coding_agent` lives on `RunExecutionProfile` and is persisted on
`run_execution_profile_selected`. Allowed values: `codex`, `claude_code`,
`cursor`.

Never infer the worker from `intent.command`. `intent.json` may copy
`coding_agent` as a non-authoritative extra field.

### 4.2 Shared execution service

Keep `execute_codex_direct_service`. After `_resolve_execution_profile`,
dispatch to a backend adapter. Keep `run_codex_exec` as the Codex entry
until the move is proven behavior-compatible.

`reconcile_codex_invocation` must keep calling the same service so restore
and extracted-prompt spawns use the profile worker.

### 4.3 Worker adapters

Each adapter owns only what Codex spawn already owns for Codex:

- executable detection (`found` / `codex_path`-shaped fields; keep key names)
- backend-specific model allowlist (same pattern as
  `ALLOWED_CODEX_MODEL_SELECTIONS`)
- CRAX preset → argv mapping
- argv, cwd, env
- stream-json → existing `CODEX_PROGRESS_ALLOWED_KINDS` when the CLI has JSON
- `final_message.md` producer (native file or synthesis from durable stdout)
- map process outcome onto the **existing** result dict

Adapters must not own planner, ChatGPT, git, run lifecycle, multi-session,
or claims.

### 4.4 Durable wrapper

Unchanged: `command[]`, cwd, identity, stdout/stderr/exit/cancel. No JSON
parser in the wrapper.

If a specific CLI cannot run with inherited stdin, that adapter may request
`stdin=DEVNULL` on **its** child. Do not rewrite the wrapper as a new
process-manager or as a Codex behavior change.

### 4.5 Final message

Codex already writes `final_message.md` via `--output-last-message`.
Claude and Cursor do not. To keep the **same** ChatGPT transport, after
proven process exit and **before** `codex_exec_finished`:

1. if `final_message.md` is already valid, use it
2. else re-parse durable `stdout.log` and atomically write the file
3. then persist `codex_exec_finished` as today

That is compatibility with the current loop, not extra policy.

Each CRAX coding step remains a new process, as Codex `exec` is today.
Continuity stays ChatGPT conversation + repo + ledger. Do not add
provider `--resume` / `--continue` / `persist` because Codex does not use
those in this loop either.

---

## 5. Compatibility strategy

### 5.1 Profile schema

Today `schema_version` is a hard equality check. Adding `coding_agent` to
v1 keys would invalidate every existing profile event. Dual-schema is
required for that mechanical reason, not as a safety gate.

| Case | Rule |
|---|---|
| Old event, no `coding_agent`, `schema_version=1`, exact six keys | New reader reconstructs `coding_agent=codex`. |
| New event, `schema_version=2` | Exact keys = v1 keys + `coding_agent`. Worker ∈ `{codex, claude_code, cursor}`. Model allowlist is worker-dependent, same idea as today’s Codex allowlist. |
| Old binary reading v2 | Stays `INVALID`. Must not spawn a different worker than the one recorded. |
| New binary reading v1 | Accept as Codex. |
| Unknown extra keys / unknown version | Invalid, as today. |
| `sandbox` | CRAX product preset names. Persist the preset, not raw flags. |
| `reasoning_effort` / `approval_policy` | Remain today’s Codex-shaped stored fields. Non-Codex runs persist `codex_default` (today those fields are not passed as Codex argv either). |

**Writes:** Codex dashboard/CLI keep writing schema v1 (six keys) so
rollback of a Codex-only binary still works. Non-Codex starts write schema
v2 with `coding_agent` required.

No SQLite migration. No backfill.

### 5.2 Dual reconstruction sites

The current tree has **two** exact-key reconstructors. Update them together:

- `agent/ledger.py::_execution_profile_from_metadata_json`
- `agent/run_services.py::_execution_profile_from_metadata`
- `agent/ledger.py::bind_run_execution_profile`
- `agent/ledger.py::_reconstruct_execution_profile`
- `agent/ledger.py::AtomicExecutionProfileResult`
- `agent/run_services.py::RunExecutionProfile` and `ExecutionProfileLedger`
- `agent/run_services.py::_execution_profile_metadata`
- `agent/run_services.py::select_run_execution_profile` /
  `get_run_execution_profile`
- `agent/local_controller.py::_profile_summary`
- Fake `bind_run_execution_profile` implementations in tests

### 5.3 Legacy names that stay

Event types, `codex_invocation_id`, `.../codex/...` artifact path,
controller states `starting_initial_codex` / `codex_working`, reason-code
names, and the ChatGPT sentinel. No rename campaign.

---

## 6. Worker capability matrix

Every worker exposes the **same CRAX product abilities Codex has now**.
CLI flag mapping is an adapter implementation detail.

| CRAX ability | `codex` | `claude_code` | `cursor` |
|---|---|---|---|
| Selectable at start | yes (default) | yes | yes |
| `read-only` | current `-s read-only` | map to Claude’s closest unattended inspect/edit-blocked flags | map to Cursor’s closest unattended ask/read flags |
| `workspace-write` | current `-s workspace-write` | map to Claude’s closest unattended edit flags | map to Cursor’s closest unattended edit flags |
| `danger-full-access` | current `--dangerously-bypass-approvals-and-sandbox` plus **existing** confirm / remote gates | map to Claude’s closest unattended full-permission flags, **same CRAX confirm / remote gates** | map to Cursor’s closest unattended full-permission flags, **same CRAX confirm / remote gates** |
| Models | current Codex allowlist | Claude allowlist (`claude_default` omitting `--model`, plus documented aliases such as `sonnet` / `opus` / `haiku`) | Cursor allowlist (`cursor_default` omitting `--model`, plus a small documented set; do not dump the live account catalog into CRAX) |
| Initial + extracted prompts | current | same loop | same loop |
| Restore / cancel / mixed sessions | current | same | same |
| Project context | whatever Codex already loads from the repo | Claude’s normal headless defaults (do **not** force `--bare`) | Cursor’s normal headless defaults (headless binary is `agent`, not `cursor`) |

Post-run enforcement stays **exactly** today’s CRAX governance: git delta,
read-only attributable-write check, observation-heavy workspace-write,
full-access bypass. CRAX already does not re-implement Codex’s sandbox
inside Python; it will not re-implement Claude’s or Cursor’s either.

Closest mapping direction from Audit 3 (implementation may adjust flags to
match the CRAX preset intent, not to add new CRAX policy):

**Claude** (`claude -p`, cwd = repo, stream-json for progress):

| CRAX preset | Intended argv direction |
|---|---|
| `read-only` | `--permission-mode dontAsk --tools Read` (or the current equivalent that matches Codex read-only intent) |
| `workspace-write` | `--permission-mode acceptEdits` (or current equivalent) |
| `danger-full-access` | `--dangerously-skip-permissions` (or current equivalent), only after today’s CRAX full-access confirm |

**Cursor** (executable `agent`, not `cursor`):

| CRAX preset | Intended argv direction |
|---|---|
| `read-only` | `-p --mode=ask` plus whatever Cursor requires to actually start in that repo (e.g. `--trust` if the CLI refuses untrusted workspaces — that is Cursor’s start requirement, analogous to needing the binary on PATH) |
| `workspace-write` | `-p` with Cursor’s unattended apply/write flags (docs: `--force --sandbox enabled`) |
| `danger-full-access` | `-p` with Cursor’s unattended full-approve flags (docs: `--yolo --sandbox disabled`), only after today’s CRAX full-access confirm |

Do not treat Cursor as “Claude with different argv.” Different binary,
different flags, same CRAX presets.

Do not pass `--resume`, `--continue`, `--bg`, `agent persist`, or
`--worktree`. Codex `exec` does not use those; each CRAX step is a new
process in the session repo.

---

## 7. Staged implementation plan

Stages exist so Codex does not break while the seam is built. They are
**not** permission-enablement gates.

### Stage 0 — Characterization / regression locks

**Purpose.** Pin current Codex behavior before moving code.

**Scope.** Tests only. No `agent/` behavior change.

**Files.** `tests/test_codex_invocation.py`, `tests/test_codex_services.py`,
`tests/test_run_services.py`, `tests/test_local_controller.py`,
`tests/test_extracted_prompt_services.py`,
`tests/test_multi_session_restore.py`, optionally
`tests/test_execution_profile_schema.py`.

**Forbidden.** Adapter, schema bump, dashboard worker selector.

**Exit.** Exact-key v1 profile pinned on both reconstructors; extra key is
INVALID; missing controller profile vs CLI default pinned; wrapper/cancel
current behavior pinned; Codex final-message file is not synthesized from
stdout today; existing Codex ratchet green.

### Stage 1 — Dual execution-profile schema reader

**Purpose.** Make `coding_agent` representable without breaking old rows.

**Scope.** Dual reader (v1 ⇒ Codex, v2 requires `coding_agent`). Codex
**writes remain v1**. No spawn change. No second backend.

**Files.** Sites in §5.2.

**Exit.** Codex still writes six keys. v1 reads as Codex. Malformed/unknown
v2 fail closed.

### Stage 2 — Codex adapter seam, Codex-only

**Purpose.** Move current argv/normalize/final-message/`found` logic behind
a dispatch **without changing Codex behavior**.

**Scope.** After `_resolve_execution_profile`, worker is Codex from v1.
New small adapter module holding moved Codex functions. Keep the name
`run_codex_exec`. `observe_existing` still Codex-shaped in this stage;
stop claiming Codex was found if `intent.command` is absent.

**Files.** `agent/codex_services.py`, `agent/codex_terminal.py`, new adapter
module, `agent/codex_invocation.py` (`build_intent_payload` may copy
`coding_agent`).

**Forbidden.** Changing Codex CLI flags. Changing event names. Writing v2
for Codex.

**Exit.** Codex tests + controller/supervision/server ratchet green. Same
`codex exec` argv shape, `--output-last-message`, started/finished pairing.

### Stage 3 — Selectable workers with the same CRAX abilities

**Purpose.** Claude Code and Cursor are usable like Codex: same presets,
same loop, same governance, same ChatGPT contract.

**Scope.**

- `POST /api/runs/start` optional `coding_agent`, default omitted = Codex
  (`additional_session` optional-field pattern).
- `codex` / omitted → v1 profile write, current Codex path.
- `claude_code` / `cursor` → v2 profile write, that adapter, **all three
  CRAX sandboxes** with today’s full-access confirm/remote rules.
- Unknown worker → 400, do not coerce to Codex.
- Invalid model for that worker → same style of validation error Codex
  already uses.
- Missing binary → `found=False` for that worker, do not spawn another.
- Extracted prompt, restore, retry, approval-run all read the profile.
- `observe_existing` uses `intent.command`; `which("codex")` must not
  override a Claude/Cursor command.
- Dashboard: worker selector; sandbox dropdown stays the current three
  presets; model dropdown depends on worker; session cards show worker;
  no control to change worker after start.
- `build_gpt_feedback_message` includes `coding_agent` and does not say
  the worker was Codex when it was not.
- Kickoff/instructions markdown name the worker; sentinel text unchanged.
- CLI `codex-run` may stay Codex-only until the dashboard path works; if a
  worker flag is added, it must go through the same profile, not argv-only.

**Files.** Adapter modules; `agent/codex_services.py`;
`agent/run_services.py`; `agent/ledger.py`; `agent/local_server.py`;
`agent/local_controller.py`; `agent/extracted_prompt_services.py`;
`agent/cli.py::_run_codex_exec_flow`; `agent/gpt_feedback.py`;
`agent/web_static/*`; `kick_off_prompt_gpt.md`;
`instructions_for_dsktp_app.md`.

**Forbidden.** Feature flags. Hiding presets per worker. Mandatory `--bare`.
New governance rules. Sentinel rename. Event rename. Spawning Codex when
the profile is not Codex.

**Exit.** Codex default path unchanged. A Claude run stays Claude through
extracted prompts and restore. A Cursor run stays Cursor. All three CRAX
presets are offered for each worker and rejected only for the same reasons
Codex already rejects (bad value, missing confirm, remote full-access off).
Mixed sessions show the worker. Sentinel parser unchanged.

### Stage 4 — Optional later naming cleanup

Not required. Maybe rename `codex_*` events/paths/sentinels much later.
Default: do not.

---

## 8. Stage-specific test matrix

Compatibility ratchet when touching controller/execution/server:

- `tests/test_local_controller.py`
- `tests/test_supervision_services.py`
- `tests/test_local_server.py`

Also keep green as touched:

- `tests/test_codex_invocation.py`
- `tests/test_codex_services.py`
- `tests/test_initial_codex_run_services.py`
- `tests/test_extracted_prompt_services.py`
- `tests/test_supervise.py`
- `tests/test_governance_services.py`
- `tests/test_run_services.py`
- `tests/test_multi_session_restore.py`
- `tests/test_multi_session_concurrency.py`
- `tests/test_multi_session_start.py`
- `tests/test_multi_session_dashboard.py`
- `tests/test_web_static.py`
- `tests/test_chatgpt_services.py`
- `tests/test_chatgpt_ax_capture.py`

`test_run_services.py` does not currently pin exact-key reconstruction as
dedicated tests. Stage 0 should add that (new module is fine).

### Stage 0

- v1 exact six keys reconstruct (both reconstructors)
- extra key → INVALID
- unknown version → INVALID
- controller missing profile → no spawn
- CLI missing profile → Codex default still allowed
- immutability after `codex_exec_started`
- current wrapper stdin/pgid/cancel
- Codex missing `final_message.md` is not synthesized from stdout today
- sentinel extraction unchanged

### Stage 1

- v1 reads as Codex
- v2 requires `coding_agent`
- Codex starts still write v1
- malformed v2 does not become Codex

### Stage 2

- Codex argv/result unchanged (`codex exec`, `--output-last-message`)
- started then finished on result-shaped failures
- unexpected exception: started, no finished
- extracted prompt and restore still use `execute_codex_direct_service`
- progress optional for correctness

### Stage 3

- omitted `coding_agent` → Codex, v1 write
- `claude_code` / `cursor` write v2 and spawn that CLI
- unknown worker → 400, no Codex spawn
- worker identity survives extracted prompts, restore, retry
- mixed Codex + Claude (or Cursor) sessions isolated (cancel A ≠ kill B)
- each CRAX preset produces the mapped argv for that worker
- danger-full-access still requires today’s confirm / remote gate
- missing binary → `found=False`, not another worker
- Claude/Cursor auth/error fixtures do not become `final_message_status=valid`
- final message synthesized from durable stdout after restart
- `--resume` / `--continue` / `persist` absent from argv
- GPT feedback includes `coding_agent`
- legacy sentinel unchanged
- Codex path still green

---

## 9. API / dashboard scope

Minimal UI. Same presets as today.

**Start** (`POST /api/runs/start`): current required fields unchanged.
Add optional `coding_agent`. Unexpected fields still 400.

- sandbox: current three CRAX presets for every worker
- model: allowlist depends on worker
- full-access: current confirm / `--allow-remote-full-access` behavior,
  independent of which worker runs

**Options:** `coding_agent_options` plus current sandbox options; model
options filtered by agent.

**Read model:** `execution_profile.coding_agent` (implied `codex` on v1);
session list/detail show it; no PATCH to change it.

Dashboard labels may say the selected worker. Controller state ids
(`codex_working`, `starting_initial_codex`) stay.

No raw CLI flags. No per-worker hidden presets.

---

## 10. ChatGPT contract scope

| Item | Rule |
|---|---|
| Sentinel parser | Unchanged. |
| Kickoff / desktop instructions | Operator markdown today. Name the actual worker; keep the sentinel block exactly. |
| Completion report | Include `- coding_agent: <value>`. Do not claim Codex when another worker ran. Same `final_message` transport. |
| Planner | Still `codex_exec_finished` shape. No new protocol. |

---

## 11. Migration and rollback

| Situation | Behavior |
|---|---|
| Old v1 Codex run on new binary | Codex, unchanged. |
| New Codex run | Write v1. Old binary can still run it. |
| New Claude/Cursor run | Write v2. That worker on every spawn. Missing binary → `found=False`. |
| Downgrade while a v2 run exists | Old binary sees INVALID profile. Do not spawn Codex. Resume after upgrade. |
| Historical replay | v1 stays. v2 needs the dual reader. |
| Partial invocation during upgrade | Profile + `intent.command` + artifacts. Uncertain never replayed. |
| Controller restart | Snapshot is not worker authority. Profile + artifacts win. |

No SQLite migration. No rewriting Claude/Cursor history into Codex.

---

## 12. Risks (implementation, not new policy)

| Risk | Why it matters | What to do |
|---|---|---|
| Worker only passed as an API argument | Extracted prompts would spawn Codex | Store `coding_agent` on the profile; every spawn reads it |
| Extra key on v1 profile | Current exact-key reader marks INVALID | Dual schema; Codex keeps writing v1 |
| Claude/Cursor have no `--output-last-message` | ChatGPT submit would fail | Synthesize `final_message.md` from durable stdout before finish event |
| `observe_existing` always `which("codex")` | Wrong `found` metadata | Trust `intent.command` |
| `_run_codex_exec_flow` bypasses dispatch | Extracted path stays Codex | Stay on `execute_codex_direct_service` |
| Auth JSON looks like an assistant answer | Today already forbids stdout fallback | Do not mark that text `final_message_status=valid` |

Do not mitigate these by shrinking Claude/Cursor abilities below Codex.

---

## 13. Explicit non-goals

- No ChatGPT replacement
- No Claude Desktop or Cursor IDE automation
- No new CRAX permission model
- No extra worker-specific CRAX safety vs today’s Codex rules
- No feature flag to hide Claude or Cursor
- No mandatory `--bare` / project-config jail
- No live-probe enablement gate
- No generic event/sentinel/path rename in this program
- No repo locking
- No automatic worker failover
- No mid-run worker switching
- No raw backend CLI flags in the dashboard
- No controller-as-orchestrator rewrite
- No wrapper JSON parser
- No SQLite migration
- No changing workspace-write from observation-heavy to hard enforcement
- No nested `backend_config` blob
- No quota-resume Python contract

---

## 14. Operator decisions still open

1. Exact model strings in the Claude and Cursor allowlists.
2. Whether CLI `codex-run` grows a worker flag once the dashboard path works.
3. Whether Codex ever cuts over to writing schema v2.
4. Precise Claude/Cursor flag strings if the vendor CLIs change; the CRAX
   preset names do not change.

Settled: same CRAX abilities as Codex for every selectable worker; no extra
CRAX safety program; ChatGPT remains planner; sentinel and `codex_*`
contracts stay.

---

## 15. Definition of done

- Codex remains the default and regression-free
- `coding_agent` is durable on the execution profile
- Claude Code and Cursor are selectable per run
- Each worker has `read-only`, `workspace-write`, and `danger-full-access`
  under **today’s** CRAX gates
- Worker identity survives restart and extracted prompts
- Mixed sessions work and show the worker
- ChatGPT still uses the old sentinel
- Dashboard/ChatGPT prose name the worker that ran
- No silent fallback to a different worker

Success is not “generic naming cleaned up” and not “stricter than Codex.”

---

## Implementation notes for a future stage agent

1. Read this whole document, then the named stage, then the files in that
   stage’s table.
2. Do not implement a later stage’s work.
3. Do not add safety or restrictions that Codex does not already have.
4. Characterize before changing (`AGENTS.md`).
5. Do not invoke Codex from inside Codex. Do not perform live desktop
   automation unless the operator explicitly asks.
6. Authoritative tree is `agent/`. Do not edit `build/lib/agent`.
7. Commands in docs use literal ASCII flags such as `--help` and
   `--confirm-run`.

```
execute_codex_direct_service
  -> adapter (argv / stream / final-message / existing result dict)
  -> shared durable wrapper
  -> shared codex_exec_* events
  -> shared git/governance/planner
```

---

## Amendment log

- 2026-09-20: Initial Scope of Work from Audits 1–4 (included extra
  Claude/Cursor CRAX gates).
- 2026-09-21: Operator override. Extra CRAX safety/gates removed. Product
  rule is current Codex abilities for every selectable worker. Stages
  collapsed to seam work plus enabling Claude Code and Cursor. Audit 4
  extra restrictions (read-only-only, Cursor deferred, `--bare`, feature
  flags, live-probe enablement) are not in this plan.
