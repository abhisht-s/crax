# CRAX Multi-Coding-Agent Investigation — Audit 2

Audit 2 is observational only. There is no multi-coding-agent design in the docs or code. The question is compatibility: if another headless worker kept the existing Codex-named events and sentinels, how much of CRAX would actually have to change?

Do not treat this document as an implementation plan or an adapter design.

---

## 1. Documentation findings

There is no documented multi-agent architecture. Every root doc treats **ChatGPT as planner/reviewer** and **Codex CLI as the only coding worker**.

| Doc | What it actually says |
|---|---|
| `AGENTS.md` | Operating rules. Authoritative tree is `agent/`. Do not invent architecture. |
| `ARCHITECTURE.md` | Modular monolith with a Codex adapter. Target is still Codex, git, ChatGPT, diagnostics. |
| `README.md` | Product loop is ChatGPT Desktop → Codex → paste back. Setup requires `codex` on PATH. |
| `POLICY_MODEL.md` | Post-Codex governance. Sandbox presets are Codex values. Workspace-write is observation-heavy. Danger-full-access is confirmation-gated and not on the local dashboard. |
| `STATE_MACHINE.md` | Planner waits on `codex_exec_finished`. Continuation from `created` is denied because “initial Codex has not run.” |
| `DIAGNOSTICS_VS_PRODUCTION.md` | Production includes “running Codex through the Codex CLI adapter” and extracting `BEGIN_NEXT_CODEX_PROMPT`. |
| `MULTI_SESSION_*` | Multi-session is many Codex loops sharing one ChatGPT lane. Stage 7 dashboard exists. No worker abstraction. Stage 8 live proof of concurrent Codex CLIs is still `[U]`. |
| `kick_off_prompt_gpt.md` / `instructions_for_dsktp_app.md` | ChatGPT is told it is talking to Codex. The machine contract is the sentinel pair. |

`MULTI_SESSION_CONCLUSIONS.md` is historical and stale in places (unused queue, no comma rejection, no busy timeout). Current multi-session behavior is in `MULTI_SESSION_CONTEXT.md` and the implementation plan.

Execution-profile, dashboard, kickoff, and sentinel behavior are specified in code more than in dedicated docs. Docs do not describe a `coding_agent` field, backend-specific profiles, or mixed workers.

---

## 2. Full `codex_exec_started` consumer map

**Producer:** `agent/codex_services.py::execute_codex_direct_service` writes the event **before spawn**, then the durable wrapper runs.

Started metadata that production actually writes:

- `prompt`, `repo_path`, `timeout`, `sandbox`, `prompt_contract`
- `model` only if not `codex_default`
- when progress is enabled: `codex_invocation_id`, `json_stream`, `prompt_sha256`, artifact paths, optional `extraction_event_id` / `extraction_prompt_sha256`

| Consumer | Why | Fields | Codex-specific? | Keep name + add `coding_agent`? | Class |
|---|---|---|---|---|---|
| `ledger.bind_run_execution_profile` | Immutability gate: any started event freezes the profile | Event **type existence only** | Name only | Yes | 2 |
| `run_services.select_run_execution_profile` | Surfaces that gate | Type via ledger | Name only | Yes | 2 |
| `codex_invocation.ledger_invocation_records` / `classify_invocation` | Pairs start with finish; restore/live/uncertain | `codex_invocation_id`; start evidence | Generic invocation | Yes | 2 |
| `codex_services.reconcile_codex_invocation` | Re-runs/observes open invocation | `prompt`, `repo_path`, `sandbox`, `prompt_contract` | Generic worker start | Yes, if those fields stay | 2 |
| `extracted_prompt_services` unpaired-start repair | Uses **started wrapper event**, then looks for a later finished event | Indirect | Generic | Yes | 2 |
| `local_controller._latest_valid_repo_sandbox_event` | Restore repo/sandbox if controller-start event is missing | `repo_path`, `sandbox` | Generic | Yes | 2 |
| `local_controller` start-failure tests | Assert start abort did not emit started | Type | Name only | Yes | 2 |
| Dashboard/planner/GPT feedback | Do **not** read started for loop progress | — | — | N/A | — |

Nothing in planner, governance, ChatGPT submit, or continuation requires the worker to have been Codex. The event means **a durable coding invocation was intended**. Preserving the name is enough for these consumers.

Classification summary: **all production consumers are (2) generic behavior behind Codex naming**, except cosmetic “Running Codex exec.” message text.

