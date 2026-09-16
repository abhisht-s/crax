# Multi-session CRAX: implementation plan (v2)

Staged, executable plan for [MULTI_SESSION_FEATURE.md](MULTI_SESSION_FEATURE.md).
Shared context every stage agent must read first:
[MULTI_SESSION_CONTEXT.md](MULTI_SESSION_CONTEXT.md). Design-audit history:
[MULTI_SESSION_CONCLUSIONS.md](MULTI_SESSION_CONCLUSIONS.md).

Precedence: the feature doc wins on product rules; this file wins on build
order and scope. Where the conclusions doc says the target is two sessions,
this plan supersedes it: the **capability target is four** concurrent
sessions, proven live at two before the cap is raised to four (Stage 8).

This plan replaces the previous A–H plan. Mapping from the old stages:

| Old | Fate |
|---|---|
| A (characterize) | Done; tests landed with Stages 1–2 |
| B (SQLite busy timeout) | Done; `PRAGMA busy_timeout` = 10s, no WAL needed so far |
| C (wait, not failure) | Done; absorbed into Stage 3's already-built portion |
| D (session registry, max 1) | Done; this plan's Stage 2 |
| E (second session) | Split into Stages 3, 4, and 5 |
| F (dashboard + HTTP) | This plan's Stage 7 |
| G (live proof) | This plan's Stage 8b |
| H (merge decision) | Unchanged; after Stage 8 |
| X (capture slicing) | Dropped from scope; only the Stage 8b probe survives |

Work only in `/Users/abhisht/Documents/crax-multi-session` on
`feature/multi-session`. Do not implement a stage until the previous
stage's acceptance is met and the operator agrees to start the next one.

**[C]** means confirmed current code. **[T]** means target.

---

## Invariants no stage may violate

From the feature doc and the context doc:

1. A session is one ChatGPT conversation (`project_title` + `chat_title`)
   plus a repository. Two live sessions must never share that conversation
   pair. Sharing a ChatGPT project or a repository is allowed.
2. Starting or stopping one session must not disturb another.
3. A single session, run alone, must keep behaving as it does today.
4. Waiting on the ChatGPT lane is a normal state, never a failure.
5. Never replay a Codex execution or ChatGPT submission unless the
   previous attempt provably did not happen. Uncertain means pause and
   reconcile, not retry.
6. Destination verification fails closed. No unproven paste, ever.
7. No lease or mutex is taken from an owner that is provably alive.
8. The ledger is the source of truth. Terminal states are absorbing.
9. Never hold `LocalController._lock` across Codex or ChatGPT UI work.
10. No live desktop automation unless the stage says so and the operator
    approves it.

---

## Stage status

| Stage | Title | Status |
|---|---|---|
| 1 | Durable Codex execution | **Done** |
| 2 | Per-run session runtime | **Done** |
| 3 | Shared ChatGPT lane | **Done** |
| 4 | Real concurrent session workers | **Done** |
| 5 | Multi-session start and identity rules | **Done** |
| 6 | Restart and durability recovery | **Done** |
| 7 | Backend API and dashboard | **Done** |
| 8 | Torture testing and live proof | Not started |

Per-stage workflow (all stages): read the root docs fully -> audit the
post-previous-stage code -> confirm the stage still matches the docs ->
implement only that stage -> run focused validation -> review the actual
diff before moving on.

---

## Stage 1 — Durable Codex execution — DONE

One Codex invocation is restart-safe before any concurrency exists.

Delivered **[C]**:

- `agent/codex_invocation.py`, `agent/codex_invocation_wrapper.py`:
  per-invocation wrapper; durable stdout/stderr/final-message/exit
  artifacts; reuse-safe process identity (pid + boot id + start identity
  + pgid); idempotent finalization.
- `reconcile_codex_invocation` on restore: finalize from artifacts when
  provably finished, fail when provably dead, **pause for reconciliation
  when uncertain**. An uncertain execution is never replayed.
- Shutdown handlers terminate active invocations
  (`install_codex_shutdown_handlers`,
  `terminate_all_active_codex_invocations`).

Evidence: `tests/test_codex_invocation.py`, `tests/test_codex_services.py`
pass.

---

## Stage 2 — Per-run session runtime — DONE

Run-specific state is keyed by `run_id`; concurrency is not enabled.

Delivered **[C]**:

- `ControllerSessionRuntime` and `LocalController._sessions` registry:
  per-run worker, cancel event, approval snapshot, controller state,
  retry state, `action_running`, destination, repo, sandbox, navigation
  flag.
- `active_run_id` demoted to the **focused** run; `/api/runs/current*`
  and mirrored controller fields unchanged for one session.
- `DEFAULT_MAX_ACTIVE_SESSIONS = 1`; second start still returns
  `active_run_exists`.
