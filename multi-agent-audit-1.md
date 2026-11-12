# Multi-agent Audit 1: Current Codex Execution Contract

This is a read-only audit of the **existing Codex execution contract**. No interface is proposed. Claims below are from current `agent/` code and tests unless marked as documentation-only.

**Scope:** Audit 1 of a multi-step investigation into adding multiple coding-agent backends to CRAX. ChatGPT Desktop remains the planner/reviewer; CRAX remains the supervisor/transport/state machine. This audit does not decide an abstraction.

---

## 1. Documentation findings

There is **no documented multi-agent architecture**. The docs describe a Codex-only worker.

| Document | What it actually says about execution |
|---|---|
| `AGENTS.md` | Codex execution is a risky seam. Do not nest Codex. `agent/` is source of truth. |
| `ARCHITECTURE.md` | Product loop is “Codex works in a repo, results go to ChatGPT.” Codex is named as an **adapter module**, but that is a modular-monolith *target*, not a pluggable backend. |
| `README.md` | Requires Codex CLI as `codex`. Loop is ChatGPT → Codex → ChatGPT. |
| `POLICY_MODEL.md` | Post-Codex governance is the policy owner. Objective Codex failures force review. Prompt-contract path-safety can preflight-fail spawn. Workspace-write is observation-heavy. `danger-full-access` is gated. |
| `STATE_MACHINE.md` | Run status `completed` is the **mid-loop Codex-finished** value, not loop-finished. Planner requires a valid latest Codex result before handoff. |
| `DIAGNOSTICS_VS_PRODUCTION.md` | Production includes “Running Codex through the Codex CLI adapter,” git/governance, ChatGPT feedback from Codex output, sentinel extraction. |
| `MULTI_SESSION_CONTEXT.md` | Stage 1 durable invocation + restart reconciliation is the execution source of truth. Uncertain Codex is never replayed. Codex may overlap across sessions; ChatGPT is serialized. |
| `MULTI_SESSION_IMPLEMENTATION_PLAN.md` | Stage 1 “Durable Codex execution” is **Done**. Invariant 5: never replay a Codex execution unless it provably did not happen. |

**Doc vs code mismatch (relevant):** `POLICY_MODEL.md` says the dashboard does not expose `danger-full-access`. Code does: `LOCAL_CONTROLLER_ALLOWED_SANDBOXES` includes it, and `agent/local_server.py` can keep it when full-access is allowed (`--allow-remote-full-access`). Local default still hides it.

**Quota UI is documented as unknown, and is not implemented in Python.** `MULTI_SESSION_CONTEXT.md` marks Codex quota `[U]`. Dashboard JS reads `model.quota_wait` and POSTs `/api/runs/.../quota-resume`, but **no Python module defines `quota_wait` or that route**. That is dashboard residue, not current execution contract.

---

## 2. End-to-end execution flow (concrete functions)

Two entry points, one spawn primitive.

### Initial invocation (dashboard / controller)

```
LocalController.start_run
  -> start_local_controller_run          # run row, conversation claim, destination, execution profile
  -> runtime.controller_state = starting_initial_codex
  -> worker: LocalController._initial_worker
       -> default_initial_run_executor
            -> execute_initial_direct_codex_run_service
                 1. parse_prompt_contract
                 2. capture_git_snapshot / capture_invocation_git_state
                 3. events: git_snapshot_before_codex, prompt_contract_parsed, invocation_git_state_before
                 4. preflight: invalid contract paths; danger-full-access needs confirm
                 5. execute_codex_direct_service
                 6. apply_post_codex_governance_service
       -> if ok: _automatic_progress_loop   # planner takes over (send to GPT, etc.)
```

### Subsequent extracted-prompt invocation

