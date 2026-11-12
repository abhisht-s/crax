Audit 4 is design-only. No files were modified. The smallest safe change is: **persist one immutable `coding_agent` on the existing execution profile, keep every legacy `codex_*` contract, and add adapters only inside the current spawn/normalize/final-message seam**. The live Codex loop stays the default and the reference path.

---

## Overarching answer

Promote the **execution profile + `execute_codex_direct_service` + `run_codex_exec`** seam into a multi-backend worker system. Do **not** genericize `local_controller.py`. Do **not** rename events, sentinels, or artifact paths in v1.

The code already has the right grain:

- Profile bind is the immutable per-run execution contract (`run_execution_profile_selected`, frozen after `codex_exec_started`).
- `execute_codex_direct_service` already owns start/finish events, open-invocation reconcile, and progress persistence.
- `run_codex_exec` already separates argv construction from the command-generic durable wrapper.
- Git/governance/planner already consume `codex_exec_*` result shape + `final_message`, not Codex internals.

What is missing is a **durable worker identity**, a **schema strategy that cannot silently become Codex**, and a **backend adapter** for argv / stream / final-message / failure mapping.

---

## 1. Durable representation of `coding_agent`

**Recommendation:** store `coding_agent` on the execution profile. Allowed values: `codex`, `claude_code`, `cursor`. Schema v1 events with no field **are Codex**. Schema v2 events **must** include the field. Never infer the worker from argv.

### Option analysis

| Location | Old-run compatibility | Exact-key reconstruction | Immutability | Restore | Extracted path | Startup race | Validation | Dashboard/API | Migration | Silent Codex fallback |
|---|---|---|---|---|---|---|---|---|---|---|
| **1. Add to execution profile** | v1 events stay valid if the reader still accepts schema 1 | Today `set(metadata) != expected_keys` fails; dual-schema is required | Already frozen after `codex_exec_started`; idempotent compare already exists | Restore re-reads profile at spawn | Extracted/initial both call `execute_codex_direct_service`, which already resolves the profile | Profile is written after claim, before `local_controller_run_started`, before any worker | `RunExecutionProfile` already fail-closes unknown values | `_profile_summary` / `execution_profile_options()` are the natural read model | One event type, one reconstruction function | **Avoidable** if v2 requires the field and missing/unknown is INVALID, not default |
| **2. Separate run-level event** | Best for old profile bytes | Leaves profile exact-key untouched | Need a second freeze rule | Two sources of truth | Extracted path would have to read a new event | Same start-order hazard as destination bind | New validator | Extra API field | Looks like destination binding | **High:** a Claude start that writes profile but loses the worker event would spawn Codex. Old controllers would also spawn Codex unless the profile is poisoned |
| **3. `local_controller_run_started` only** | Extra keys are ignored by restore | Not exact-key; restore only checks `metadata_version`, source, sandbox | Not the spawn authority | `_recover_run_configuration` uses this for repo/sandbox, **not** for spawn model/worker | CLI/extracted can run without this event | Crash after profile, before this event, would lose worker if this were authority | Weak, not exact-key | Easy to display, wrong to trust | Low write cost, high semantic cost | **High:** spawn uses profile, not this event |
| **4. Infer from invocation command** | Historical Codex argv is Codex | N/A | No. First spawn needs the choice before argv exists | After first spawn, `intent.json` has argv, but missing/malformed argv would guess | Same | Cannot start the first invocation | Fragile | None | None | **Certain:** parse failure becomes Codex |
| **5. Existing binding analog** | Destination bind is extra-key-tolerant; conversation claim is uniqueness, not identity | Destination bind is **not** exact-key; profile is the only exact-key execution schema | Destination bind is ChatGPT identity; claim is chat exclusivity | Neither is consulted at Codex spawn | N/A | Claim-then-profile-then-destination is the current start order | Already strong for their own jobs | Already exposed | Reusing them would overload unrelated invariants | Worker is not what they store |

Evidence:

- Profile reconstruction is exact-key and version-pinned:

```4279:4290:agent/ledger.py
    expected_keys = {
        "schema_version",
        "sandbox",
        "model",
        "reasoning_effort",
        "approval_policy",
        "profile_source",
    }
    if set(metadata) != expected_keys:
        return None
    if metadata.get("schema_version") != RUN_EXECUTION_PROFILE_SCHEMA_VERSION:
        return None
```