- Live-conversation collision check (`duplicate_chatgpt_conversation`)
  and comma rejection in `RunDestinationBinding` (identity parse needs
  exactly one comma).
- Registry persisted in the controller snapshot (`sessions` map) and
  restored on restart; in-flight sessions restore as `blocked`.
- Internal cancel/approve/retry take `run_id`.

Evidence: `tests/test_local_controller.py`, `tests/test_run_services.py`,
`tests/test_local_server.py` pass (one-run contracts intact).

---

## Stage 3 — Shared ChatGPT lane — DONE

The coordination primitive for the one physical ChatGPT Desktop is
complete, fair, and proven under contention with synthetic run ids —
while still only one real session runs (`max_active_sessions` stays 1).

Delivered **[C]**:

- Three-layer lane in `_run_chatgpt_handoff_transaction`:
  FIFO handoff queue (enqueue -> claim-if-head) -> machine-global desktop
  mutex (`fcntl` file lock with owner liveness identity) -> UI lease.
  Wrong layer order or a second scheduler is a regression.
- Contention on any layer returns `waiting_for_chatgpt=True`
  (`CHATGPT_WAIT_REASON_CODES`); the controller backs off 0.5s doubling
  to 8s (`_wait_for_chatgpt_lane`) and re-plans. No `waiting_for_retry`.
- **Pre-submit lane yielding** (predates this stage's final slice; the
  earlier "remaining work" note claiming it was missing was a stale
  audit): navigation failure, gate failure, and retryable submit failure
  complete the queue entry with
  `chatgpt_handoff_yielded_retryable_ui_failure`, release lease and
  mutex, and return a retryable wait. Uncertain submissions block for
  reconciliation and are never replayed. In-slice attempt budgets bound
  lane occupancy only; the session itself never hard-fails on a
  temporarily unusable UI.
- **Fairness**: oldest ready entry goes first; completion frees the head;
  immediate re-enqueue joins the tail; a fast rerequester cannot starve
  an older waiter; simultaneous claims have one winner
  (`BEGIN IMMEDIATE` serialization).
- **Stale-owner recovery** (liveness evidence only, never elapsed time;
  `process_instance_liveness` three-state verdict, unknown fails closed):
  - Desktop mutex: dead owner's flock evaporates with the process;
    recovery is the normal kernel-atomic acquisition path.
  - UI lease: acquire events now record full owner identity (pid, boot
    id, start identity, pgid); an acquire that finds the lease held by a
    provably dead instance writes one
    `chatgpt_ui_lease_stale_owner_recovered` release event and acquires
    in the same transaction. Legacy pid-only leases recover when the pid
    is gone and wait when the pid is alive but unprovable.
  - Handoff queue: a head claimed by a provably dead instance is expired
    (`chatgpt_handoff_claim_owner_dead`) by the next claimer; a crashed
    run's own dead claim self-heals at re-enqueue; a pending head whose
    run is hard-terminal is expired (`chatgpt_handoff_run_terminal`).
    Terminal runs are refused at enqueue and claim (`RUN_TERMINAL`), and
    supervision stops before claiming.
- Extraction is ledger-only and never enters the lane.

Evidence: `tests/test_chatgpt_handoff_queue.py` (FIFO, tail rejoin,
starvation, claim races, dead-owner and terminal-run recovery),
`tests/test_chatgpt_ui_lease.py` (dead-pid / boot-change / pid-reuse
recovery, live-owner and unknown-owner denial, recovery race, no
time-based expiry), `tests/test_chatgpt_desktop_mutex.py` (liveness
verdict, identifier round-trip, cross-process exclusion),
`tests/test_supervision_services.py` (yield paths, one-lease transaction,
uncertain-submit block, terminal-run guard). One-run controller/server
contracts unchanged.

---

## Stage 4 — Real concurrent session workers — DONE

Independent loop workers genuinely run at the same time. Codex overlaps;
ChatGPT serializes through the Stage 3 lane. First stage where A/B/C/D
coexist — in tests, with fakes, no Desktop.

Delivered **[C]**:

- `max_active_sessions` is honored everywhere and clamped to a product
  hard cap of four (`MAX_ACTIVE_SESSIONS_HARD_CAP = 4`); default stays 1.
  Configuring 1–4 is respected; configuring above 4 clamps to 4. A start
  beyond the effective cap returns `active_run_exists`. No HTTP exposure
  yet (Stage 5).
- Concurrency audit found exactly one remaining singleton collision:
  `start_run` unconditionally dropped the focused session after the
  capacity check, which with capacity above one would have evicted a
  **live** focused session A when starting B. Fixed: only a replaceable
  (terminal) focused session is dropped; a live focused session survives
  a sibling start, and focus simply moves to the new run.