```
_automatic_progress_loop
  -> run_supervision_step_service
       planner detect_next_supervise_action == ASK_RUN_PROMPT
       -> execute_extracted_codex_prompt_service
            gates: continuation, repo, sandbox, full-access, prompt identity
            events: extracted_codex_prompt_selected, extracted_codex_prompt_run_started
            -> _run_codex_exec_flow (agent/cli.py)
                 same pre-git + execute_codex_direct_service + apply_post_codex_governance_service
            -> extracted_codex_prompt_run_finished
```

### Shared spawn primitive

```
execute_codex_direct_service
  -> resolve execution profile (sandbox/model)
  -> latest_open_invocation:
       UNCERTAIN -> fail, never spawn
       LIVE/COMPLETE -> observe_existing_codex_invocation (no new process)
       none -> add_event(codex_exec_started) THEN run_codex_exec(..., json_stream=True)
  -> add_event(codex_exec_finished) unless one already exists for that invocation_id
```

Production `run_codex_exec` with `json_stream=True` does **not** call Codex directly. It:

```
run_json_streaming_command
  -> write intent.json
  -> spawn_invocation_wrapper   # python -m agent.codex_invocation_wrapper --manifest ...
  -> wrapper Popen(intent.command) with stdout/stderr files
  -> tail_stdout_and_wait + normalize_codex_jsonl_event
  -> read final_message.md, emit process_exited
```

Planner then treats `codex_exec_finished` as the completion token. If exit is clean, next action is `ask_send_to_gpt`. Feedback is built from that event’s `final_message`, not stdout.

---

## 3. Invocation input contract

### Required before spawn (code-confirmed)

| Input | Source | Generic vs Codex-specific |
|---|---|---|
| `run_id` | run row | generic |
| prompt text | initial: `user_instruction` / start form; subsequent: sentinel-extracted `prompt_text` + sha256 | generic text; extraction sentinels are Codex-named |
| `repo_path` | start validation / read model | generic |
| `sandbox` | execution profile + request; must match profile | product policy, encoded as Codex CLI `-s` |
| `model` | execution profile (`codex_default`, `gpt-5`, `gpt-5-codex`) | Codex-specific allowlist |
| `reasoning_effort`, `approval_policy` | profile, currently locked to `codex_default` (approval may be `never`) | Codex profile fields; **not passed as argv** |
| `prompt_contract` | `parse_prompt_contract(prompt, sandbox)` | generic policy over prompt text |
| `run_id`-scoped `codex_invocation_id` | generated as `codex-invocation-{hex}` when progress is enabled | naming Codex-specific; id is generic |
| cwd | resolved repo path | generic |
| environment | wrapper copies `os.environ` + `PYTHONPATH` + `PYTHONUNBUFFERED=1` | generic inheritance; Codex auth is ambient |
| output paths | `data/runs/<run>/codex/<invocation_id>/` | path segment `codex/` is naming |
| git baseline | `git_snapshot_before_codex`, `invocation_git_state_before` | generic |
| controller action | initial: `initial_codex`; later: planner `ask_run_prompt` | naming |
| conversation claim | required before `ask_run_prompt` (not before initial Codex) | supervisor, ChatGPT-identity, not worker |
| timeout | **always discarded** (`del timeout_seconds`); `LOCAL_CONTROLLER_INITIAL_CODEX_TIMEOUT_SECONDS = None` | no live timeout contract |

### Pre-spawn policy that can block launch

- Invalid prompt-contract paths → `validation_error`, **no process** (except `danger-full-access`, which skips that path-safety preflight).
- `danger-full-access` without confirm → validation error.
- Extracted path additionally: `can_continue_run` must allow (`completed` or `approved`); supervise/extracted refuses `danger-full-access` unless `allow_full_access`; prompt sha/event/method must still match approval.
- Controller-created runs **must** have a durable execution profile; sandbox must match it.
- Open invocation `STATUS_UNCERTAIN` → no spawn.
- Ledger durability block → no new Codex start.
- Dirty git tree is **recorded, not a spawn blocker**.

### Not required / not used at spawn