Classes used throughout:

1. semantically Codex-specific
2. generic behavior hidden behind Codex naming
3. cosmetic/display only
4. unclear

---

## 3. Full `codex_exec_finished` consumer map

**Producer:** `execute_codex_direct_service` stores the **entire result dict** as event metadata (or reuses an existing finished event for the same `codex_invocation_id`).

### Planner — `supervise.detect_next_supervise_action`

- **Requires** a latest finished event or it stops with `no_codex_result`.
- Reads: `validation_error`, `found`, `timed_out`, `exit_code`.
- Copies `sandbox` onto the plan for display; does **not** use it to decide the next action.
- Does **not** read `final_message`, stdout/stderr, model, command, or argv.
- Uses the event **id** as the watermark for “has this result been sent to ChatGPT?”

Invariant: clean process result (`found is not False`, no validation error, not timed out, `exit_code == 0`). Missing/empty final message does **not** stop the planner; it fails later at ChatGPT submit.

**Could Claude emit the same event?** Yes for the planner, if those four fields are honest. Class: **2**.

### GPT feedback / submit

`gpt_feedback.build_gpt_feedback_message` → `chatgpt_services.submit_feedback_to_chatgpt_service`.

Reads from latest finished metadata:

- required: `final_message_status == "valid"` and non-empty `final_message`
- size: `len(final_message) <= 100_000`
- also copies `exit_code`, `timed_out` into the ChatGPT payload
- does **not** use stdout/stderr, sandbox, model, command, or invocation id
- payload title is the literal string `"Codex completion report"`

Tests pin **no stdout/stderr fallback** (`test_feedback_payload_uses_clean_final_message_not_raw_stdout_or_stderr`).

**Could Claude emit the same event?** Yes if it fills `final_message` / `final_message_status` the same way. ChatGPT would still be told it was Codex. Class: **2** for fields, **3** for the word “Codex” in the wrapper.

### Governance / status

`apply_post_codex_governance_service` is called with the **in-memory result dict**, not by loading the event. After the fact it records `codex_exec_finished_event_id` on workspace-write policy events.

`run_status_policy._codex_objective_failure` reads `validation_error`, `found is False`, `timed_out`, missing/`!= 0` `exit_code`.

`_auto_supervision_allowed` requires `found is True`, clean exit, status `completed`, decision `continue`/`record_only`.

Git/governance itself is worker-agnostic: snapshots, attributable delta, file classification, prompt-contract mismatches, read-only writes.

**Could Claude emit the same event?** Yes for status/governance if `found`/`exit_code`/`timed_out`/`validation_error` match. Class: **2**.

### Extracted-prompt repair

If `extracted_codex_prompt_run_started` has no matching finished wrapper, it looks for a later `codex_exec_finished` and copies `exit_code`, `timed_out`, `codex_invocation_id`.

**Could Claude emit the same event?** Yes. Class: **2**.

### Restore / reconcile

Finished event for the invocation id ⇒ `STATUS_ALREADY_FINISHED`; no replay. Uncertain start-without-finish still pauses. `observe_existing_codex_invocation` rebuilds the result from **disk artifacts + intent.json command**, then may write finished.

**Could Claude emit the same event?** Yes if the durable wrapper/intent/artifacts were used. Class: **2**, with spawn-layer caveat below.

### Dashboard / API

`_latest_codex_result` exposes `event_id`, `exit_code`, `timed_out`, `found`, `validation_error`, `sandbox`, `repo_path`, stdout/stderr **lengths**. Display/read model only. Class: **3**.

### Supervision step / continuation

`ask_run_prompt` stores `approved_codex_event_id` from the finished event id. Continuation cares about run **status**, not worker identity.

### Tests

Many suites pin the event **name** and a subset of fields (`exit_code`, `final_message*`, `found`). They would keep passing if another backend emitted the same shape.

### Direct answer for section B

A Claude/Cursor execution that emitted the **same** `codex_exec_finished` metadata (`found=True`, `exit_code=0`, `timed_out=False`, no `validation_error`, `final_message_status=valid`, non-empty `final_message` ≤ 100k, `sandbox`/`repo_path`/`codex_invocation_id` present) would **not** require those consumers to change.

No production consumer parses Codex argv or Codex JSONL schema out of the finished event. `command` is stored and returned on `CodexDirectExecutionResult` but not read by planner/governance/feedback.