- Everything else in the traced paths (worker creation/cleanup,
  `_automatic_progress_loop`, approval, tick, retry, cancel, failure
  pause, ChatGPT wait/backoff, terminal cleanup) was already keyed by
  explicit `run_id` from Stage 2 and is now proven under real concurrent
  workers. Legitimately global: `LocalController._lock` (registry
  mutations only), the machine-global desktop mutex, the controller
  instance id used in claim-owner identity, and process-shutdown Codex
  termination. Focused-run mirror fields remain compatibility-only; no
  worker path reads them when a runtime exists.
- Stage 1 Codex process registry keying by `run_id` audited as
  sufficient: one worker per run means at most one live invocation per
  run; `terminate_codex_run(run_id)` remains correctly scoped. Not
  changed.
- Isolation proofs (`tests/test_multi_session_concurrency.py`, fakes,
  controllers built with max 2–4): two and four live runtimes coexist;
  fifth start rejected; same project + different chats allowed; same
  repo allowed; same live chat rejected; fake Codex A and B overlap in
  time; A waiting on ChatGPT (and even captive inside its backoff sleep)
  does not stop B; A waiting on approval does not stop B/C; A's
  retryable failure and `waiting_for_retry` do not pause B; a worker
  exception in A mutates only A; cancel A terminates only A's Codex,
  clears only A's approval, and leaves B/C/D workers, cancel flags, and
  states untouched in every tested phase (running Codex, waiting for
  ChatGPT, waiting for approval, waiting for retry); terminal A is
  absorbing (tick and approval refused, worker ceased) while siblings
  continue; focused/current APIs follow the newest start while the
  unfocused background session keeps progressing to completion.
- Stage 3 lane under real session workers (real ledger queue in a
  temporary database): three concurrent controller workers serialize
  through the FIFO handoff queue with at most one claim active at any
  moment and strict enqueue-order slices; cancelling a queued session
  removes only its work (its terminal pending entry is expired by the
  next claimer, never claimed) and a terminal run is refused at both
  enqueue and claim (`RUN_TERMINAL`).
- SQLite under concurrent writers: four threads writing events and
  status updates for different run_ids, and four threads running
  enqueue/claim/complete queue cycles, all succeed within the existing
  10-second busy timeout. No `SQLITE_BUSY` observed; **no WAL needed**,
  ledger settings unchanged.

Evidence: `tests/test_multi_session_concurrency.py` (25 tests) passes;
one-run ratchet (`tests/test_local_controller.py`,
`tests/test_supervision_services.py`, `tests/test_local_server.py`,
`tests/test_run_services.py`) passes; Stage 1
(`tests/test_codex_invocation.py`, `tests/test_codex_services.py`) and
Stage 3 (`tests/test_chatgpt_handoff_queue.py`,
`tests/test_chatgpt_ui_lease.py`, `tests/test_chatgpt_desktop_mutex.py`)
suites remain green.

Known gap recorded at the time (closed in Stage 6): restore still bailed
early when the focused run was terminal, which would drop live siblings.
Stage 4 persistence itself was already correct — the snapshot `sessions`
map records every live runtime, verified under a two-live-session start.

---

## Stage 5 — Multi-session start and identity rules — DONE

Explicit second/third/fourth session creation, enforced server-side.

Delivered **[C]**:

- **Additional-start shape settled: a flag, not a route.** The existing
  `/api/runs/start` body gains an optional boolean `additional_session`
  (type-checked; non-boolean is a 400). A legacy body never carries the
  flag, so today's Start button and any cached client are physically
  unable to start a sibling: with *any* live session, the default start
  returns `active_run_exists` even when capacity is free. An explicit
  `additional_session: true` start is allowed while
  `live_count < max_active_sessions` (hard cap 4) and otherwise returns
  `session_capacity_reached`. Both codes, plus
  `duplicate_chatgpt_conversation`, map to HTTP 409. Repeated/racing
  legacy submits create exactly one session.
- **Cap exposure:** `LocalControllerServer(max_active_sessions=…)` and a
  `--max-active-sessions` CLI flag (validated 1–4) pass the cap to the
  controller constructor; the default stays 1 and the constructor still
  clamps to the hard cap of 4.