- ChatGPT UI lease (Codex does not use the desktop).
- Timeout.
- Reasoning/approval CLI flags (explicitly omitted; tests pin this).
- Quota resume (no Python contract).

---

## 4. Exact Codex CLI / spawn contract

Production argv construction is in `run_codex_exec`:

```
codex exec [--json] -C <resolved_repo>
  either --dangerously-bypass-approvals-and-sandbox
  or     -s <read-only|workspace-write>
  optional -m <model>   # omitted when model is codex_default
  --output-last-message <final_message.md>
  <prompt as last positional>
```

`shutil.which("codex")` is used only for `found` / `codex_path`. The argv still starts with the literal `"codex"`, not the resolved path.

| Piece | Classification |
|---|---|
| executable `codex`, subcommand `exec` | pure Codex CLI syntax |
| `--json` when `json_stream` | Codex CLI syntax; production always on when ledger can persist progress |
| `-C repo` | Codex CLI for workspace; semantic requirement is “run in this git repo” |
| `-s sandbox` | Codex CLI encoding of CRAX permission presets |
| `--dangerously-bypass-approvals-and-sandbox` | Codex CLI encoding of CRAX `danger-full-access` |
| `-m model` | Codex CLI; CRAX allowlist is product policy |
| `--output-last-message path` | Codex CLI; **load-bearing** for ChatGPT feedback |
| prompt as last argv | Codex CLI; ARG_MAX is an implicit risk |
| no timeout on the subprocess | product policy (tests: numeric timeout is ignored) |
| env: inherited + PYTHONPATH/UNBUFFERED | wrapper, not Codex flags |

Non-streaming path (`json_stream=False`) uses `subprocess.run` with `capture_output=True` and **no durable wrapper**. Production controller/CLI ledger always has `add_codex_progress_event`, so production is the JSON/wrapper path.

---

## 5. Durable invocation architecture — how generic it really is

Artifacts under `data/runs/<safe_run_id>/codex/<invocation_id>/`:

| File | What it contains | Classification |
|---|---|---|
| `intent.json` | command list, cwd, prompt, sha, sandbox, model, json_mode, paths, optional extraction ids, `state: spawn_pending` | **generic subprocess intent**, Codex field names (`codex_invocation_id`) |
| `identity.json` | wrapper pid, pgid, boot_id, start_identity, invocation id | **generic process identity**, Codex key name |
| `stdout.log` / `stderr.log` | child byte streams | generic |
| `final_message.md` | Codex last assistant message | **Codex-specific semantics** (CLI writes it because `--output-last-message` is in argv; wrapper never writes it) |
| `exit.json` | `exit_code`, `signaled`, `wait_status`, `finished_at`, `codex_invocation_id` | generic exit evidence, Codex field name |
| `cancel_requested.json` | source + timestamp | generic cancel intent, Codex id field |
| `progress_persistence_failed.json` | ledger progress write failed | generic telemetry failure |

Wrapper process (`agent.codex_invocation_wrapper`):

- Reads `intent.command` as a list and `Popen`s it with `cwd`.
- Redirects stdout/stderr to files.
- Writes identity **of the wrapper**, not of Codex.
- Forwards SIGINT/SIGTERM to the child.
- On `FileNotFoundError` writes exit 127.
- **Does not read `sandbox`, `model`, or `json_mode`.**
- **Does not parse JSONL.**
- Wrapper is spawned with `start_new_session=True`; child is not, so child is in the wrapper’s process group.

**Could this wrapper run Claude/Cursor unchanged if handed a different `intent.command`?**

**Process lifecycle: yes.** `run_wrapper` only requires `command: list`, paths, and cwd. That is proven by the wrapper code.

**Hidden Codex assumptions that are not in argv:**