The remaining Codex-specific pieces are **upstream of the event**: `shutil.which("codex")`, argv construction, `--output-last-message`, JSONL normalization, and profile model allow-list.

---

## 4. Final-message contract and producer independence

**What “final message” is:** ChatGPT-transport contract, not a Codex-specific semantic object. The loop will not paste anything else to ChatGPT.

**Producer today:** Codex CLI `--output-last-message <path>`. After wait, CRAX reads that path. Durable path is `data/runs/<run_id>/codex/<invocation_id>/final_message.md`.

**Validation** (`codex_terminal._final_message_artifact_result`):

| Status | Meaning |
|---|---|
| `missing` | file not written |
| `invalid_utf8` | not UTF-8 (strict; no replacement) |
| `unreadable` | OSError |
| `empty` | missing after strip |
| `valid` | strip, keep text, length = `len(clean_text)` |
| `not_requested` / `path_prepare_failed` | preflight / path errors |

No markdown-vs-plain check. `.md` is filename only.

**Downstream:**

- `execute_codex_direct_service._reason_code`: any status other than `valid` ⇒ `codex_final_message_<status>` ⇒ `ok=False`.
- Planner does **not** look at it.
- `build_gpt_feedback_message` requires `valid` + non-empty, max 100_000 chars, UTF-8 SHA-256. No stdout fallback. Oversized payload is hashed but **not** included in the ChatGPT message.
- Submit service fails closed with `gpt_feedback_generation_failed` if that builder returns non-submittable.

**Must it be on disk before process exit?** Codex is asked to write it before exit. CRAX only **reads after wait**. Restore completeness (`_exit_evidence_is_complete`) requires `exit.json` + stdout/stderr files, **not** `final_message.md`. Observation then reads the file. If it is missing, finished metadata is `missing` and ChatGPT submit fails.

**Can it be synthesized after exit from stdout?** Current **downstream** code would not care **how** the file was produced, as long as:

1. the file is UTF-8, non-empty after strip, and present when `_final_message_artifact_result` runs, and
2. those fields are copied into `codex_exec_finished` **before** GPT feedback.

There is **no** later re-read of the file. Synthesizing after the finished event is written would **not** be seen.

`result_from_artifacts` does not synthesize from stdout. `observe_existing` also only reads the file.

**Answer:** producer independence is possible **if synthesis happens in the spawn/observe layer before `codex_exec_finished` is written**. Downstream is file-and-metadata agnostic. Tests already pin “no stdout fallback” at the **feedback** layer, which is what you want.

---

## 5. Execution-profile coupling map

Lifecycle:

1. Dashboard/start API: sandbox + optional model; `reasoning_effort` always `codex_default`; `approval_policy` is `never` only for danger-full-access, else `codex_default`.
2. `RunExecutionProfile` validates allow-lists.
3. `select_run_execution_profile` → `bind_run_execution_profile` (`BEGIN IMMEDIATE`).
4. After any `codex_exec_started`, further selection is refused (`execution_profile_immutable_after_codex_exec_started`), even an identical profile.
5. Lookup reconstructs from `run_execution_profile_selected` events only.
6. Each `execute_codex_direct_service` call re-reads the profile, **requires sandbox match**, and passes `model` to the runner only if it is not `codex_default`.
7. `reasoning_effort` and `approval_policy` are **persisted and shown**, not passed to Codex argv. Danger-full-access’s `never` is a stored label; CLI encoding is `--dangerously-bypass-approvals-and-sandbox`.

**Exact-key landmine:** both ledger and `run_services._execution_profile_from_metadata` require

`set(metadata) == {schema_version, sandbox, model, reasoning_effort, approval_policy, profile_source}`.

An extra `coding_agent` key, with no schema bump, makes **new and old** profile events `malformed` / `INVALID`. Controller runs then fail with `execution_profile_missing` / invalid before spawn.

**Missing profile:** CLI/non-controller runs are allowed (legacy). Controller runs are not. Legacy synthetic profile is read-only + `codex_default` + `legacy_compatibility`.

| Field | Classification |
|---|---|
| `sandbox` | Product preset names; Codex CLI encoding at spawn. Worker-neutral as **labels**; Codex-specific as **argv**. |
| `model` | Codex-specific allow-list: `codex_default`, `gpt-5`, `gpt-5-codex`. Would need to become backend-specific. |
| `reasoning_effort` | Legacy compatibility / unused at spawn. |
| `approval_policy` | Stored; `never` only for full access; unused at spawn except dashboard. Could become backend-specific. |
| `profile_source` | Worker-neutral provenance. |
| `schema_version` | Exact-match compatibility. |