- **Atomic same-chat ownership:** new ledger primitive
  `claim_chatgpt_conversation` / `release_chatgpt_conversation_claim`
  (event-sourced, `BEGIN IMMEDIATE`, same style as the UI lease). The
  claim is taken in `start_local_controller_run` right after run creation
  and before profile selection and destination binding, with the caller's
  controller instance id and full process identity recorded. Exactly one
  of two racing starts wins — proven for threads in one controller, for
  two controller instances sharing one ledger database, and for raw
  concurrent claim transactions. The loser returns
  `duplicate_chatgpt_conversation` and never binds the destination, never
  registers a runtime, and never launches Codex. Reclaim happens only when
  the owning run's status is replaceable
  (completed/failed/needs_review/rejected) or the recorded owner process
  is provably dead (three-state liveness verdict; unknown fails closed);
  each reclaim writes an audit release event under the old owner's run.
  Claims are released on start failure after the claim, on cancel, and on
  hard-terminal transitions; needs_review keeps its claim so a resumable
  run is not silently displaced. **Scope honestly recorded:** the claim is
  per ledger database. Separate worktrees (separate `data/agent_ledger.db`
  files) are outside its reach; that scenario remains governed by the
  documented operating rule plus the machine-global desktop mutex, which
  serializes UI slices but does not enforce conversation uniqueness.
- **Identity rules unchanged and re-proven under the new contract:** same
  project + different chats allowed, same repo allowed (no repo lock),
  comma-bearing and empty titles rejected at validation
  (`invalid_destination`), duplicate live conversation rejected.
- **Forced navigation for multi-live mode:** the controller computes the
  effective flag per slice (`_navigation_for_handoff`), leaving the lane
  and destination gate untouched. One live session keeps its opt-in
  checkbox. The moment a sibling start makes live count exceed one, every
  live run's `navigation_forced_multi_live` latch is set (also re-checked
  at handoff time), forcing navigation on every subsequent slice for those
  runs. Settled behavior for the revert question: the latch is **sticky
  for the run's lifetime** — a run that ever coexisted with a live sibling
  keeps forced navigation even after the sibling stops, because the
  sibling may have moved ChatGPT while it was alive. The latch is
  persisted in the controller snapshot and restored on restart.
- **Start failure atomicity:** capacity and duplicate rejections happen
  before run creation (in-process) or before binding (ledger claim); a
  claim followed by a profile or binding failure releases the claim and
  records the existing start-failure event; a failed start leaves no
  runtime, no focused-run change, no worker, and the conversation and
  slot immediately reusable.
- **Focus semantics preserved:** starting B/C/D never stops, cancels, or
  pauses A; focus moves to the newest start and remains UI/default-route
  state only; mirrored fields track the focused runtime.

Evidence: `tests/test_multi_session_start.py` (26 tests: default-vs-
additional contract, capacity, identity, in-process and cross-controller
races, raw claim atomicity/reclaim/release, failure atomicity, forced
navigation and latch persistence/restore);
`tests/test_local_server.py` additions (`additional_session` plumbing and
type checks, 409 mapping, `max_active_sessions` pass-through);
`tests/test_multi_session_concurrency.py` updated (harness starts siblings
explicitly; over-cap reason is `session_capacity_reached`) and green;
one-run ratchet (`tests/test_local_controller.py`,
`tests/test_supervision_services.py`, `tests/test_local_server.py`,
`tests/test_run_services.py`) green; Stage 1/3 suites and
`tests/test_ledger_destination_binding.py` green.

---

## Stage 6 — Restart and durability recovery — DONE

Four mixed session states recover independently after a CRAX restart, and
CRAX stops initiating new external side effects when persistence is
untrustworthy.

Delivered **[C]**:

- **Restore audit and focused-run bug.** The previous
  `_restore_persisted_session` returned early when the focused run was
  missing or terminal/replaceable, which dropped live siblings in the
  snapshot. Restore now discovers candidates from
  `list_restore_candidate_runs()` (ledger run rows whose status is not
  `failed`/`rejected`), overlays the snapshot `sessions` map as runtime
  hints, and unions `list_active_chatgpt_conversation_claims()`.
  `focused_run_id` is UI/default-route state only. Focus restoration:
  previous focus if it is still a live restored session; else the first
  remaining live session in discovery order; else the first restored
  session so `/current` still has something to address; else empty.
- **Restore from durable evidence, not snapshot alone.** Ledger run
  status, planner read model, Codex invocation artifacts, conversation
  claims, and approval integrity outrank the snapshot. In-flight
  snapshot states still map to `blocked` until evidence proves safe
  work. `failed`/`rejected` are restore-absorbing (no runtime, no
  worker). Run status `completed` is mid-loop and is *not* absorbing;
  loop-finished is `read_model.completed`.