- Immutability is already “no rebind after `codex_exec_started`”.
- Controller-created runs **fail closed** if the profile is missing; non-controller CLI runs with no profile already behave as Codex (`model = CODEX_DEFAULT_SELECTION`).
- `local_controller_run_started` is a restore hint for repo/sandbox, not spawn authority.
- Dashboard `legacy_compatibility` is a **display** default for missing profiles, not a spawn default for controller runs.

**Do not** put the worker only in `local_controller_run_started`. Extra keys there are ignored, so it cannot enforce anything.

**Do not** use a separate worker event as the authority. That copies destination-binding’s shape onto a choice that spawn already reads from the profile, and it creates the exact silent-fallback bug this audit must prevent.

Optional denormalization later: copy `coding_agent` onto `local_controller_run_started` and the session list for display. Authority remains the profile.

**No mid-run switch:** once `codex_exec_started` exists, `bind_run_execution_profile` already refuses a different profile. Worker is part of that tuple.

---

## 2. Execution-profile schema compatibility

`schema_version` is not a migration engine today. It is a **hard equality check** against `RUN_EXECUTION_PROFILE_SCHEMA_VERSION = 1`. Extra keys, missing keys, unknown version, or a model outside `ALLOWED_CODEX_MODEL_SELECTIONS` all become `INVALID`.

### Compatibility rules

| Case | Rule |
|---|---|
| Old profile event, no `coding_agent`, `schema_version=1` | New reader reconstructs it as `coding_agent=codex`. This is explicit compatibility, not a guess. |
| New profile event with `coding_agent`, `schema_version=2` | Exact keys = v1 keys + `coding_agent`. Worker must be in `{codex, claude_code, cursor}`. Model allowlist is **worker-dependent**. |
| Old controller reading new v2 profile | Stays INVALID. Fail closed. An old binary must **not** spawn Codex for a Claude/Cursor run. |
| New controller reading old v1 profile | Accept as Codex. Required for historical ledger replay and unfinished Codex runs. |
| Should `schema_version` bump? | **Yes, but only on writes that include `coding_agent`.** Codex-only writes should keep emitting v1 until a deliberate cutover. |
| Backend-specific model | Keep a single top-level `model` field. Validate against the worker’s allowlist. Do **not** nest `backend_config` in v1. Nested config is extra schema surface and is not needed to spawn. |
| `sandbox` | Remains the CRAX product preset: `read-only` / `workspace-write` / `danger-full-access`. Persist the preset, not raw flags. |
| `reasoning_effort` / `approval_policy` | Remain legacy Codex fields. Non-Codex runs persist `codex_default` and must not pretend they map to Claude/Cursor. |
| Unknown extra keys | Fail closed, per that schema version’s exact key set. Do not loosen v1 to “allow extras.” That would let an old controller ignore `coding_agent` and spawn Codex. |

**Write policy (this is the rollback hinge):**

1. Ship the dual reader first.
2. Codex dashboard/CLI keeps writing schema v1 (exact six keys) until Claude is actually enabled.
3. Non-Codex starts always write schema v2 with `coding_agent` required.
4. Only after the new reader has been the daily driver, optionally cut Codex writes over to v2 with `coding_agent=codex`.

That is the minimum migration surface: one reconstruction function, one bind signature extension, no SQLite table migration, no backfill job. Old Codex rows stay bytes-identical.

---

## 3. Exact adapter seam

The proposed shape is **almost right**, with one correction: the generic service already exists, and it is `execute_codex_direct_service`, not the controller.

Current call graph **[C]**:

```
local_controller worker
  -> execute_initial_direct_codex_run_service
  -> execute_extracted_codex_prompt_service
      -> execute_codex_direct_service
          -> _resolve_execution_profile
          -> run_codex_exec / observe_existing_codex_invocation
              -> argv
              -> run_json_streaming_command -> spawn_invocation_wrapper
              -> normalize_codex_jsonl_event
              -> _final_message_artifact_result
          -> codex_exec_started / codex_exec_finished
      -> apply_post_codex_governance_service
```