**Subsequent prompts:** sandbox comes from the **controller runtime / recovered start config**, then **must match** the frozen profile. Model comes from the **profile every time**. There is no worker field, so a future `coding_agent` that existed only as a `run_codex_exec(...)` argument would **not** stick across extracted prompts or restore.

**Old runs if `coding_agent` were absent:** safe only if reconstruction treats it as optional. The current exact-key check does the opposite.

---

## 6. Sandbox / policy semantics independent of Codex CLI syntax

Product presets (same strings in ledger, controller, supervise, Codex argv): `read-only`, `workspace-write`, `danger-full-access`.

### read-only

| Layer | Behavior |
|---|---|
| Intent | Inspect; no edits for this dashboard run (`local_server` copy). |
| Codex encoding | `codex exec -s read-only`. |
| Preflight | Prompt-contract path-safety can fail spawn (except full access). |
| Auto-submit | Allowed only if changed-file count is **0**. Any attributable write blocks auto-send. |
| Auto-run next prompt | `safety_classifiers_disabled` ⇒ treated as auto-safe if non-empty. |
| Post-run verify | Git attributable paths. If `sandbox == "read-only"` and paths exist ⇒ objective failure `read_only_sandbox_attributable_write` ⇒ `needs_review`. |
| Retry | Initial Codex retry is retryable **only** for read-only. |
| Trust vs verify | CRAX does **not** prove Codex enforced the sandbox. It verifies **git delta after the fact**. |

### workspace-write

| Layer | Behavior |
|---|---|
| Intent | Edit this repo; “outside-workspace and dangerous access remain blocked by this dashboard run” is **copy**, not an extra CRAX enforcer. |
| Codex encoding | `-s workspace-write`. |
| Pre-run policy | `classify_workspace_write_prompt` returns `allowed=True` (`workspace_write_scoped_auto`, `safety_classifiers_disabled`). |
| Post-run policy | `verify_workspace_write_post_run` also returns `allowed=True` for unexpected/prohibited/invalid/secret-like cases; observations only. |
| Auto-submit | `send_plan_auto_safe` requires a matching `workspace_write_post_run_policy` with `allowed is True` (currently always true if the event exists). |
| Governance | Still enforces invalid contract paths and high-confidence secrets (unless full access). |
| Trust vs verify | Enforcement of “workspace only” is **trusted to Codex**. CRAX records git observations. |

### danger-full-access

| Layer | Behavior |
|---|---|
| Intent | Autonomous, no FS/network/prompt-policy limits; loop does not request per-run approval. |
| Codex encoding | `--dangerously-bypass-approvals-and-sandbox` (not `-s`). |
| Confirmation | CLI `--confirm-full-access`; extracted-prompt `allow_full_access` + confirm; dashboard local options **omit** it; remote requires Mac `--allow-remote-full-access` and typing `ENABLE FULL ACCESS`. |
| Supervise CLI | `cli.py` refuses to auto-send after a full-access Codex run (`if args.sandbox == "danger-full-access": return None`). Extracted-prompt service blocks unless `allow_full_access`. |
| Controller auto-submit | `send_plan_auto_safe` returns **True** for this preset (`danger_full_access_auto_submit`). Dashboard vs CLI are not identical. |
| Governance | Skips path-safety, read-only write, and secret objective failures; sets `autonomous_full_access_policy_bypass`. |
| Trust vs verify | Almost entirely trust the worker. Git still recorded. |

**Implication for future backends:** they do **not** need identical Codex flags. They need **equivalent product intent** plus today’s **post-run git/governance**, which is the only objective enforcement CRAX actually performs. Workspace-write “enforcement” is mostly observation. Read-only is the one preset with a hard post-run write check. Full access is a policy bypass plus confirmation UX.

---

## 7. Progress-event criticality map

Allowed kinds: `process_started`, `codex_json_event`, `assistant_commentary`, `command_started`, `command_finished`, `tool_event`, `file_change_summary`, `final_message_available`, `process_exited`, `error`, `blocked`.

Unknown kinds are coerced to `codex_json_event`. Writes are **non-critical** for the durability guard, but if a progress write throws, the **execution result** is marked `progress_persistence_failed` and `_reason_code` becomes `codex_progress_persistence_failed` ⇒ not a clean completion. So: telemetry is optional **unless** the callback is installed and then fails.