1. Artifact directory is discovered as `.../codex/<id>` (`disk_invocation_ids`).
2. Live-process search is `pgrep -f <invocation_id>` **and** command line must contain `agent.codex_invocation_wrapper`.
3. Identity match requires key `codex_invocation_id`.
4. `final_message.md` is assumed to appear because Codex was asked to write it. Another CLI that does not write that file still “completes” the wrapper, but CRAX will treat the invocation as failed/unsubmittable.
5. Cancel assumes the worker stays in the wrapper process group. A CLI that daemonizes or `setsid`s grandchildren can outlive SIGTERM/SIGKILL of the wrapper group.
6. JSONL progress is a CRAX observer assumption on stdout, not a wrapper assumption.

Active registry `_ACTIVE_CODEX_PROCESSES` is keyed by **`run_id`**, one live invocation per run. That is generic supervisor policy, Codex-named.

---

## 6. Raw Codex stream → normalized progress

Stdout is tailed line-by-line. Each non-empty line goes to `normalize_codex_jsonl_event`.

**Observed expected raw shapes (from parser + tests, not a full Codex schema spec):**

- JSON object per line.
- Type keys tried: `type`, `event_type`, `event`, `kind`; or nested `item.type` / `item.kind`.
- Assistant commentary **only** if `item.type == "agent_message"` and `item.text` exists. Test fixture: `{"type":"item.completed","item":{"type":"agent_message","text":"..."}}`.
- Commands: presence of `command`/`cmd`, or type string containing `command`/`exec`. Test fixture uses `item.type == "command_execution"`.
- Tools: `tool_name`/`tool` or `"tool"` in type.
- File changes: type contains `file`+`change` or `patch`, or lists `files`/`paths`/`changed_files`.
- Errors/blocked: type string heuristics (`error`, `failed`, `blocked`, …).
- Malformed JSON → kind `codex_json_event`, status `malformed`; line is skipped, process continues.
- Non-object JSON → `codex_json_event` / `observed`.
- Raw command output is **not** copied into progress (test pins `aggregated_output` is dropped).

**Normalized kinds** (`CODEX_PROGRESS_ALLOWED_KINDS`):

| Kind | Origin | Neutral? |
|---|---|---|
| `process_started` | CRAX, at wrapper spawn | structurally generic; titles say Codex |
| `codex_json_event` | fallback / malformed | Codex-named catch-all |
| `assistant_commentary` | Codex `agent_message` | kind is generic; **schema is Codex-specific** |
| `command_started` / `command_finished` | Codex command/exec heuristics | kind generic; schema Codex-specific |
| `tool_event` | Codex tool heuristics | same |
| `file_change_summary` | Codex file/patch heuristics | same |
| `final_message_available` | CRAX after reading `final_message.md` | generic artifact check |
| `process_exited` | CRAX after wait | generic |
| `error` / `blocked` | heuristics on event type | generic kinds, Codex type strings |

Progress is stored as ledger events of type `codex_progress_event`. **This write is non-critical.** Failure sets `progress_persistence_failed.json` and can make `_reason_code` = `codex_progress_persistence_failed` even if Codex succeeded. Execution source of truth remains `codex_exec_started` / `codex_exec_finished` + disk artifacts.

Dashboard extra: JS looks for `metadata.event_type === "thread.started"` and `value_summary.codex_session_id`, and `item_type === "todo_list"` + `plan_items`. **Python `_json_value_summary` does not populate `codex_session_id` or `plan_items`.** So session-id/plan UI is Codex-schema-dependent and currently under-specified in the Python normalizer.

**Observed minimum output a future worker must satisfy for the rest of CRAX not to care:**

1. A subprocess that exits with a durable `exit_code`.
2. A UTF-8 non-empty `final_message.md` (or equivalent field CRAX already reads from the finished event).
3. Optional JSONL on stdout **only if** we still want live dashboard commentary; the planner/governance/ChatGPT path does **not** read progress kinds.
4. Git-visible workspace changes, because governance attributes by git delta, not by tool events.

---

## 7. Final-result contract and downstream consumers

`execute_codex_direct_service` persists the **raw process dict** as `codex_exec_finished` metadata.

`_reason_code` success is only `codex_exec_completed` when:

- no `validation_error`
- `found` is not false
- not `timed_out`
- not `progress_persistence_failed`
- `exit_code` is int 0
- `final_message_status` missing or `valid`
- `termination_reason` not `uncertain`
- `invocation_complete` is not false

`CodexDirectExecutionResult.ok` is exactly `reason == "codex_exec_completed"`.

**Load-bearing finished-event fields:**

| Field | Who reads it |
|---|---|
| `exit_code` | planner stop, status policy, diagnostics, gpt_feedback, extracted `_exit_code_from_flow` |
| `timed_out` | same |
| `found` | planner, status policy |
| `validation_error` | planner, governance (skips post-git if set), status policy |
| `sandbox` | planner display / plan |
| `final_message` + `final_message_status` | **gpt_feedback — no stdout fallback** |
| `codex_invocation_id` | idempotent finished-event reuse, uncertain markers |
| `stdout` / `stderr` | stored, hashed on the result object; **not submitted to ChatGPT** |
| `command` | recorded; not planner-critical |
| `invocation_complete` / `termination_reason` / `cancel_requested` | reason codes / cancel |

Planner `_codex_result_stop` **stops the loop** (does not send to ChatGPT) on validation error, not found, timeout, missing exit, nonzero exit. It does **not** stop on missing final message. Missing final message fails later in `build_gpt_feedback_message` with `codex_final_message_unavailable`.

There is **no stdout-derived fallback** for the assistant message. Tests in `test_supervise.py` pin that.

`timed_out=True` exists as a result shape, but production never sets a subprocess timeout. It is a historical/CLI-shaped field.

---

## 8. Git and governance boundary

**Before:** git snapshot + invocation git state. Dirty tree is evidence, not a block.

**After (skipped if `validation_error`):**

- `git_snapshot_after_codex`
- `invocation_git_state_after`
- `invocation_delta_attributed` (attributable vs preexisting dirty)
- `changed_file_classification`
- `prompt_repo_impact_diagnostics` (prompt text + git/classification + exit/timeout)
- `supervision_decision` (risk policy, observation-heavy)
- `run_governance_observation` (read-only writes, secrets, invalid paths)
- workspace-write post-run verify (often `allowed=True` even for concerning cases)
- `run_status_transition`

Governance **does not inspect Codex JSONL**. It cares that *some process* ran in a repo and git moved. Codex-specific residue:

- event names `*_codex*`
- diagnostics flags named `codex_ended_uncleanly_with_changes`, `timed_out_with_no_changes`
- objective failures keyed off Codex result dict (`found`, `validation_error`, `exit_code`)
- read-only sandbox + attributable writes → governance objective failure (this is **sandbox policy**, not Codex JSON)

Handoff auto-safety (`send_plan_auto_safe`): read-only + zero changed files auto-submits; workspace-write needs post-run `allowed=True`; changed files in read-only block auto-submit. That is git/sandbox policy.

---

## 9. Restart / reconciliation contract

`classify_invocation` statuses:

| Status | Meaning | Next action |
|---|---|---|
| `already_finished` | ledger already has `codex_exec_finished` | do not spawn |
| `never_spawned` | no start event, intent, or identity | may spawn (treated as no open item) |
| `live` | identity matches live wrapper, or argv live before identity | **observe**, do not spawn |
| `complete` | wrapper gone, `exit.json` has `exit_code`, stdout+stderr files exist | **finalize from artifacts**, do not spawn |
| `uncertain` | start evidence, completion unproven (truncated exit, pid mismatch, spawn_pending with no process, etc.) | **block, never replay** |

**Duplicate-execution invariant (code-confirmed):**

> If there is start evidence (`codex_exec_started` **or** `intent.json` **or** `identity.json`) and completion is not proven, CRAX will not start another process. Uncertain is not retry.

Cases:

- **Still running after controller restart:** restore sees `LIVE` → resume worker calls `reconcile_codex_invocation` → `observe_existing_codex_invocation` tails until `exit.json` complete → writes `codex_exec_finished` if missing.
- **Completed while controller down:** `COMPLETE` → finalize from artifacts; finished-event write is idempotent per `codex_invocation_id`.
- **Start recorded, spawn uncertain:** `UNCERTAIN` → `codex_invocation_uncertain` event; session blocked; retry of initial Codex also refuses.
- **Wrapper alive / dead:** identity is wrapper pid+boot+start+pgid. Recycled pid with different start identity is **not** adopted (test pins this) and is not signalled.
- **Missing/truncated exit:** uncertain.
- **Extracted prompt persisted, Codex not launched:** restore test `test_extracted_prompt_with_no_codex_start_launches_once` — planner `ask_run_prompt` launches once under Stage 1 rules.
- **Codex finished, extracted wrapper `*_run_finished` missing:** `_repair_extracted_run_finished` synthesizes the wrapper event from existing `codex_exec_finished`; **does not replay**.
- **Cancel racing completion:** cancel marker is written; result `termination_reason=operator_cancelled`; if exit still appears, finished event is still written from artifacts. Run status is set `failed` by controller cancel.

Progress events are **not** required for reconciliation.

---

## 10. Initial vs subsequent invocation

| | Initial | Extracted |
|---|---|---|
| Prompt | start `initial_instruction` / `user_instruction` | `BEGIN_NEXT_CODEX_PROMPT` extraction |
| Extra events | `local_controller_run_started`, git/contract, `codex_exec_*` | plus `extracted_codex_prompt_selected/run_started/run_finished` |
| Coordinator | `execute_initial_direct_codex_run_service` | `_run_codex_exec_flow` |
| Continuation gate | new run, status `created`→running via governance | `can_continue_run` must be completed/approved |
| Conversation claim | claimed at start, but not rechecked before initial Codex | **required** before `ask_run_prompt` |
| Repair path | open invocation observe/uncertain | unpaired extracted start + missing wrapper finished |
| Spawn primitive | **same** `execute_codex_direct_service` → `run_codex_exec` | **same** |
| Post-run | **same** `apply_post_codex_governance_service` | **same** |

Both ultimately converge on one Codex execution primitive. Anything that must stay compatible if the worker changes is that primitive’s result dict + git/governance events + `codex_exec_finished`. Extracted wrapper events are an extra idempotency layer around that primitive.

---

## 11. Cancel / shutdown contract

Operator cancel (`LocalController.request_cancel`):

1. Set per-run `cancel_requested`.
2. Block controller state; drop pending approval.
3. `terminate_codex_run(run_id)`:
   - if in-process registry has a live wrapper: write `cancel_requested.json`, SIGTERM process group, then SIGKILL after 2s;
   - else classify latest disk invocation and `terminate_verified_identity` only if identity still matches.
4. Ledger: `local_controller_cancel_requested`.
5. Run status → `failed` (“Run cancelled by operator.”).
6. Release conversation claim.

Shutdown: `install_codex_shutdown_handlers` + `atexit` → `terminate_all_active_codex_invocations`. Tests pin SIGTERM of parent does not leave a live Codex wrapper.

**Behavioral contract a non-Codex worker must satisfy:**

- One killable process group per `run_id`.
- Cancel is persisted on disk before/during signal.
- Identity is verified before signalling (no pid-reuse kills).
- After cancel, CRAX may still observe artifacts and write `codex_exec_finished`; it must not start a replacement process for that invocation.
- Planner/dashboard see a failed/blocked session, not a new Codex launch.

---

## 12. Tests as executable contract, and gaps

**Strongest pins**

`tests/test_codex_invocation.py` — Stage 1 durability:

- success writes artifacts + progress
- nonzero exit persists `exit.json`
- JSONL arrives before exit
- cancel signals wrapper group and keeps cancel file
- live recovery does not duplicate wrapper
- child completion before controller finalize is idempotent
- truncated artifacts → uncertain
- pid reuse is not adopted or signalled
- progress persistence failure is observable
- spawn_pending without process never reruns
- `codex_exec_started` failure prevents spawn
- controller death during output recovers without duplicate spawn
- SIGTERM / intentional shutdown do not leave live Codex

