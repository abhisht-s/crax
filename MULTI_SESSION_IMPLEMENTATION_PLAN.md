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
| 5 | Multi-session start and identity rules | **Next** |
| 6 | Restart and durability recovery | Not started |
| 7 | Backend API and dashboard | Not started |
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

Known gap carried forward (unchanged, Stage 6): restore still bails
early when the focused run is terminal, which would drop live siblings.
Stage 4 persistence itself is correct — the snapshot `sessions` map
records every live runtime, verified under a two-live-session start.

---

## Stage 5 — Multi-session start and identity rules

### Goal

Explicit second/third/fourth session creation, enforced server-side.

### Work **[T]**

1. Decide the additional-start shape: dedicated route
   (`/api/runs/start_additional`) vs a flag on the existing start body.
   Decision criteria: today's Start button and any cached client must be
   physically unable to start a second session by accident. Lock the
   decision with a test that the old start body cannot create a second
   session while one is live.
2. Enforce at start, under `_lock`:
   - Hard cap: at most **4** live sessions; the effective cap is
     `max_active_sessions` (default 1 until Stage 8 proves 2, then 4).
   - One live session per `(project_title, chat_title)`; reject with
     `duplicate_chatgpt_conversation` (exists **[C]**; extend tests to
     racing starts: exactly one winner).
   - Same project allowed; same repo allowed; comma in titles rejected
     (exists **[C]**).
   - Ambiguous or unparseable chat identity fails closed at start.
3. Navigation rule: when more than one session is live, every lane slice
   behaves as `allow_destination_navigation=True` regardless of each
   session's checkbox — two chats cannot both already be on screen.
   Single live session keeps its opt-in behavior.
4. Racing duplicate starts (same conversation, simultaneous): one winner,
   loser gets the collision error, ledger holds one run.

### Acceptance

Contract tests: old body cannot double-start; additional start works to
cap; collision and race rules hold; cap-4 enforced even when configured
higher by mistake.

### Files

`agent/local_server.py`, `agent/local_controller.py`,
`agent/run_services.py`, `tests/test_local_server.py`,
`tests/test_local_controller.py`.

---

## Stage 6 — Restart and durability recovery

### Goal

Four mixed session states recover independently after a CRAX restart, and
CRAX stops initiating side effects when persistence is untrustworthy.

### Work **[T]**

1. Restore audit. Known gap **[C]**: `_restore_persisted_session` returns
   early when `active_run_id` is missing or the focused run is
   terminal/replaceable — live sibling sessions in the snapshot would be
   dropped. Fix: restore every non-terminal session in the snapshot
   independently; pick a sane focused run (previous focus if live, else
   any live session); a terminal focused run must not veto siblings.
2. Mixed-state restore test: A was waiting on ChatGPT, B queued for the
   lane, C running Codex, D waiting on approval; kill and restart the
   controller (in-process re-construction against the same ledger).
   Expect: C reconciles via invocation artifacts (finalize, fail, or
   pause-uncertain); D's approval snapshot intact; A and B re-enter the
   wait/queue path rather than failing; nothing double-executes.
3. Queue and mutex hygiene on restore: dead-owner queue claims and mutex
   from the previous process do not deadlock the lane (build on Stage 3's
   stale-owner work).
4. **Persistence safety rule:** when a durable ledger write fails
   (sqlite3 error on commit) on any path that gates an external side
   effect — starting Codex, entering the lane, submitting — the session
   pauses (`blocked`, explicit reason code) instead of proceeding.
   Define the exact detection points; do not sprinkle try/except
   everywhere. A read failure or a snapshot-persist failure must not
   silently continue into a paste.
5. Terminal states absorbing: a restored terminal run never gets a
   worker, never re-enters the lane, and never re-runs Codex — test it.

### Acceptance

Mixed-state restart test passes; persistence-failure test proves no new
side effect after a failed durable write; existing restore tests pass.

### Files

`agent/local_controller.py`, `agent/ledger.py`,
`agent/supervision_services.py`, `tests/test_local_controller.py`.

---

## Stage 7 — Backend API and dashboard

### Goal

Start, watch, approve, retry, and stop up to four sessions without
curling JSON.

### Work **[T]**

- `GET /api/runs` — live sessions: project, chat, repo, controller state,
  Codex status, queue position, whether this run currently owns the lane,
  pending approval, retry/wait reason.
- `/api/runs/<run_id>/progress|events|cancel|retry|approval` — approval
  decisions name a `run_id`.
- Keep `/api/runs/current*` bound to the focused session for
  compatibility; a focus-switch endpoint changes only which run those
  legacy routes address.
- **Changing focus must never pause, tick, or otherwise touch a
  background session.**
- Explicit "start additional session" control (Stage 5's shape); same
  form fields as today.
- Dashboard: up to four session cards (chat/project/repo/state, lane
  ownership, Codex status, approval prompt, wait reason, per-session
  stop/approve/retry); detail view for the focused session. Remote phone
  uses the same APIs — no second protocol.
- Update `tests/test_web_static.py` URL contracts and
  `tests/test_local_server.py`.

### Acceptance

Contract tests for list, per-id routes, and focus semantics; with the
additional-session control unused, the old form starts exactly one run
and the old dashboard flow is unchanged. Browser pass against the
headless/fake path only — live ChatGPT stays Stage 8.

### Files

`agent/local_server.py`, `agent/web_static/app.js`,
`agent/web_static/index.html`, tests above.

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

1. Stage 5: additional-start route vs flag.
2. Stage 6: exact durable-write failure detection points.
3. Stage 8: whether Codex needs serializing after real quota behavior.

Settled in Stage 3: yield-and-requeue reuses the wait-lane backoff
(0.5s doubling to a capped 8s); no separate constants were needed.

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
- (add entries here as stages complete)