| Kind / signal | Planner | Governance | Restore | Dashboard | Diagnostic |
|---|---|---|---|---|---|
| `codex_exec_started` / `finished` / artifacts | Yes | Yes | Yes | Yes | — |
| `process_started` / `process_exited` | No | No | No | Live label | — |
| `final_message_available` | No | No | No | Status/length | — |
| JSONL-derived kinds | No | No | No | Commentary, commands, errors | Codex schema |
| `assistant_commentary` | No | No | No | Rendered event list | Codex `agent_message` |
| `error` / `blocked` | No | No | No | Issue line | Heuristic from JSONL type names |
| `thread.started` / `codex_session_id` | No | No | No | JS only | Undocumented Codex JSONL |
| `todo_list` / `plan_items` | No | No | No | JS only | Same |
| `quota_wait` / `quota-resume` | No Python | No | No | **Frontend residue** (JS posts `/quota-resume`; no Python route) | Dead |

JSONL normalization (`normalize_codex_jsonl_event`) is Codex-shaped: `type`/`event`/`kind`, nested `command`/`tool`, `agent_message`, substring heuristics (`blocked`, `error`, `exec`, `patch`). Another worker’s stream-json would mostly land as `codex_json_event` unless an adapter mapped it.

**Minimum contract for CRAX correctness:**

1. Durable start + finish events with the fields in sections 2–3.
2. Durable artifacts (`exit.json`, stdout/stderr files) for restore.
3. `final_message` fields valid before finish is recorded.
4. Progress callback must **not throw** if attached.

**Optional observability:** every JSONL kind, session id, todo plan, quota UI.

---

## 8. Controller-state / action naming map

| Name | True meaning | Rename needed? |
|---|---|---|
| `starting_initial_codex` | First coding-worker invocation for the run | No; compatibility debt |
| `initial_codex` (action_key) | Same | No |
| `codex_working` (operator status) | Worker thread in a Codex-running action kind | No; display |
| `CODEX_RUNNING_ACTION_KINDS` | `initial_codex`, `approval_run_prompt`, `restore_resume` | Semantics = coding worker running |
| `ask_run_prompt` | Run the extracted sentinel prompt | Already worker-generic |
| `ask_send_to_gpt` / capture / extract | ChatGPT lane | Generic |
| `codex_invocation_uncertain` | Unproven spawn/completion; never replay | Generic fail-closed |
| `waiting_for_retry` / ChatGPT wait | Lane/backoff | Generic |
| `quota_*` | Not implemented in Python | Frontend residue |

Keeping legacy names is **harmless** for functionality. Semantics of the coding states are “worker running,” not “must be Codex CLI.”

---

## 9. ChatGPT-facing Codex contract map

**Machine-parsed:**

- `BEGIN_NEXT_CODEX_PROMPT` / `END_NEXT_CODEX_PROMPT` in `prompt_extraction.py` and `chatgpt_ax_capture.py`.
- Planner requires `extraction_method == "sentinel_block"`. Alternate extractors exist (`labeled_fenced_code_block`) and are **rejected** for auto-run.
- Capture completeness waits for a full sentinel block after the submission marker.
- Feedback transport: `AGENT_SUBMISSION` marker + final-message body. Independent of which worker ran.

**Narrative only (not parsed):**

- Kickoff: `say exactly "Hey ChatGPT, this is Codex."`
- Instructions: “Codex, OpenAI’s coding agent”
- Feedback wrapper: `"Codex completion report"`
- Planner stop strings that say “Codex CLI” / “codex-run”
- Dashboard labels “Codex working”

**Does ChatGPT need to know the real worker?** Not for extraction. Keeping `BEGIN_NEXT_CODEX_PROMPT` in v1 is **technically safe**. ChatGPT is not told model/sandbox/worker identity in the feedback payload (only `run_id`, status, exit_code, timed_out, changed_files, review/stop reasons, final message).

A non-Codex worker can keep the sentinel without breaking extraction. ChatGPT may still **write prompts as if talking to Codex**; that is prompt-quality, not a parser break.

---

## 10. Initial vs subsequent worker selection

Every coding invocation, initial or extracted, goes through `execute_codex_direct_service` → `run_codex_exec` (or observe/reconcile of an **already started** invocation).

