# Multi-session CRAX: shared context

Read this document before working on any stage of
[MULTI_SESSION_IMPLEMENTATION_PLAN.md](MULTI_SESSION_IMPLEMENTATION_PLAN.md).
It explains what we are building, the philosophy every stage must obey, and
the current shape of the code so a stage agent can judge whether its task
still makes sense against reality.

Classification used throughout (same convention as
[MULTI_SESSION_CONCLUSIONS.md](MULTI_SESSION_CONCLUSIONS.md)):

- **[C]** Confirmed in authoritative `agent/` code (not `build/lib/agent`)
- **[I]** Inferred from that code
- **[T]** Target architecture; not implemented yet
- **[U]** Unknown without a live prototype (ChatGPT Desktop, Codex quota)

## Reading order for a stage agent

1. `AGENTS.md` — repository operating rules (always applies).
2. This document — mission, philosophy, current architecture.
3. `MULTI_SESSION_IMPLEMENTATION_PLAN.md` — your stage's goal, work,
   tests, and acceptance. Read the whole plan, then your stage in detail.
4. `MULTI_SESSION_FEATURE.md` — product rules (what a session is, what
   must never happen).
5. `MULTI_SESSION_CONCLUSIONS.md` — the original design audit. Historical
   but still accurate about single-session mechanics; where it says the
   target is two sessions, the implementation plan supersedes it.
6. The code files named in your stage's "files" table, plus the tests that
   already cover them.

If the plan and the code disagree, stop and audit before implementing. If
the plan and the feature doc disagree on a product rule, the feature doc
wins; the plan wins on build order.

## The two checkouts

| | Live loop | This work |
|---|---|---|
| Folder | `/Users/abhisht/Documents/agent-gpt-codex-loop` | `/Users/abhisht/Documents/crax-multi-session` |
| Branch | `main` | `feature/multi-session` |
| Role | Daily driver; do not touch | Plan, implement, prove |

**[C]** The ledger path `data/agent_ledger.db` is relative to the process
cwd, so each folder has its own database and its own `.venv`. Disk
isolation does **not** isolate ChatGPT Desktop, the clipboard, or macOS
Accessibility. Never run this worktree's handoff while the live loop is
using ChatGPT. The machine-global desktop mutex (below) enforces this in
code, but the operating rule stands regardless.

## What we are building

CRAX today runs one supervised loop: one ChatGPT chat directs Codex in one
repository, overnight, without babysitting. We are turning it into a system
that can run **up to four** independent loops at once on one Mac.

Each session is bound to:

1. **A ChatGPT chat** — the conversation Codex reports to and takes the
   next prompt from. This is the session's identity.
2. **A ChatGPT project** — the folder that chat lives in. Sessions may
   share a project.
3. **A repository** — the codebase Codex edits. Sessions may share a
   repository, including two workspace-write sessions on one path. That is
   an operator's deliberate choice; git attribution may interleave and we
   accept it. CRAX guarantees session isolation and routing correctness,
   not serialization of a shared working tree.

The one hard rule: **two live sessions must never share a ChatGPT
conversation** (`project_title` + `chat_title`). Mixing conversations —
pasting one session's Codex report into another session's chat, or
capturing the wrong reply — is the failure mode everything else is
designed around.

Codex work overlaps freely across sessions. The single physical ChatGPT
Desktop is one fair, serialized, machine-global lane that sessions take
turns on.

## The philosophy (every stage must preserve these)

1. **Each session is a stubborn, patient state machine.** It keeps making
   progress without babysitting. Waiting is a normal state, not a failure.
2. **Safely recoverable problems are retried indefinitely with capped
   backoff**, not abandoned after an arbitrary attempt count. "Safely
   recoverable" means retrying cannot duplicate a side effect: lease/queue
   contention, a busy desktop, a navigation failure *before* anything was
   submitted.
3. **Never guess when identity or side effects are uncertain.** If we
   cannot prove which chat is on screen, do not paste. If we cannot prove
   a Codex execution or ChatGPT submission did not happen, do not replay
   it — pause the session for reconciliation or operator review instead.