CLI `codex-run` also enters at `execute_codex_direct_service`.

| Candidate | Code change | Shared reconcile | Shared governance | Shared initial/extracted | Shared cancel | Legacy result events | Backend parse/final-message |
|---|---|---|---|---|---|---|---|
| Around `run_codex_exec` (argv + stream + extract + classify) | Narrow. `codex_terminal.py` already splits command construction from wrapper | Yes, stays in `execute_codex_direct_service` | Yes | Yes | Yes (`terminate_codex_run` / wrapper pgid) | Yes | **Clean.** This is where those jobs already live |
| Around `execute_codex_direct_service` | Wide. Would duplicate open-invocation, uncertain, progress, finish-event idempotency | Would split or copy it | Yes | Call sites stay, internals churn | Yes | Easy to accidentally fork | Too late; spawn already happened |
| Argv + normalization only, no classification/final-message | Too small. Claude/Cursor cannot use `--output-last-message` | Yes | Yes | Yes | Yes | Finish events would still assume Codex success rules | Final-message synthesis would leak into the wrapper or service |
| New generic orchestrator in `local_controller.py` | Largest and wrong | Controller would start owning spawn | Risk of bypassing governance | Two entry paths | Cancel already keyed by `run_id`; don’t touch it | High rename temptation | Turns the controller into the thing ARCHITECTURE.md says it should not be |

**Recommended seam:** keep `execute_codex_direct_service` as the generic CRAX coding-worker service (keep the name in v1). Inside it, after profile resolve, dispatch to a backend adapter. The adapter does **not** own the wrapper, ledger, git, or planner.

```
execute_codex_direct_service          # shared: profile, reconcile, events
  -> adapter.validate / map sandbox / build argv / env/cwd/stdin policy
  -> shared durable wrapper + artifact dir
  -> adapter.normalize_stream_line
  -> after proven exit: adapter.extract/synthesize final_message.md
  -> adapter.classify onto legacy result fields
  -> shared codex_exec_* events
  -> shared git/governance/planner
```

That **proves** the user’s preferred shape, with the generic service sitting **above** the adapter and **around** the wrapper, not in the controller.

Do not rename `run_codex_exec` in the first slice. Add dispatch beside it, then keep Codex as the only adapter until characterization says the Codex path is unchanged.

---

## 4. Minimum adapter behavioral contract

Not a Python Protocol. The smallest observed jobs:

| Responsibility | Owner |
|---|---|
| Validate backend availability / executable on PATH | **Adapter**, called from shared service before spawn |
| Validate backend-specific model | **Adapter**, using profile.model |
| Unsupported CRAX sandbox for this backend | **Controller/profile layer** at start, **adapter** again at spawn (defense in depth) |
| Construct argv | **Adapter** |
| cwd / env / stdin policy | **Adapter** chooses; **wrapper** applies (stdin must be DEVNULL for all backends) |
| Map CRAX sandbox preset → backend flags | **Adapter**. Persist the CRAX preset, never raw flags |
| Normalize stream-json → existing CRAX progress kinds | **Adapter**. Shared service writes `codex_progress_event` |
| Classify success/failure onto legacy result fields | **Adapter**, then shared `_reason_code()` |
| Extract final assistant result | **Adapter** |
| Synthesize `final_message.md` when the backend does not write it | **Adapter**, invoked by shared executor **after proven process exit**, before `codex_exec_finished` |
| Expose executable path/version | **Adapter**, into existing `found` / `codex_path`-shaped fields (keep key names) |
| Diagnostics metadata (`coding_agent`, argv0, version) | **Adapter** → extra keys on the existing result dict; unknown extras on **progress** are already summarized, not exact-key |
| Optional preflight auth check | **Not v1.** Fail at first invocation with classified failure. A separate auth probe is an operator live action |
| Durable spawn, identity, cancel, reconcile | **Durable wrapper** |
| `codex_exec_started` / `finished` / uncertain | **Shared CRAX execution service** |
| Git snapshot, file classification, risk policy, status | **Governance** |
| Worker selection, sandbox/model options, remote full-access gate | **Controller/profile/API** |
| ChatGPT sentinel, kickoff, planner | **Not adapter** |

Keep ChatGPT/planner logic out of adapters. Adapters produce a Codex-shaped result dict plus a valid or invalid `final_message.md`.