- **Mixed-state reconstruction.** Verified ChatGPT submit restores to
  planner `capture_gpt_response` (no resend). Captured text restores to
  `extract_next_prompt` (ledger-only). Extracted prompt with no Codex
  start launches that prompt once under Stage 1 rules. Live invocation
  is observed; complete invocation finalizes idempotently; uncertain
  invocation stays blocked and is never replayed. Matching approval
  snapshots restore; stale snapshots are discarded and rebuilt from
  current evidence, never resurrected. Safely retryable UI/pre-submit
  failures resume automatically with a fresh capped backoff (monotonic
  deadlines are RAM-only). They do **not** require operator Retry.
  Uncertain submit/Codex, capture integrity conflicts, and conversation
  claim conflicts stay blocked for reconciliation. Cancelled / failed /
  rejected / loop-finished remain absorbing and are not resurrected.
- **Conversation claim on resume.** Before `ask_send_to_gpt`,
  `capture_gpt_response`, `ask_run_prompt`, approval execution, or
  retry advancement, the run must own the durable claim for its bound
  chat (`_verify_conversation_claim_for_advancement`). Idempotent if it
  already owns the claim; atomic reclaim if the current owner is
  replaceable or provably dead; `conversation_claim_conflict` if another
  live/resumable session owns the chat. `needs_review` stays reclaimable
  for a *new start* (replaceable-session rule). Resume of the old run
  after that reclaim fails closed — two live owners are impossible.
  Claim replaceable statuses are `failed` / `needs_review` /
  `rejected`; `completed` is excluded because it is mid-loop.
- **Queue reconstruction.** The ChatGPT queue is coordination state, not
  source of truth. Restore does not require stale queue rows to survive.
  A session that still needs ChatGPT re-enters through the planner and
  `enqueue_chatgpt_handoff` (idempotent for an already-pending entry).
  Dead claim owners self-heal on enqueue (Stage 3). Terminal
  (`failed`/`rejected`) runs are refused at enqueue. Uncertain
  submission stays blocked; verified submit resumes capture.
- **Workers.** Daemon threads do not survive process death. Restore
  spawns a per-run resume worker only when evidence proves safe
  continuable work, after the registry is fully built. Restoring A does
  not alter B/C/D workers. Simultaneous ChatGPT-ready sessions enter
  Stage 3's fair queue rather than racing the desktop. If
  `list_restore_candidate_runs` itself fails, restore fails closed:
  durability-blocked, no resume workers, never a healthy-empty boot.
- **Global durability block.** `DurabilityGuardedLedger` is the single
  detection point (no scattered try/except). Correctness-critical
  writes: `create_run`, `add_event`, `update_run_status`, ChatGPT
  enqueue/claim/complete/block, UI lease acquire/release, conversation
  claim/release, destination and execution-profile binding. sqlite
  errors and atomic `operational_failure` results flip
  `LedgerDurabilityGuard`. Non-critical: `add_codex_progress_event`,
  `save_local_controller_snapshot`. While blocked: no new Codex start,
  no new ChatGPT submit, no new session start; sessions are not marked
  terminal; wait uses the ChatGPT lane backoff; `/current` exposes
  `ledger_durability`. Recovery is `check_durable_write_health` (a
  committed `BEGIN IMMEDIATE` write to `durability_health`), not a
  successful read. After recovery, open Codex invocations are
  reconciled before new mutations. Already-running work is not killed
  solely because the ledger became unavailable.
- **Sibling-start drop policy.** An additional start at cap>1 no longer
  evicts a finished focused sibling from the registry. Cap-1 and legacy
  replacement still clear the focused slot.
- **Cross-worktree limitation unchanged.** Conversation uniqueness is
  atomic only inside one shared ledger DB. Stage 6 did not add a
  machine-scoped conversation lock.

Evidence: `tests/test_multi_session_restore.py` (terminal-focused
sibling restore, mixed A/B/C/D states, capture/extract/send, Codex
live/complete/uncertain, auto-retry after restart, uncertain submit
does not resend, claim conflict does not auto-advance, ledger
candidate discovery when the snapshot is missing/corrupt, loop-finished
and cancelled runs not resurrected, enumeration failure fail-closed,
approval/stale/retry, queue loss and idempotency, worker uniqueness,
needs_review claim conflict, global durability block/recover); Stage
4/5 suites updated for mid-loop `completed` and cancel-wait isolation;
`tests/test_chatgpt_handoff_queue.py` (mid-loop `completed` may
enqueue; `failed` remains queue-terminal);
`tests/test_multi_session_start.py` (`completed` does not give up the
claim). Stage 1 (`tests/test_codex_invocation.py`,
`tests/test_codex_services.py`), Stage 3 (handoff queue, UI lease,
desktop mutex), Stage 4 concurrency, Stage 5 start, and the one-run
ratchet (`tests/test_local_controller.py`,
`tests/test_supervision_services.py`, `tests/test_local_server.py`,
`tests/test_run_services.py`,
`tests/test_ledger_destination_binding.py`) remain green.

### Files