4. **The ledger and durable invocation artifacts are the source of truth,
   not RAM.** A crash or restart must reconstruct each session from
   evidence. Terminal states are absorbing. If durable persistence cannot
   be trusted, CRAX must not initiate new external side effects.
5. **Failures are isolated per session.** Every worker, cancel flag,
   approval snapshot, retry state, and Codex process is keyed by `run_id`.
   Session A breaking, blocking, or being cancelled must not harm B, C,
   or D.
6. **The ChatGPT lane is fair.** FCFS via a FIFO queue, no starvation, no
   busy-spinning, no lease stealing from an owner that is provably alive.
   Stale locks from dead processes are reclaimable; locks held by live
   processes are inviolable.
7. **Fail closed at the desktop.** Destination verification (window title
   `"<chat>, <project>"`, single composer) gates every submit. A wrong or
   unproven destination aborts the slice; it never "probably" pastes.

## Current architecture (post Stage 4, code-confirmed)

One dashboard process owns everything. The loop per session:

ChatGPT issues a prompt -> Codex executes in the repo -> governance records
evidence -> the supervision planner picks the next action -> CRAX pastes the
Codex report into that session's chat -> captures ChatGPT's reply ->
extracts the next prompt between `BEGIN_NEXT_CODEX_PROMPT` /
`END_NEXT_CODEX_PROMPT` sentinels -> repeat.

### Controller: session registry **[C]**

`agent/local_controller.py`:

- `ControllerSessionRuntime` — per-run state: worker thread, cancel event,
  `action_running`, pending approval, controller state, repo, sandbox,
  destination titles, navigation flag, ChatGPT wait counters.
- `LocalController._sessions: dict[run_id, ControllerSessionRuntime]` —
  the registry. Real concurrency is enabled up to `max_active_sessions`
  (constructor argument, default `DEFAULT_MAX_ACTIVE_SESSIONS = 1`,
  clamped to the product hard cap `MAX_ACTIVE_SESSIONS_HARD_CAP = 4`).
  Stage 4 proved 2–4 concurrent workers with fakes; there is still no
  HTTP/API way to raise the cap (Stage 5).
- `active_run_id` is the **focused** run. Legacy `/api/runs/current*`
  behavior and the mirrored top-level controller fields follow the focused
  runtime (`_apply_focused_runtime`). Focus is compatibility only: a
  background (unfocused) session keeps progressing, and no worker path
  reads the mirror fields when a runtime exists.
- `start_run` rejects: over-cap starts (`active_run_exists`), duplicate
  live `(project_title, chat_title)` (`duplicate_chatgpt_conversation`).
  A new start drops only a **replaceable (terminal)** focused session; a
  live focused session survives a sibling start and focus moves to the
  new run.
- Cancel, approve, and retry take a `run_id` internally, and their
  isolation is proven under real concurrent workers
  (`tests/test_multi_session_concurrency.py`): cancel A terminates only
  A's Codex, invalidates only A's queued lane work, clears only A's
  approval; deciding A's approval leaves siblings' snapshots untouched;
  one session's retryable failure, backoff sleep, or worker exception
  never changes a sibling's state.
- The full registry is persisted in the controller snapshot
  (`_persist_session_locked` writes a `sessions` map) and restored on
  restart; in-flight sessions come back as `blocked`, as before.
- Never hold `LocalController._lock` across Codex or ChatGPT UI work.

### Durable Codex execution **[C]**

`agent/codex_invocation.py` (+ `codex_invocation_wrapper.py`):

- Every Codex run is a tracked invocation with durable artifacts
  (stdout/stderr/final message/exit status) that survive process death.
- Process identity is reuse-safe: pid + boot id + process start identity +
  pgid. A recycled pid cannot be mistaken for a live invocation.
- Finalization is idempotent. `reconcile_codex_invocation` runs on
  restore: a provably-finished invocation is finalized from artifacts; a
  provably-dead one is failed; an **uncertain** one pauses the session for
  reconciliation. An uncertain execution is never replayed.