---

## 5. Legacy event compatibility strategy

**v1 choice: A. Keep all event names unchanged. Add `coding_agent` inside metadata where it is already free-form (start/finish/result). Do not dual-write. Do not rename.**

| Consumer | Effect of A |
|---|---|
| Planner | `_latest_event(..., "codex_exec_finished")` and `_codex_result_stop` keep working |
| Restore | Open invocation is keyed by `codex_invocation_id` on those events |
| Profile immutability | Still keyed off `codex_exec_started` |
| Extracted repair | Looks for `extracted_codex_prompt_run_*` paired with `codex_exec_finished` |
| Dashboard | Progress kinds and `final_message_available` stay |
| Tests | Huge ratchet stays green if names stay |
| Historical replay | Old rows remain valid |
| Old runs | Unchanged |

B (dual-write) doubles every planner/restore/test branch and creates split-brain if one write lands. C (rename now) is the opposite of Audits 1–2. Do not do it until long after mixed workers are boring.

Add `coding_agent` onto `codex_exec_started` / `codex_exec_finished` metadata as a **non-authoritative copy**. Authority is still the profile. Downstream may ignore the extra key; that is fine because those events are not exact-key reconstructed.

Keep `extracted_codex_prompt_*` and `next_codex_prompt_extracted` names. They are ChatGPT-extraction events, not backend names.

---

## 6. Final-message synthesis and recovery

**Where:** shared executor **after proven child exit**, calling the adapter, **before** `codex_exec_finished` is written. Not in the durable wrapper. Not only in the live stream parser.

Current Codex path **[C]**: Codex writes `final_message.md` via `--output-last-message`; CRAX reads the file after exit; restore re-reads the same file from artifacts. The wrapper never parses JSONL.

### Crash after child exit, before finish event

**Codex**

1. Wrapper has `exit.json`, `stdout.log`, `stderr.log`.
2. Codex should already have written `final_message.md`.
3. Restore classifies COMPLETE, `observe_existing_codex_invocation` re-reads the file, then `execute_codex_direct_service` writes `codex_exec_finished` if missing.
4. If the file is missing/empty, fail closed (`final_message_status` ≠ `valid`). Do not invent text from JSONL in v1. That would change Codex behavior.

**Claude / Cursor**

1. Wrapper still has durable `stdout.log` (NDJSON) and `exit.json`.
2. There is no native `final_message.md`.
3. Restart must **re-parse stdout.log** with the same adapter, write `final_message.md` atomically, then write the finish event.
4. Live in-memory parser state is discarded on crash. That is required, not optional.

### Rules

- Prefer an already-valid `final_message.md`; do not rewrite it.
- If missing, synthesize from durable stdout (and stderr only as failure evidence, never as success text).
- Write `final_message.md` fsync/atomic **before** `codex_exec_finished`.
- Malformed/truncated NDJSON, missing terminal result, `is_error`, `authentication_failed`, trust/auth stderr → `final_message_status` not `valid`, and do not copy that text into a successful assistant message.
- Repeated reconcile is idempotent because the file is the artifact, and finish-event write is already skipped if one exists for that `codex_invocation_id`.

The wrapper must **not** grow a JSON parser. It remains command-generic.

---

## 7. Provider-specific success/failure

A seven-way public taxonomy (`success`, `auth_failure`, `permission_blocked`, …) is **too much for v1 ledger/planner**. The planner only stops on:

- `validation_error`
- `found is False`
- `timed_out`
- missing/`nonzero` `exit_code`

It does **not** inspect `final_message_status`. So a pretty new enum that is not mapped onto those fields would be ignored, and a false success would still be handed to ChatGPT.

**v1 rule:** adapters classify internally, then **must** populate the legacy fields so existing stops fire.