`agent/local_controller.py`, `agent/ledger.py`, `agent/local_server.py`,
`tests/test_multi_session_restore.py`, plus focused updates in
`tests/test_chatgpt_handoff_queue.py`,
`tests/test_multi_session_start.py`,
`tests/test_multi_session_concurrency.py`,
`tests/test_local_controller.py`. `agent/supervision_services.py` was
audited and not changed.

---

## Stage 7 — Backend API and dashboard — DONE

### Goal

Start, watch, approve, retry, and stop up to four sessions without
curling JSON. Presentation only: Stages 1–6 engine semantics are
unchanged.

### Delivered **[C]**

- **Session list.** `GET /api/runs` → `LocalController.list_sessions()`.
  Registered live/resumable sessions only (in-process `_sessions`, plus
  a focused id not yet in the map). Not a historical catalog. Each
  entry: `run_id`, project/chat/repo, `focused`/`live`/`terminal`,
  controller/planner fields, `codex_running`, ChatGPT wait/lease/queue
  attribution, approval, retry/reconciliation flags, `operator_status`
  derived by `session_operator_view`, latest failure preview, timestamps.
  List metadata: `focused_run_id`, `max_active_sessions`,
  `live_session_count`, `session_capacity_remaining`,
  `ledger_durability`, `chatgpt_lane` (lease owner + FIFO snapshot from
  read-only `ledger.describe_chatgpt_handoff_queue()`).
- **Per-run reads.** `GET /api/runs/<run_id>` (`get_run_state`) and
  `GET /api/runs/<run_id>/progress|events` reuse existing controller
  read models. Unknown ids → `run_not_found` / HTTP 404.
  `/api/runs/current*` remain focused-run aliases.
- **Focus.** `POST /api/runs/<run_id>/focus` (`focus_run`) updates
  `active_run_id` and the mirrored compatibility fields only. Proven not
  to pause, cancel, or change sibling worker/queue/approval state.
- **Per-run controls.** `POST /api/runs/<run_id>/{cancel,approval,retry,tick}`
  call the existing per-run controller methods. No fall-through to the
  focused sibling. Legacy `/api/runs/current/cancel|retry`,
  `/api/approval`, `/api/tick` still target focus. Per-run mutating
  routes are `control` scope.
- **Additional start UX.** Default Start still omits
  `additional_session`. **Start additional session** appears when at
  least one session is live, sends `additional_session: true`, and is
  disabled at configured cap or while durability-blocked. Header shows
  `live / max active`.
- **Dashboard.** Session cards + focused detail. ChatGPT waiting uses
  wait tone and “This is expected.” Approvals/Stop/Retry bind `run_id`.
  Conversation-claim conflict is labeled as another session owning the
  chat; Retry is withheld for that reason and for
  `reconcile` / `retry_after_fix` / `review_required`. Global durability
  banner is separate from per-session failures.
- **SSE / polling.** Selected-run SSE plus JSON progress poll; list
  refresh via `GET /api/runs`. Focus switch resets progress memory and
  drops mismatched `run_id` events. No EventSource/localStorage/innerHTML.
- **Remote.** Same authenticated server and authorization rules. No
  second remote state model.

### Acceptance

Contract tests for list, per-id routes, focus-only switching, isolated
stop/approve/reject/retry, unknown-id fail-closed, Stage 5 additional
start, capacity, durability visibility, and queue attribution. Static
dashboard contracts cover cards, additional start, expected ChatGPT
wait, per-session actions, and durability banner. With the additional
control unused, the old form still starts exactly one run.
`/api/runs/current*` remain aliases. Stage 4/5/6 suites remain green.
Live ChatGPT stays Stage 8.

### Files

`agent/local_controller.py`, `agent/local_server.py`, `agent/ledger.py`,
`agent/web_static/app.js`, `agent/web_static/index.html`,
`agent/web_static/style.css`, `tests/test_multi_session_dashboard.py`,
`tests/test_local_server.py`, `tests/test_web_static.py`,
`tests/test_chatgpt_handoff_queue.py`.

---

## Stage 8 — Torture testing and live proof

### 8a — Torture (fakes, no Desktop)

Deliberately ugly cases, each a repeatable test:

- A monopolizes the lane (long fake capture) while B/C/D queue; release
  order is FIFO; nobody starves; nobody enters `waiting_for_retry`.
- Stop A mid-lane-wait, mid-Codex, and mid-approval; B/C/D unaffected in
  each case.
- Codex crash (nonzero exit, killed pid, vanished pid) per session.
- Uncertain submission on A: A pauses for reconciliation; the lane frees;
  B proceeds.
- CRAX SIGKILL and restart from mixed states (Stage 6's matrix, but
  under load).
- Duplicate start races; same-repo concurrent edits (both complete;
  evidence attribution may interleave — assert isolation, not clean
  attribution).