- `terminate_codex_run(run_id)` kills only that run's process.
  Server shutdown terminates all active invocations
  (`terminate_all_active_codex_invocations`).

### The ChatGPT lane **[C]**

The lane is three layers, acquired in order inside
`supervision_services._run_chatgpt_handoff_transaction`:

1. **Handoff queue** (ledger, FIFO by sequence): `enqueue_chatgpt_handoff`
   then `claim_chatgpt_handoff_for_run`. Claim succeeds only when this run
   is the pending head. Not-head or head-already-claimed returns a **wait**
   result. One active entry per run. Completion (`complete` / `block`)
   frees the head. A head claimed by a provably dead process instance, or
   a pending head whose run is hard-terminal (`completed` / `failed` /
   `rejected`), is expired in-transaction by the next claimer with an
   audit event; live or unproven owners are never touched. Terminal runs
   are refused at enqueue and claim (`run_terminal`), so cancelled work
   can never perform a handoff.
2. **Desktop mutex** (`agent/chatgpt_desktop_mutex.py`): machine-global
   `fcntl` file lock at
   `~/Library/Application Support/crax/chatgpt-desktop.lock` with owner
   identity (pid, boot id, start identity, pgid). Protects the one
   physical ChatGPT Desktop across *processes* (for example this
   worktree's dashboard vs the live one). Held by a live owner -> wait.
   A dead owner's flock evaporates with the process, so recovery returns
   through the normal kernel-atomic acquisition path.
3. **UI lease** (ledger): the original per-database lease. Acquire events
   record the full owner process identity (pid, boot id, start identity,
   pgid). Already held by a live or unproven owner -> wait, with no
   time-based expiry. Held by a **provably dead** owner (boot changed, pid
   gone, or pid reused with a different start identity) -> the acquiring
   transaction writes one stale-release audit event
   (`chatgpt_ui_lease_stale_owner_recovered`) and acquires normally, all
   inside one `BEGIN IMMEDIATE` transaction so racing discoverers have
   exactly one winner. The guarded manual release path still exists.

Owner liveness is a shared three-state verdict
(`process_instance_liveness` in `chatgpt_desktop_mutex.py`): `dead` only
on provable evidence, `live` only on exact instance match, `unknown`
otherwise — and `unknown` always fails closed as a wait. Elapsed time is
never evidence.

Inside the lane: optional navigation (only when
`allow_destination_navigation`; send path retries the whole
navigate -> verify -> submit sequence up to a bounded attempt budget) ->
read-only destination gate -> paste -> submit -> **unbounded capture** in
the same leased transaction (marker visible, complete sentinel block, text
stable) -> release lease -> complete or block the queue entry -> release
mutex.

**Pre-submit lane yielding [C]:** when navigation, the destination gate,
or a retryable submit failure exhausts the in-slice attempt budget and
nothing was submitted, the transaction completes its queue entry with
`chatgpt_handoff_yielded_retryable_ui_failure`, releases the lease and
mutex, and returns a retryable wait — the session backs off, re-enqueues
at the tail, and retries; it is never marked failed for a temporarily
unusable UI. The in-slice attempt bounds protect the shared lane, not an
excuse to abandon the session. A submit whose outcome is **uncertain**
(`_chatgpt_submit_is_uncertain`) is the opposite: the queue entry is
blocked and the session pauses for reconciliation; it is never replayed.

**[C]** Post-submit ambiguity is detected (`_chatgpt_submit_is_uncertain`)
and is *not* a retryable wait: an uncertain submission must never be
resubmitted on guesswork.

**[C]** `extract_next_prompt` is ledger-only: `_run_extract_next_prompt`
parses the captured text without entering the lane. Extraction never
occupies the desktop.

### Wait, not failure **[C]**

`CHATGPT_WAIT_REASON_CODES` in `supervision_services.py`:

- `chatgpt_ui_lease_already_held`
- `chatgpt_desktop_mutex_already_held`
- `chatgpt_handoff_queue_not_head`
- `chatgpt_handoff_queue_head_already_claimed`

These produce `ok=True, waiting_for_chatgpt=True` results. The controller's
`_wait_for_chatgpt_lane` sleeps with exponential backoff
(`CHATGPT_WAIT_INITIAL_SECONDS = 0.5` doubling to
`CHATGPT_WAIT_MAX_SECONDS = 8.0`) and re-plans. The session never enters
`waiting_for_retry` because a sibling holds the lane. All other failures
still go through `_pause_for_action_failure` as before.

### Ledger **[C]**

`agent/ledger.py`: SQLite, rollback journal (no WAL), 10-second busy
timeout (`SQLITE_BUSY_TIMEOUT_SECONDS`), `BEGIN IMMEDIATE` on atomic
paths, per-call connections. Runs, per-run events, destination bindings,
prompt artifacts under `data/runs/<run_id>/`, the UI lease, the handoff
queue, and the controller snapshot all live here. Stage 4 concurrent
multi-run writer tests (four threads of events/status updates and four
threads of queue enqueue/claim/complete cycles) pass with these settings
unchanged; no `SQLITE_BUSY` was observed, so WAL remains unnecessary.

`agent/run_services.py`: `RunDestinationBinding` rejects empty titles and
titles containing a comma (the window-identity parser splits on exactly
one comma).

## What is *not* built yet **[T]**

- No way to start a second session in the product: no API flag or route
  raises `max_active_sessions` above 1 (Stage 5). The controller itself
  now supports and is tested at 2–4.
- No forced-navigation rule for multiple live sessions yet: with more
  than one live session every lane slice must navigate regardless of the
  per-session checkbox (Stage 5).
- Restart recovery is per the single-session contract; mixed multi-session
  restore states are untested, and restore bails early when the focused
  run is terminal, which would drop live siblings (Stage 6).
- No "ledger unwritable -> freeze new side effects" rule (Stage 6).
- No session-list API or dashboard (Stage 7).
- No four-session torture or live proof (Stage 8).

## Known unknowns **[U]**

- Whether two or more concurrent Codex CLIs on one Mac share auth state or
  rate limits. Prove at two sessions before enabling four.
- Whether Classic ChatGPT keeps generating a reply when its chat loses
  focus. This bounds worst-case lane hold time and gates any future
  capture-slicing idea. Record the answer during Stage 8 live proof; do
  not depend on it.

## Vocabulary

| Term | Meaning |
|---|---|
| Session | One live loop: `run_id` + ChatGPT conversation + repo + worker |
| Focused run | The `run_id` that legacy `/current` APIs and the single-run dashboard panel address |
| Lane | The serialized path to ChatGPT Desktop: queue -> mutex -> lease -> verified UI slice |
| Slice | One lane occupancy: navigate/gate/submit or gate/capture, then release |
| Wait | A retryable non-failure (`waiting_for_chatgpt=True`); backoff then re-plan |
| Uncertain | A side effect that may or may not have happened; never replay, always reconcile |
| Terminal | `completed`, `failed`, `needs_review`, `rejected` — absorbing states |

## How to work a stage

1. Read the documents in the order above.
2. Audit the code the stage touches. The plan records what was true when
   written; the code may have moved. If the stage's premise no longer
   holds, say so before implementing.
3. Characterize existing behavior with focused tests before changing it.
4. Implement only your stage. Do not reach into a later stage's work, even
   when it is tempting.
5. Run the focused test modules for the files you touched, plus the
   compatibility ratchet: `tests/test_local_controller.py`,
   `tests/test_supervision_services.py`, `tests/test_local_server.py`.
   Use this worktree's own `.venv` (stdlib `unittest`).
6. Update the plan's stage status and amendment log. If you changed a
   documented behavior, update this document too.
7. No live desktop automation (ChatGPT, Accessibility, clipboard, paste)
   unless the stage explicitly says so **and** the operator has approved
   that live action.
8. Never invoke Codex from inside Codex. Do not install dependencies
   without explicit approval. Commands in docs use literal ASCII flags
   such as `--help` and `--confirm-run`.