| Provider case | Legacy mapping |
|---|---|
| Binary missing | `found=False`, `exit_code=None` |
| Bad model / unsupported sandbox / isolation flags rejected | `validation_error` set, `exit_code=2` |
| Claude: no result / malformed NDJSON | not `found` failure; `exit_code` actual; `final_message_status` invalid; set `validation_error` so planner stops even if exit is 0 |
| Claude: terminal result + `is_error` / `authentication_failed` | do **not** treat as success even if `subtype=success`; `validation_error` or keep nonzero exit; final message invalid |
| Claude: exit 1 + `subtype=success` + `is_error=true` | fail closed until a live probe says otherwise |
| Cursor: nonzero + stderr trust/auth | nonzero exit already stops; still do not synthesize a valid final message from stderr |
| Cursor: no result event | same as missing final message |
| True structured success | `exit_code==0`, `final_message_status=valid`, no `validation_error` |

Optional diagnostic key `provider_outcome` on finish metadata is fine. Do not make the planner depend on it in v1.

**Open [U]:** whether Claude or Cursor can be *actually successful* with a nonzero process exit. v1 must not normalize that to success until a disposable-repo probe proves it. If proven, the adapter may set compatibility `exit_code=0` only when structured success is unambiguous. Document that as a compatibility translation, not as the real wait status (real wait status stays in `exit.json`).

---

## 8. Permission preset architecture

**Recommendation: option 1.** Keep the same three CRAX product presets. Persist `sandbox` as that preset. Validate support against the backend. Unsupported combinations fail at **start**, not after spawn.

Do **not** invent backend-specific preset names in the profile. Do **not** ship a generic capability matrix UI in v1. A small **support table** in code is enough:

| Worker | `read-only` | `workspace-write` | `danger-full-access` |
|---|---|---|---|
| `codex` | yes | yes | yes, existing confirm/remote gates |
| `claude_code` | yes, after probes | no until proven | no |
| `cursor` | disabled in v1 | disabled | no |

Dashboard/API: `execution_profile_options()` becomes worker-dependent. Selecting Claude returns only `read-only`. Do not show workspace-write as if it were safe.

Answers:

- Unsupported combo fails at start validation **and** again at spawn.
- Profile persists the **requested CRAX preset**, not backend flags.
- Raw flags are not user-settable. Flags are an adapter implementation detail. User-settable flags would bypass the honesty of the preset and the remote full-access gate.
- Remote full-access gating stays on the CRAX preset. If a backend does not support `danger-full-access`, the option is absent even when remote full-access is enabled. Codex remains the only backend that can receive it.

---

## 9. Project-config isolation policy

**v1 default: isolated / script-like mode.** Reproducibility and permission correctness beat provider “normal” quality.

| Backend | v1 policy |
|---|---|
| Codex | unchanged (current CLI flags) |
| Claude | `--bare` **should be the v1 default**, plus explicit `--tools` and `dontAsk` / equivalent. Repo `CLAUDE.md`, user settings, MCP, and hooks must not silently widen powers |
| Cursor | **remain disabled** until there is a proven isolation equivalent. Audit 3 found project-config/trust/`--force` semantics too risky |

Per-backend configurable “load repo config” is a later operator option, default off. Hidden provider session state (resume, local transcripts, trust files) must not become CRAX source of truth. Multi-session reproducibility requires the adapter to pass isolation flags every invocation, not rely on a previous process.

This is an operator quality trade: `--bare` may make Claude dumber. That is acceptable for a first backend. It is not acceptable to get a smarter Claude that can write because a repo config loaded extra tools.

---

## 10. Durable-wrapper hardening

**Required before any second backend**, after Codex characterization tests so the Codex path does not silently change.

| Item | Verdict | Evidence |
|---|---|---|
| `stdin=DEVNULL` on wrapper **and** child | **Required** | Wrapper `Popen` sets stdout/stderr files only; child inherits stdin. Wrapper spawn also omits `stdin=DEVNULL`. Interactive CLIs can block on inherited TTY |
| Exact process-group semantics | **Required to document and pin** | Wrapper uses `start_new_session=True`; child does not. Cancel `killpg`s wrapper pgid. Wrapper signal handler sends to `child.pid` only, not the group |
| Child pid vs wrapper pgid | Keep wrapper as group leader; do not let adapters `setsid` | If Claude/Cursor daemonize into a new session, `killpg` misses them |
| Kill escalation | Already SIGTERM then SIGKILL on the wrapper group | Keep; characterize Codex |
| Daemonized grandchildren | **Desirable follow-up**, not a blocker if providers are invoked without background modes | Cannot fully solve in the wrapper |
| Provider-native background/agent modes | **Required: forbid in argv** | Adapter contract |
| Max post-result child wait | **Desirable**, independent of first Claude read-only if the CLI exits when the result event is emitted | Don’t wait forever for subagents |
| Codex behavior change | `stdin=DEVNULL` should be a no-op for `codex exec`; still needs a focused characterization test | Don’t ship wrapper changes untested on Codex |