| Path | Sandbox source | Model source | Worker source |
|---|---|---|---|
| Initial controller worker | Start request → runtime.sandbox | Frozen profile | Implicit Codex (`which("codex")`) |
| Extracted `ask_run_prompt` | Same recovered sandbox, must match profile | Profile again | Same implicit Codex |
| Reconcile/observe | Started-event `sandbox` + `intent.json` `command` | Intent/profile | **Whatever command was already spawned** |
| Cancel | `terminate_codex_run(run_id)` | n/a | Process group of that run’s wrapper |

**Accidental Codex fallback:** if `coding_agent` existed only as a `run_codex_exec` parameter, then:

- initial run could pass `claude_code`
- extracted/reconcile **new spawn** would default back to Codex unless the same value were stored on the immutable profile (or started intent) and read on every call
- observe of a live invocation would **not** fall back; it reuses `intent.command`

So “agent immutable per run” is **not** implemented. Today only sandbox+model are immutable, and only sandbox is recovered into the controller runtime.

Cancellation and feedback do not select a worker.

---

## 11. Multi-session implications for mixed workers

Structural isolation is **run_id-keyed**, not Codex-binary-keyed:

- `_ACTIVE_CODEX_PROCESSES[run_id]`
- wrapper is command-generic (`intent.command`)
- cancel is `terminate_codex_run(run_id)`
- shutdown terminates all active invocations
- git is repo-global; same-repo writers already allowed
- ChatGPT lane is independent of which coding binary ran

**Could A be Codex and B be Claude concurrently, same event contract?** Structurally yes: no registry assumes a single global Codex process or a single binary name.

**Real blockers, not naming:**

1. No per-run worker identity, so B would not stay Claude on the next prompt (section 10).
2. Concurrent **different** CLIs’ auth/quota is `[U]` even for two Codex processes.
3. Artifact directory name `.../codex/<invocation_id>` is naming only.
4. Live-pid discovery greps `agent.codex_invocation_wrapper` + invocation id — fine if both use the wrapper.
5. Dashboard copy still says “Codex working” for both.
6. `observe_existing` still reports `codex_path = shutil.which("codex")` even when observing a non-Codex command — cosmetic/wrong `found` metadata if Codex is absent.

No Stage 7 read-model field assumes all sessions share one worker binary.

---

## 12. Compatibility tests and missing characterization

**Already protecting a same-shaped backend:**

- `tests/test_supervise.py` — planner gates; **final_message not stdout**; oversize; missing/empty not submittable
- `tests/test_chatgpt_services.py` — same transport guards
- `tests/test_codex_invocation.py` — start/finish pairing, started-write failure prevents spawn, restore from artifacts
- `tests/test_codex_services.py` — started+finished, profile sandbox match, missing controller profile, model omitted when default
- `tests/test_run_services.py` — immutability after started; exact profile equality
- `tests/test_governance_services.py` / `test_run_status_policy` (via supervise/cli) — `found` / exit / timeout
- `tests/test_extracted_prompt_services.py` — repair from finished event
- `tests/test_multi_session_concurrency.py` / `restore.py` — run_id cancel, uncertain never replayed
- `tests/test_local_controller.py` — started not emitted on failed start; latest finished in read model

**Missing before extracting a seam (do not write them yet):**

1. Final-message producer independence (file written by adapter after exit, not by Codex `--output-last-message`).
2. Progress optionality: loop still completes with zero JSONL kinds if start/finish/final_message exist.
3. Extra event metadata (`coding_agent`) must not break consumers; extra **profile** keys currently **must** break (pin that).
4. Worker/profile immutability across extracted prompts (today it would **not** hold).
5. Mixed-worker two-session: different `intent.command`, same event names, isolated cancel.
6. Restore of a non-Codex `intent.command` via observe (wrapper-generic vs `which("codex")` on the result).
7. Missing-binary / auth-failure classification that is not `codex_not_found` / `which("codex")`.
8. `quota-resume` / `thread.started` documented as frontend-only so they are not treated as backend contract.

---

## 13. Hypothetical compatibility experiment

Assumption: `run_codex_exec(...)` gains `coding_agent = "codex" | "claude_code" | "cursor"`; adapters return the same result dict; CRAX still emits `codex_exec_started/finished/progress_event/invocation_uncertain` and still uses `BEGIN_NEXT_CODEX_PROMPT`.

### What would **not** break (outside spawn/JSONL normalization)