- Ledger failure mid-flight (Stage 6's rule under concurrency).
- Stale mutex/queue/lease from a killed sibling process.

### 8b — Live proof (operator-approved, explicit)

Preconditions: production loop in `agent-gpt-codex-loop` **stopped**;
this worktree's dashboard and `.venv` only; distinct ChatGPT chats,
comma-free titles; operator has approved live desktop automation.

Script:

1. Session A completes one full round alone.
2. Start B (different chat). While A is in Codex, B runs Codex. While A
   holds the lane, B waits — never fails — then pastes.
3. Every paste: window identity is that session's `"<chat>, <project>"`.
   A's marker never appears in B's chat, and the reverse.
4. Stop A; B completes a round.
5. Repeat with both sessions on the **same** repo.
6. **Cap gate:** only after two live sessions pass overnight do we raise
   the live cap toward four, then repeat the script with three and four.
7. **Probe (record, do not depend):** while ChatGPT generates in A's
   chat, switch to B's chat. Does A's reply finish in the background?
   Write the answer into this file's amendment log. It bounds worst-case
   lane hold time; it does not gate the feature.
8. **[U]** If concurrent Codex CLIs auth-lock or starve each other,
   record it; serializing Codex would be a deliberate follow-up decision,
   not an improvised patch.

Fail closed: a wrong-chat paste, a sibling in `waiting_for_retry` because
the lane was busy, or stop-A killing B's Codex are Stage 3–6 bugs. Fix
there; do not proceed.

### Acceptance

8a suite green; 8b script passes at two sessions, then at four. Only then
is multi-session production-ready. Merge to `main` remains a separate,
explicit decision, with the default cap still 1 on merge.

---

## Closed decisions (do not reopen in code without asking)

1. Same repository allowed, including two writers. No start-time repo lock.
2. Same ChatGPT project allowed.
3. Same ChatGPT conversation forbidden among live sessions.
4. Additional sessions are explicit; default `max_active_sessions = 1`.
5. Capability target is four sessions; live proof gates 1 -> 2 -> 4.
6. With more than one live session, every lane slice navigates.
7. Capture slicing / switch-during-think is out of scope; only the 8b
   probe records the underlying fact.
8. The handoff queue is the one scheduler. No second queue.
9. Uncertain side effects pause for reconciliation. No attempt-count
   giveups on safely recoverable waits; no blind retries on uncertainty.

## Open decisions (settle inside the named stage)

1. Stage 8: whether Codex needs serializing after real quota behavior.

Settled in Stage 3: yield-and-requeue reuses the wait-lane backoff
(0.5s doubling to a capped 8s); no separate constants were needed.

Settled in Stage 5: additional starts are a boolean flag
(`additional_session`) on the existing start body, not a dedicated route —
a legacy body physically cannot carry it. Forced navigation latches per
run once it has coexisted with a live sibling and never reverts for that
run. Same-chat uniqueness is atomic per ledger database via the
conversation claim; cross-worktree uniqueness is documented as out of the
claim's reach.

Settled in Stage 6: correctness-critical durable writes are detected at
the controller ledger wrapper (`DurabilityGuardedLedger`) rather than
scattered try/except blocks; telemetry/progress and the controller
snapshot are non-critical. Recovery is a dedicated
`check_durable_write_health` write, not a successful read. `needs_review`
remains reclaimable for a new start; a resumed/retried run must own the
conversation claim before advancement and fails closed otherwise. Run
status `completed` is mid-loop (not queue-terminal, not claim-
reclaimable); loop-finished is planner `read_model.completed`. Focus on
restore is UI-only and never determines which sessions exist.

## Amendment log

- 2026-09-16: Plan v2 replaces the A–H plan. Stages 1–2 recorded done
  (evidence: `codex_invocation` + controller-registry test suites green).
  Old Stage A queue-fitness amendment recorded implicitly: the ledger
  handoff queue was characterized (`tests/test_chatgpt_handoff_queue.py`)
  and wired into the lane; it is the production scheduler.
- 2026-09-16: Stage 3 complete. Audit correction: pre-submit lane
  yielding was already implemented and tested before this slice; the
  plan's "remaining work" item 1 was stale. This slice added: full owner
  identity on UI lease acquire events; automatic dead-owner recovery for
  the UI lease and the handoff queue (three-state liveness, unknown fails
  closed, race-safe under `BEGIN IMMEDIATE`); terminal-run refusal and
  expiry in the queue (`RUN_TERMINAL`); fairness/starvation/race tests.
  SQLite unchanged (busy timeout 10s, rollback journal, no WAL needed).
  One characterization test updated: the "no time-based expiry" lease
  test now pins a live-but-unprovable owner, because its fabricated dead
  pid is now correctly recoverable. `max_active_sessions` remains 1; a
  second start is still rejected.
- 2026-09-16: Stage 4 complete. Audit finding: the only remaining
  concurrency collision was `start_run` unconditionally dropping the
  focused session after the capacity check; with capacity above one this
  would have evicted a live focused session. Fixed to drop only a
  replaceable (terminal) focused session. Added
  `MAX_ACTIVE_SESSIONS_HARD_CAP = 4` (configured caps clamp to it;
  default stays 1). New `tests/test_multi_session_concurrency.py` proves
  2–4 real concurrent controller workers with fake Codex/ChatGPT
  services, full cancellation/approval/retry/failure isolation, Stage 3
  lane FIFO serialization under real session workers against the real
  ledger queue, terminal-run lane refusal, and stable concurrent SQLite
  writers. SQLite unchanged (busy timeout 10s, rollback journal, no WAL
  needed — concurrent-writer tests hit no `SQLITE_BUSY`). Stage 1 Codex
  process registry keying by `run_id` audited and kept. The Stage 6
  restore gap (terminal focused run drops live siblings on restore) is
  unchanged and still deferred; snapshot persistence of all live
  runtimes was verified correct.
- 2026-09-16: Stage 5 complete. Audit findings: there was no external way
  to request an additional session (any over-cap start returned
  `active_run_exists`); same-chat uniqueness was in-process only
  (registry check under `_lock`), not atomic across controller instances
  or processes; destination binding recorded per-run identity with no
  cross-run uniqueness; navigation flowed from the persisted run-started
  event through the read model into three supervision call sites; on start
  failure the created run kept its non-terminal status but was never
  registered, so no phantom live runtime existed. Delivered: the
  `additional_session` start flag with a conservative legacy default and
  `session_capacity_reached`; server/CLI cap exposure
  (`--max-active-sessions`, clamp to 4 unchanged); the atomic per-ledger
  conversation claim with liveness/replaceable-status reclaim and release
  on cancel/hard-terminal/failed-start; and the sticky per-run forced-
  navigation latch for multi-live mode, persisted and restored. Lane,
  queue, gate, and Stage 4 registry untouched. New suite
  `tests/test_multi_session_start.py`; Stage 4 harness now starts siblings
  explicitly. Known gaps recorded, not absorbed: cross-worktree
  (separate-ledger) conversation uniqueness is not enforced in code; a
  needs_review run resumed via retry after its conversation was reclaimed
  by a new session could produce two live sessions on one chat — this
  pre-existing replaceable-status semantics was settled in Stage 6
  (re-verify/reclaim ownership before resumed advancement; fail closed).
- 2026-09-16: Stage 6 complete. Restore audit confirmed the focused-run
  early-return bug (terminal focused A dropped live B/C/D) and that
  `completed` is mid-loop, not queue-terminal or claim-reclaimable.
  Delivered: independent mixed-state restore from snapshot plus active
  conversation claims; focus is UI-only; claim-on-resume fail-closed
  (`conversation_claim_conflict`); queue reconstructed from planner
  evidence (idempotent enqueue); workers spawned only for proven safe
  work; global `LedgerDurabilityGuard` on correctness-critical writes
  with `check_durable_write_health` recovery; snapshot/progress writes
  classified non-critical. Additional start at cap>1 no longer drops a
  finished focused sibling. New suite `tests/test_multi_session_restore.py`.
  Cross-worktree conversation uniqueness remains out of scope.
- 2026-09-16: Stage 6 recovery-gap patch. Safely retryable restored work
  now auto-resumes with a fresh capped backoff instead of
  `manual_retry_required`. Authoritative restore candidates come from
  `list_restore_candidate_runs` (ledger run rows); snapshot and claims
  are hints. Loop-finished runs are not resurrected. Authoritative
  listing failure fail-closes via the durability guard rather than
  booting empty and healthy. Remaining notes: `retry_after_fix`
  (deterministic payload/integrity blockers) still does not auto-retry;
  abandoned `created` runs with no controller-start configuration are
  not restored; cross-worktree conversation uniqueness is unchanged.
- 2026-09-16: Stage 7 complete. No Stage 7 architecture conflict with
  the docs: focus stays UI/default routing; additional start stays the
  Stage 5 `additional_session` flag; `/current` stays a focused alias.
  Delivered the session-list read model, per-run read/control/focus
  routes, dashboard session cards + detail, ChatGPT lane observability
  from the existing queue/lease, selected-run SSE plus list polling,
  global durability banner, and conversation-claim visibility. Engine
  semantics were not redesigned. Remaining: Stage 8 torture and live
  proof; no historical analytics product; recently terminal runs are
  visible only while they remain in the in-process registry.