Unnecessary: rewriting the wrapper as a per-backend process manager.

---

## 11. Invocation artifact naming/path

**Keep** `data/runs/<run_id>/codex/<invocation_id>/` and `codex_invocation_id` for all backends in v1.

Restore, disk discovery, tests, and `intent.json` keys are all Codex-named. A parallel tree or dual-write is migration theater. A backend-specific subdir (`.../claude/...`) would split `disk_invocation_ids()` and open-invocation repair.

Put `coding_agent` inside `intent.json` as an extra field. Do not rename `codex_invocation_id`. Generic path/key rename is a later stage, after event renaming, if ever.

---

## 12. ChatGPT sentinel / narrative

| Item | v1 |
|---|---|
| `BEGIN_NEXT_CODEX_PROMPT` / `END_NEXT_CODEX_PROMPT` | **Unchanged.** Machine-parsed; huge test surface; ChatGPT may already have it in memory |
| Kickoff wording | **Parameterize worker identity.** Do not keep saying “this is Codex” if Claude ran. That misleads the planner |
| Completion report title | Change `Codex completion report` to include the actual worker, e.g. keep the string prefix for grep-stability or add a metadata line `- coding_agent: claude_code` |
| Completion metadata to ChatGPT | **Yes, include `coding_agent`** as a run-metadata bullet. Cheap and honest |
| Leave all narrative saying Codex | **No.** Sentinel stays; prose should not lie |

Minimum parser risk, no misleading planner context. Do not teach ChatGPT a new sentinel in v1.

---

## 13. Dashboard / API fields

Start body already uses required+optional exact fields (`additional_session` is the pattern). Add optional `coding_agent`, default `codex`. Legacy clients that omit it stay Codex.

Minimum fields:

**Start**

- `coding_agent` optional, default `codex`
- `model` allowlist depends on agent
- `sandbox` options depend on agent
- unsupported combinations 400, not silently coerced

**Options endpoint**

- `coding_agent_options`
- `sandbox_options` / `model_options` filtered by selected agent
- Claude: only `read-only` until write is proven
- Cursor omitted or `enabled: false`

**Existing run read model**

- `execution_profile.coding_agent` (implied `codex` on v1)
- session list entry includes `coding_agent`
- no control to change it

Do not design polished UI. A selector + filtered dropdowns is enough.

Start API exact-field checking means a new required field would 400 old dashboards. Keep it optional.

---

## 14. Mixed-worker multi-session

Already allowed: mixed sessions, same repo, no repo lock. Worker is per `run_id`, so A=Codex, B=Claude, C=Cursor is structurally fine once each backend exists.

Constraints:

- Same ChatGPT conversation still forbidden.
- Same repo writers may interleave; that is an existing operator choice, now with different tools.
- Isolation flags must still hold **per invocation**, because a sibling Codex workspace-write session does not justify Claude loading repo MCP.
- Dashboard must show the worker on each card so the operator can see the mix.
- Do not serialize coding workers in v1 unless Stage 8 / live probes show auth or quota coupling across CLIs. That is still **[U]**, now across vendors.

---

## 15. Rollout order

The user’s draft is close. This is the order that matches actual dependencies:

0. **Characterization of current Codex profile exact-key, wrapper stdin/pgid, and final-message restore.** No behavior change.
1. **Wrapper hardening** (`stdin=DEVNULL` both layers, pin process-group tests). Codex-only. Required before a second backend, not before profile dual-read.
2. **Dual-schema profile reader** (v1 ⇒ Codex; v2 accepted). Still write v1. No spawn change. This is the real “persist default Codex without behavior change.”
3. **Adapter seam with Codex as the only adapter.** `execute_codex_direct_service` dispatches; Codex adapter is a move of current argv/normalize/final-message code. Prove Codex tests + controller ratchet green.
4. **API/dashboard optional `coding_agent=codex`**, still no second backend. Options payload can grow `coding_agent_options` with one enabled value.
5. **Claude adapter behind a disabled/experimental flag**, start validation rejects it unless the flag is on. Support table: `read-only` only. `--bare` + tools restriction. Still no live spend.
6. **Operator-approved live probes in disposable repos** (section 19). Not against this repo, not against the daily-driver checkout.
7. **Enable Claude read-only** only after those probes pass.
8. **Claude workspace-write** only after section 20 probes. Do not show the preset before that.
9. **Cursor remains disabled** until after Claude read-only has been a daily-driver backend.
10. **Generic event/sentinel/path rename** only much later, if ever. Not part of making a second backend work.

Do not enable Claude by writing v2 profiles before the dual reader is on the daily-driver binary.

---

## 16. Migration and rollback

| Situation | Behavior |
|---|---|
| Old runs (no worker field, v1 profile) | New code treats as Codex. Spawn unchanged |
| New runs with omitted `coding_agent` | Write v1 Codex profile. Old binary can still run them after rollback |
| New runs with `coding_agent=codex` explicit | Prefer still writing v1 until cutover, so rollback remains possible |
| New runs with Claude/Cursor | Write v2. Old binary sees INVALID profile and **must not** spawn Codex |
| Unfinished Codex runs during upgrade | v1 profile + existing artifacts; new code reconciles as today |
| Unfinished Claude run during downgrade | Old binary fail-closes. Operator resumes only after re-upgrading. That is the correct fail-closed outcome |
| Controller restart across version change | Profile + artifacts remain source of truth. Snapshot may lack `coding_agent`; ignore it |
| Feature-flagged Claude | Flag off ⇒ start API rejects `claude_code` even if the adapter exists |
| Removing Claude later | Disable flag; historical v2 Claude rows remain INVALID on a Codex-only binary, which is correct. Do not rewrite history to Codex |
| Historical ledger replay | v1 rows stay. v2 rows require the dual reader |

Codex remains default and reference. Rollback of the experiment is “turn the flag off and keep writing v1.” Rollback of the binary is safe **as long as no v2 rows were written**, which is why Codex stays on v1 writes until Claude is actually used.

No SQLite migration. No backfill.

---

## 17. Risk ranking

| Rank | Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|---|
| 1 | Worker selection silently falling back to Codex | Critical | High if worker lives outside v2 profile | Dual-schema; v2 requires field; old binary INVALID; never default on parse failure |
| 2 | False success / wrong `final_message` | Critical | High for Claude/Cursor structured errors | Classify onto legacy stop fields; never mark auth/trust text valid |
| 3 | Final-message loss after crash | High | Certain for Claude if synthesis is RAM-only | Re-parse `stdout.log` after exit; atomic file before finish event |
| 4 | Permission widening via provider config | High | High if `--bare` is not default | Isolated mode; reject Cursor until proven |
| 5 | Dashboard offering unsafe presets | High | High if options stay Codex-global | Worker-filtered options; fail at start |
| 6 | Profile schema incompatibility | High | Certain if v2 is written before dual reader | Reader first; Codex keeps writing v1 |
| 7 | Wrapper hang / orphan child | Medium-High | Medium, worse with subagents | stdin DEVNULL; forbid background modes; pin killpg |
| 8 | Hidden provider-native session state | Medium | Medium | Isolation flags every spawn; artifacts remain CRAX SoT |
| 9 | Mixed-worker same-repo edits | Medium | Operator-chosen, already allowed | Honest UI; no false serialization promise |
| 10 | Model/auth churn | Medium | High in live use | Adapter allowlists; fail closed; no auto-fallback to another worker |
| 11 | Misleading legacy event names | Low-Medium | Certain, but load-bearing | Keep names; tell ChatGPT the real worker in prose |
| 12 | Kickoff still saying “this is Codex” | Medium for planner quality | Certain if unchanged | Parameterize narrative, not sentinel |

---

## 18. Open decisions that still need the operator