Planner, governance, continuation, GPT feedback builder, destination gate, sentinel extraction, extracted-prompt repair, profile immutability **by event type**, multi-session registry, cancel-by-run_id, dashboard latest-result fields, ChatGPT lane.

Those modules never inspect argv or require the worker to have been Codex.

### What **would** still break or silently mis-route

These are exact breakpoints, not design advice:

1. **`execute_codex_direct_service` does not take or persist `coding_agent`.** Initial, extracted, and **new-spawn** reconcile all call `run_codex_exec` without that argument. Default remains Codex. Subsequent prompts fall back.

2. **`RunExecutionProfile` / ledger exact-key reconstruction.** Persisting `coding_agent` on the profile without a schema change invalidates every profile event.

3. **Model allow-list** (`gpt-5`, `gpt-5-codex`, `codex_default`). A Claude/Cursor model string cannot be stored today. Start validation fails, or you lie with a Codex model name.

4. **`shutil.which("codex")` and `found`.** Planner/governance treat `found is False` as `codex_not_found`. If the adapter still uses the Codex binary check, Claude cannot start when `codex` is missing even if `claude` exists. If the adapter sets `found=True` for Claude, consumers are fine.

5. **`observe_existing_codex_invocation`** always sets `"found": True` and `codex_path = shutil.which("codex")`. Restore metadata can claim Codex was found when a different command actually ran.

6. **ChatGPT text still says Codex.** Extraction still works. Operator/ChatGPT behavior may be wrong, not the parser.

7. **Dashboard JS** looking for `thread.started` / `todo_list` / `quota_wait` would just show empty. Not a loop break. `quota-resume` is already a dead POST.

8. **Tests** that assert `command[0:2] == ["codex", "exec"]` or `--output-last-message` (`test_codex_services.test_run_codex_exec_requests_run_scoped_final_message_artifact`) would fail **inside** the spawn layer, which the hypothetical excludes from “outside.”

**Bottom line:** with the same legacy event/result/sentinel contract, **almost no downstream production consumer would need to change**. The loop would still be a Codex loop unless selection is plumbed through profile + `execute_codex_direct_service` + start API. Compatibility naming works; selection does not.

---

## 14. Semantic coupling vs compatibility naming

**Compatibility naming (keep in v1):**

- Event types `codex_exec_*`, `codex_progress_event`, `codex_invocation_uncertain`
- Sentinel `BEGIN_NEXT_CODEX_PROMPT`
- Controller states `starting_initial_codex`, `codex_working`
- Reason codes `codex_not_found`, `codex_nonzero_exit`, …
- Artifact dir `.../codex/...`
- Feedback title “Codex completion report”

**True semantic coupling (would have to change for a real second backend):**

- Spawn argv, `which("codex")`, `--json`, `--output-last-message`, `--dangerously-bypass-approvals-and-sandbox`
- JSONL normalization heuristics
- Execution profile model allow-list and exact metadata keys
- No durable worker field, so immutability cannot apply to agent choice
- ChatGPT kickoff/instructions that define Codex as the implementer (quality, not parse)
- Dashboard start form: Codex sandbox/model options only

**Already worker-generic:**

- Durable wrapper (command list in `intent.json`)
- Git snapshots / invocation delta / file classification
- Most governance (except sandbox-named objective failures)
- ChatGPT lane, destination gate, sentinels
- Multi-session registry, claims, cancel-by-run_id

---

## 15. Three highest-risk questions for Audit 3

1. **Where can `coding_agent` live without breaking exact-key profile reconstruction, and how is it read on every spawn (initial, extracted, restore-new-spawn) so the run cannot fall back to Codex?** Audit 2 shows that adding a key to the current profile schema is an immediate incompatibility, and that `run_codex_exec` alone cannot make the choice stick.

2. **What is the restore/observe contract for a non-Codex `intent.command` when `codex` is not on PATH?** `observe_existing` and `found`/`codex_path` still assume Codex. Audit 3 should pin whether restore must treat `intent.command` as authoritative and how missing-binary/auth failures are classified without `which("codex")`.

3. **Which sandbox guarantees are product invariants vs Codex CLI trust?** Read-only is post-run git enforcement; workspace-write is mostly observation; full access is a governance bypass plus confirmation UX. Audit 3 should decide whether another backend must match Codex’s sandbox implementation or only those CRAX-verified outcomes — without mapping Claude/Cursor flags yet.

No code was changed, no interface was designed, and no implementation plan is implied beyond these observations.