`tests/test_codex_services.py` — argv + service boundary:

- `agent_message` → bounded commentary; command output not leaked
- full-access flag mapping
- `--output-last-message` path
- timeout ignored
- model flag only when not default; no reasoning/approval argv
- JSON stream incremental parse + final file
- non-streaming command shape
- execution profile default/explicit/mismatch
- started then finished even on result-shaped failures
- unexpected runner exception: started, **no finished**

`tests/test_initial_codex_run_services.py` — event order, preflight validation still runs governance, no ChatGPT coupling.

`tests/test_extracted_prompt_services.py` — wrapper event order, approval freshness, sandbox/full-access blocks, missing wrapper finished repaired without replay.

`tests/test_governance_services.py` / `test_supervise.py` — git attribution, dirty tree, objective failures, planner stops, feedback uses final message not stdout.

`tests/test_multi_session_restore.py` — live/complete/uncertain Codex restore, extracted-with-no-start launches once, durability block prevents new Codex.

**Important behavior with weak or no characterization**

- Live Codex `--json` event schema beyond `item.completed` + `agent_message` / `command_execution`.
- `thread.started` / `todo_list` / `codex_session_id` (dashboard expects them; Python does not produce them).
- Quota wait / quota-resume (JS only).
- ARG_MAX / huge prompts on argv.
- Codex grandchildren that leave the wrapper process group.
- Auth/login failure vs missing binary (`found` is only `which("codex")`).
- `final_message` written after process exit vs missing file on exit 0.
- Concurrent two Codex CLIs sharing auth (docs mark `[U]`).
- Non-streaming production path (exists in code, not used by controller).

---

## 13. Naming vs semantic coupling

| Concept | Class |
|---|---|
| `codex exec` argv, `--json`, `-s`, `--dangerously-bypass-approvals-and-sandbox`, `-m`, `--output-last-message` | **1 semantic** |
| `shutil.which("codex")` / `found` | **1 semantic** |
| JSONL `agent_message` / command_execution heuristics | **1 semantic** |
| `final_message.md` as ChatGPT payload | **1 semantic** (artifact idea could be generic; current producer is Codex CLI) |
| Durable wrapper executing `intent.command` | **2 compatibility naming** (behavior already command-generic) |
| `codex_exec_started` / `codex_exec_finished` | **2** for structure (start/finish pair is generic); **1** for every consumer keying the string |
| `codex_progress_event` kinds except `codex_json_event` | **2** structurally; parser is **1** |
| `codex_json_event` | **1** catch-all tied to Codex JSON |
| `codex_invocation_uncertain` | **2** (the invariant is generic “do not replay uncertain spawn”) |
| `next_codex_prompt_extracted` / `BEGIN_NEXT_CODEX_PROMPT` | **1** for ChatGPT contract; **4** for whether ChatGPT would accept another name — later audit |
| `extracted_codex_prompt_*` | **2** wrapper around the same primitive |
| `data/runs/.../codex/` | **2** path naming |
| `starting_initial_codex`, action `initial_codex` | **2** / **3** |
| Dashboard “Codex Working Notes”, model labels | **3** cosmetic, plus **1** for session-id/plan/quota JS |
| `quota_wait` / quota-resume | **4 unknown** — UI without Python |
| Execution profile model allowlist | **1** |
| Git/governance event names `*_codex*` | **2** naming; logic is git |

Classification key:

1. **semantic coupling** — changing backend requires logic change
2. **compatibility naming** — name says Codex but behavior could already carry another worker
3. **cosmetic/UI naming**
4. **unknown / later audit**

Do not rename any of these as part of this investigation.

---

## 14. Observed behavioral contract CRAX currently expects from its coding worker