1. Accept Claude v1 quality loss from `--bare` (recommended yes).
2. When to spend quota on disposable-repo probes (sections 19–20).
3. Whether mixed-worker same-repo is an advertised product or an advanced operator choice (code already allows mixed writers).
4. Whether Codex should ever cut over to writing schema v2, or keep v1 forever for rollback.
5. Whether a Claude success with nonzero process exit, if proven, may be translated to compatibility `exit_code=0`.
6. Kickoff text change vs keeping ChatGPT’s existing memory of “this is Codex” (recommend honest worker name; keep sentinel).
7. Whether the experimental Claude flag is compile/runtime config or a dashboard hidden selector.
8. Whether CLI `codex-run` grows a worker flag in the same v1 or stays Codex-only until the dashboard path is proven.
9. Stage 8 still open: whether coding CLIs must be serialized for auth/quota, now possibly across vendors.

---

## 19. Live probes required before Claude read-only

Operator-approved spend, disposable repo only, not this worktree, not the daily-driver loop.

Must prove:

1. `--bare` + `--tools Read` (or the documented read-only set) + non-interactive permission mode actually **cannot write**, including via shell/MCP if those would otherwise load.
2. Stream-json produces a durable terminal result on stdout that CRAX can re-parse after killing the parent between child exit and synthesis.
3. Auth failure, logged-out, and rate-limit paths do **not** yield `final_message_status=valid`.
4. `subtype=success` combined with `is_error` / missing result is treated as failure.
5. Process exit code for a true read-only success (needed for planner mapping).
6. stdin inherited vs DEVNULL: Claude must not block waiting for TTY.
7. Cancel/`killpg` of the wrapper group actually stops Claude and does not leave a grandchild writing the disposable repo.
8. Config isolation: without `--bare`, confirm that project/user config can widen tools; with `--bare`, confirm it does not.
9. Concurrent Codex + Claude on one machine: auth/quota interference **[U]**.

---

## 20. Live probes required before workspace-write

Only after read-only is enabled and boring.

1. The proposed write mapping (whatever flags Audit 3 left unproven) can edit files **inside** the repo.
2. It cannot edit **outside** the repo without `danger-full-access`.
3. `dontAsk` / bypass does not upgrade to full host access.
4. `--bare` still blocks MCP/hooks from granting extra write tools.
5. A blocked write is classified as failure, not as a successful final message.
6. Git attribution still works (governance is backend-agnostic, but prove a Claude write is visible to `git_snapshot`).
7. Restore after crash mid-write: uncertain vs complete classification still fail-closed; no replay.

If any of these fail, keep the preset hidden.

---

## 21. Should Cursor remain disabled until after Claude ships?

**Yes.** Claude is the lower-risk first backend: documented isolation (`--bare`, `--tools`, non-interactive permissions), stream-json, structured result. Cursor still has unresolved trust/`--force`/project-config questions and weaker isolation. Shipping Cursor first, or in parallel, doubles probe cost and wrapper risk before the seam is proven with one non-Codex adapter.

---

## 22. Staged implementation outline (not implementation prompts)

1. Codex characterization tests: profile exact-key, missing-profile CLI vs controller, wrapper stdin/pgid, final-message restore.
2. Wrapper hardening under those tests.
3. Dual-schema profile reader; Codex still writes v1.
4. Extract Codex argv/normalize/final-message into a Codex adapter behind the existing function names; no second backend.
5. Prove Codex ratchet: `test_codex_*`, `test_local_controller`, `test_supervision_services`, `test_local_server`.
6. Optional `coding_agent` on start API/options/read model; default Codex; Cursor/Claude rejected.
7. Claude adapter + support table (`read-only` only) + experimental flag + `--bare`.
8. Disposable-repo probes (19).
9. Enable Claude read-only for real runs.
10. Write probes (20); only then add workspace-write to the support table.
11. Cursor design/probes after Claude read-only is stable.
12. Maybe later: v2 writes for Codex, generic event names, generic artifact paths. Not required to have a working second backend.

---

**Bottom line:** the smallest safe architecture change is a versioned `coding_agent` on the existing execution profile, adapters inside the existing `run_codex_exec` spawn/normalize/final-message boundary, legacy `codex_*` events and paths left alone, and Claude read-only as the first extra backend after wrapper hardening and fail-closed probes. Anything wider — controller-as-orchestrator, event rename, nested backend config, inferred worker, or showing unproven write presets — is how the live Codex loop gets destabilized.