Derived only from current code/tests, not an aspirational API.

**Worker-specific adapter behavior (today: Codex CLI)**

- Be the executable named `codex`, subcommand `exec`.
- Honor CRAX sandbox presets via Codex flags.
- Honor optional `-m`.
- Accept the whole prompt as the last argv token.
- Write the last assistant message to `--output-last-message`.
- With `--json`, emit newline-delimited JSON objects on stdout, including `item.type == "agent_message"` for commentary.
- Exit 0 on success; non-zero / missing exit / not-found is an objective failure.
- Stay in the wrapper process group so cancel works.
- Inherit ambient user auth from the environment/home.

**Generic durable subprocess execution (already mostly generic)**

- Write intent before spawn.
- Spawn a wrapper in a new session, persist pid/boot/start/pgid.
- Capture stdout/stderr/exit on disk.
- Tail stdout until `exit.json` is complete.
- Classify live / complete / uncertain.
- Never spawn a second process when start is recorded and completion is unproven.
- Finalize idempotently from artifacts after crash.
- Cancel only a verified identity; persist cancel intent.

**CRAX supervisor**

- Bind one immutable execution profile per run.
- One live invocation per `run_id`.
- Drive initial vs extracted from planner state, not from Codex.
- After success, enter `_automatic_progress_loop`.
- On uncertain/failure, pause; do not guess.
- Require conversation claim before extracted re-run (ChatGPT identity), not before initial worker spawn.
- Block new worker starts when the ledger is untrustworthy.

**Git / governance**

- Snapshot before and after.
- Attribute dirty-tree vs this invocation.
- Classify files, run diagnostics on prompt+delta+exit, decide continue/record/review/approval.
- Objective process failure → review-oriented status.
- ChatGPT auto-submit gated on sandbox + changed-file count / workspace-write post-run.

**Planner / ChatGPT loop**

- Treat latest `codex_exec_finished` as “worker step done.”
- Stop if that result is not a clean exit.
- Build feedback **only** from `final_message` plus run metadata and changed files.
- Paste/capture/extract sentinels; next worker prompt is whatever is inside `BEGIN_NEXT_CODEX_PROMPT`.
- Do not read JSONL progress to decide the loop.

The rest of CRAX does **not** currently require a Codex session id, a plan list, or quota handling to advance the loop. Those are dashboard overlays, and quota is not even wired in Python.

---

## 15. Highest-risk areas for Audit 2

1. **`codex_exec_finished` as a globally load-bearing event type** — planner, continuation, feedback, extracted repair, profile immutability (`execution_profile_immutable_after_codex_exec_started`). Changing the worker without this event breaks the loop even if git/governance is generic.
2. **Final-message artifact is the ChatGPT transport.** Any backend that only prints to stdout will be “done” to the wrapper and “unsubmittable” to GPT.
3. **JSONL commentary schema is Codex-shaped and thinly tested.** Dashboard session/plan fields are even less grounded.
4. **Sandbox flags are product policy, not portable.** Claude/Cursor permission models will not match `-s` / `--dangerously-bypass-approvals-and-sandbox` without a later mapping audit.
5. **Prompt-on-argv** and **process-group grandchildren** — silent production hazards for any other CLI.
6. **Extracted-prompt wrapper vs initial path** — two coordinators (`execute_initial_direct_codex_run_service` vs `_run_codex_exec_flow`) already diverge slightly; a second backend will hit both.
7. **ChatGPT-facing contract** (sentinels, “this is Codex” kickoff, `next_codex_prompt_extracted`) — out of scope here, but it is the other half of “being the worker.”
8. **Quota/rate-limit** — UI exists, Python does not; unknown whether live Codex JSON even emits it in this tree.
9. **`found: which("codex")`** — auth/login failures are not distinguished from missing binary.

Audit 2 should stay observational: map **every consumer of `codex_exec_*`, final_message, sandbox flags, and JSONL kinds** before anyone sketches an adapter.
