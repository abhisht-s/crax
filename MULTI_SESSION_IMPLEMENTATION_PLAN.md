# Multi-session CRAX: implementation plan

Staged, executable plan for [MULTI_SESSION_FEATURE.md](MULTI_SESSION_FEATURE.md).
Design notes live in [MULTI_SESSION_CONCLUSIONS.md](MULTI_SESSION_CONCLUSIONS.md).
If those disagree, **the feature doc wins** on product rules; this file wins on
build order.

Work only in `/Users/abhisht/Documents/crax-multi-session` on
`feature/multi-session`. Do not implement a stage until the previous stage’s
acceptance is met and we agree to start the next one.

**[C]** means current code. **[T]** means this plan.

---

## Product rules this plan must not violate

From the feature doc:

- A session is one ChatGPT **conversation** (project title + chat title) plus a
  repo.
- Two live sessions **must not** share that conversation pair.
- Two live sessions **may** share a ChatGPT project.
- Two live sessions **may** share a repository, including two writers.
- Starting or stopping one session must not take the other down.
- A single session, run alone, must keep behaving as it does today.

---

## What “executable” means here

The previous draft treated “shorten the ChatGPT capture wait” as the unlock.
Walking it against the code, that is **not** what makes two sessions possible,
and it can make them worse.

**[C]** Two facts that decide the order:

1. `_ACTIVE_CODEX_PROCESSES` is already keyed by `run_id`. Two Codex jobs can
   exist. The controller never starts the second one (`active_run_exists`, one
   `current_worker`, one `action_running`, one `cancel_requested`).
2. If ChatGPT UI lease acquire fails, `_chatgpt_lease_denied_result` returns
   `ok=False` and `blocked=True`. `_automatic_progress_loop` then calls
   `_pause_for_action_failure` and **returns**. That session is dead until a
   human hits retry. A second session that needs ChatGPT while the first is
   capturing would not wait. It would fail.

So the feature lands when:

- each session has its own worker, cancel, and approval;
- Codex on session B runs while session A is in Codex **or** waiting on
  ChatGPT;
- needing ChatGPT while the lease is held is a **wait**, not a failure;
- the only uniqueness check is the live ChatGPT conversation.

Releasing the lease during ChatGPT “thinking,” then switching chats, is an
**optional later improvement**. It is not required for the feature. It is also
unsafe until we know whether Classic ChatGPT **keeps generating after you leave
the chat**. If it does not, slicing capture would abort replies. That probe is
Stage G-adjacent, not Stage C.

---

## Operating rules

1. Leave `/Users/abhisht/Documents/agent-gpt-codex-loop` (`main`) alone.
2. Do not start this worktree’s dashboard/handoff while the live loop is using
   ChatGPT Desktop.
3. Create this worktree’s own `.venv` before running tests (it is not copied
   from `main`).
4. Characterization tests before each behavior change.
5. Default `max_active_sessions = 1` until Stage E’s explicit additional-session
   start. Accidental double-submit of today’s Start button still hits
   `active_run_exists`.
6. First ship target is **two** sessions.
7. Never hold `LocalController._lock` across Codex or ChatGPT UI.
8. No live ChatGPT automation unless the stage says so and you approve it.
9. Do not invoke Codex from inside Codex (`AGENTS.md`).

Commands in this document use literal ASCII flags such as `--help` and
`--confirm-run`.

---

## Stage map

```text
A  Characterize current locks, lease-denied = hard fail, unused handoff queue
B  SQLite busy timeout (WAL only if tests still get SQLITE_BUSY)
C  ChatGPT contention is wait, not failure; extract does not take the lease
D  Session registry with max_active_sessions = 1 (one worker per run_id)
E  Second session: conversation uniqueness, same repo allowed, queue + wait
F  Dashboard + HTTP for two sessions
G  Live proof (production loop stopped)
H  Merge decision (later; default still one session)
X  Optional later: capture slices / switch-during-think, only after a probe
```

Do not start F before E. Do not do X before G’s probe. Do not skip C before E:
without C, the second session dies the first time it needs ChatGPT.

---

## Stage A — Characterize (no product change)

### Goal

Name the contracts we must keep, and the contracts we will deliberately change
in C.

### Work

Extend or add focused tests for **[C]**:

- `start_run` → `active_run_exists` when a non-terminal run is current.
- Terminal current run can be replaced; racing starts produce one winner.
- `action_already_running` blocks overlapping approve / tick / retry.
- UI lease: concurrent acquire has one winner; no time-based expiry.
- Destination bind is per-run, not unique across runs.
- Send path holds one lease through capture and extract.
- Extract **service** does not call ChatGPT; the **planner action** still
  enters the leased handoff transaction.
- Capture timeout argument is discarded (`None`).
- **Lease denied is a hard fail:** acquire failure → `blocked=True` →
  automatic progress stops and waits for manual retry.
  (`_chatgpt_lease_denied_result` + `_automatic_progress_loop`).
- Unused queue: `enqueue_chatgpt_handoff`, `claim_next_chatgpt_handoff`,
  `complete_chatgpt_handoff`, `block_chatgpt_handoff`. Record: FIFO by
  sequence; one active entry per run; claim is the oldest pending head;
  production does not call this API.

### Acceptance

- Those tests pass.
- A one-line amendment here: queue is fit to wire in E, or queue needs change
  X before E.
- Zero production behavior change.

---

## Stage B — SQLite under concurrent writers

### Goal

Two session threads writing events should not immediately hit `SQLITE_BUSY`.

### Work

- Set a busy timeout on `ledger._connect()`.
- Two threads inserting events for different `run_id`s succeed.
- Enable WAL **only** if that test still fails after the timeout.

### Acceptance

Existing ledger tests pass. Journal mode documented in conclusions if it
changes.

---

## Stage C — ChatGPT wait is not a failure

This is the first behavior change. It is small on purpose.

### Goal

A session that cannot have ChatGPT **right now** keeps living. Extract does
not occupy the desktop.

### Required behavior **[T]**

1. Planner action `extract_next_prompt` does not acquire the UI lease and does
   not navigate or gate. Extraction stays ledger-only.
2. `chatgpt_ui_lease_already_held` is **not** `blocked` and **not**
   `ok=False` for the automatic progress loop. It is retryable wait: sleep /
   backoff, then the same planner action again.
3. `_automatic_progress_loop` must not call `_pause_for_action_failure` for
   that wait. Overnight single-session is unchanged because nothing else holds
   the lease. The new path is for a second session in E.
4. **Do not** unchain send → capture in this stage. Keep today’s
   navigate → gate → submit → unbounded capture under one lease. That is
   still the safe single-loop capture. Changing it is Stage X, after a live
   probe.

### Tests

- Extract-only step: no lease acquire.
- Fake second holder: session’s send/capture gets `already_held`, worker
  retries, then succeeds when the fake lease is released. Controller state
  is not `waiting_for_retry`.
- Existing send → capture → extract one-lease tests still pass.
- Single fake session still completes a full loop.

### Acceptance

Single-session overnight path unchanged except extract no longer takes the
lease (milliseconds today; still a real contract).

### Do not

- Do not add a second session.
- Do not bound capture yet.
- Do not build a session list.

---

## Stage D — Session registry, still one live session

### Goal

The controller can hold a map of sessions. Default policy still allows one.
This is the refactor that E will turn on, not a user-visible feature yet.

### Work

Replace the singleton fields with a map keyed by `run_id`:

- worker thread
- `action_running`
- `cancel_requested`
- pending approval
- controller state
- destination, repo, sandbox, navigation flag

Keep `active_run_id` as the **focused** run so `/api/runs/current*` stays
valid.

Persist the registry, not only one id. Restore-on-restart: in-flight workers
become blocked per session as today.

`max_active_sessions = 1`: `start_run` still returns `active_run_exists`.
All existing one-run tests must keep passing.

Internal cancel / approve / retry take `run_id`. `/current` passes the
focused id.

Collision helpers exist and are unit-tested, even if max=1 never calls them
on a second start:

- Reject a second live session with the same `(project_title, chat_title)`.
- Allow the same project, different chat.
- Allow the same resolved repo path, including workspace-write.
- Reject `,` in project or chat titles at start (identity parse needs exactly
  one comma). Chats that already cannot gate today get a clear start error.

Never hold `_lock` across Codex or ChatGPT.

### Acceptance

`tests/test_local_controller.py` and `tests/test_local_server.py` one-run
contracts still pass.

---

## Stage E — Second session

### Goal

In this worktree, two sessions run. Codex overlaps. ChatGPT is one lane.
Same repo is allowed. Same chat is not.

### Work

1. **Explicit additional start** (flag or dedicated route). Today’s Start
   button without that flag still `active_run_exists`. Decide the exact
   payload in this stage; lock it with a test that the old start body cannot
   start a second session.

2. **Live conversation uniqueness** among non-terminal sessions:
   `(project_title, chat_title)`. Same project allowed. Same repo allowed.

3. **Navigation:** if two (or more) sessions are live, every ChatGPT slice
   behaves as `allow_destination_navigation=True`, even if session one was
   started with the checkbox off. Two chats cannot both already be on screen.

4. **Handoff queue** (if Stage A said it is fit): before a ChatGPT slice,
   enqueue that `run_id`, claim when this run is the pending head, acquire
   lease, do today’s send-or-capture slice, complete (or block) the queue
   entry, release lease. If the head belongs to another run, **wait** (Stage
   C), do not fail. If A said the queue is unfit, retry lease acquire with
   backoff only — do not invent a second queue.

5. **Fairness:** do not busy-spin. Do not complete-and-immediately-reclaim
   in a tight loop that starves the sibling.

6. **Cancel session A** terminates only A’s Codex (`terminate_codex_run(A)`).
   B’s worker and Codex keep going.

7. **Two pending approvals** are two snapshots. Approving A does not apply
   to B.

### Tests (fakes, no Desktop)

- Two sessions, different chats, **same repo path**, both Codex fakes run
  concurrently.
- Two sessions, same project, different chats: allowed.
- Same `(project, chat)`: rejected.
- Old start body while A is live: `active_run_exists`.
- Additional start while A is in Codex: B’s initial Codex starts.
- A holds the lease (fake): B’s send waits, then proceeds; B is never
  `waiting_for_retry`.
- Cancel A: B’s Codex mock is not terminated.
- Extract on A does not take the lease (from C) and does not block B.
- Dual approval: A and B each have a pending snapshot; deciding A leaves B
  pending.

### Acceptance

Headless two-session loop with fakes. No live ChatGPT.

---

## Stage F — Dashboard and HTTP

### Goal

Start, watch, approve, and stop two sessions without curling JSON.

### Work

- `GET /api/runs` — live sessions (project, chat, repo, stage, whether this
  run owns the ChatGPT lease).
- Keep `/api/runs/current*` as the focused session.
- `/api/runs/<run_id>/...` for progress, cancel, retry; approval names
  `run_id`.
- Explicit “start additional session” control. Same form fields as today
  (project, chat, repo, sandbox, model). Not a second accidental submit.
- UI: list of sessions; detail for the focused one; per-session stop;
  per-session approval.
- Remote phone uses these APIs. No second protocol.
- Update `tests/test_web_static.py` URL contracts.

### Acceptance

- Contract tests for list / additional start / per-id cancel.
- With the additional-session control unused, the old form still starts
  exactly one run.
- Browser pass of start A, start additional B, switch focus, stop A, B still
  listed — against the fake/headless path if we can; otherwise API-level
  plus a short local UI pass **without** ChatGPT handoff. Live ChatGPT is
  Stage G.

---

## Stage G — Live proof (operator-approved)

### Goal

Prove the feature we will actually use.

### Preconditions

- Production loop from `agent-gpt-codex-loop` is **stopped**.
- This worktree dashboard only; its own `.venv`.
- Two distinct ChatGPT conversations. Same project allowed. Same repo
  allowed (include at least one same-repo run in the script).
- Titles contain no commas.
- You have approved live desktop automation.

### Script

1. Session A: one full round (Codex → paste into A’s chat → capture → next
   Codex).
2. Start additional session B on a different chat. While A is in Codex, B
   must be able to run Codex. While A is waiting on ChatGPT (lease held), B
   must keep Codex moving and must **wait**, not fail, if B also needs to
   paste.
3. Every paste: window identity is that session’s `"<chat>, <project>"`.
4. A’s marker never appears as a submit in B’s chat, and the reverse.
5. Stop A; B continues and completes a round.
6. Repeat with both sessions on the **same** repo and different chats.
7. Only then consider a longer dual loop.

### Probe for Stage X (record, do not depend on it)

While ChatGPT is generating in chat A, switch to chat B. Does A’s reply
finish in the background? Write the answer into this file. If **no**, do
not do Stage X.

### Fail closed

Wrong-chat paste, a sibling session in `waiting_for_retry` because the lease
was busy, or stop A killing B’s Codex — those are C–E bugs. Do not go to H.

**[U]** If two Codex CLIs auth-lock or starve, record it. Then we may
serialize Codex too. Do not guess that in E.

---

## Stage H — Merge decision (later)

Not a coding stage. Only after Stage G:

- Merge is an explicit request.
- Keep default one session on merge so the daily driver stays one-loop until
  we turn additional-session on in the live folder.
- Do not switch the live checkout to this branch as the daily driver until
  we say so.

---

## Stage X — Optional: switch-during-think (not MVP)

Only if Stage G’s probe shows generation **continues** when the chat is not
focused.

Then, and only then:

- Bound capture polls; release the lease between slices.
- Re-navigate and re-gate every capture slice.
- Re-enqueue at the **tail** so the sibling can paste while we wait.
- Unchain send from unbounded capture.

If the probe is no, **never** do this. Serial ChatGPT (including think time)
plus overlapping Codex is the product.

---

## Suggested first coding slice (when we say go)

Stage A only: tests, no product change. Create `.venv` in this folder first.

---

## Test strategy

| Kind | Use |
|---|---|
| Existing controller / server / lease / supervision tests | Compatibility ratchet every stage |
| Fake ChatGPT + fake Codex | Two-session wait/serialize without Desktop |
| Focused tests | Only the files that encode the slice |
| Live Desktop | Stage G (and the Stage X probe), explicit approval |

---

## Files we expect to touch

| Stage | Likely files |
|---|---|
| A | `tests/test_local_controller.py`, `tests/test_supervision_services.py`, `tests/test_chatgpt_ui_lease.py`, new queue tests |
| B | `agent/ledger.py`, ledger tests |
| C | `agent/supervision_services.py`, `agent/local_controller.py` (progress loop wait vs fail), supervision + controller tests |
| D | `agent/local_controller.py`, snapshot in `agent/ledger.py`, bind/start title checks in `agent/run_services.py`, controller tests |
| E | `agent/local_controller.py`, `agent/supervision_services.py`, queue wiring, `agent/local_server.py` start payload, two-session tests |
| F | `agent/local_server.py`, `agent/web_static/app.js`, `index.html`, `tests/test_web_static.py`, `tests/test_local_server.py` |

Avoid drive-by splits of `agent/cli.py` and
`agent/chatgpt_navigation_diagnostic.py`. CLI multi-session is out of scope;
the dashboard is the product.

---

## Closed decisions (do not reopen in code without asking)

1. Same repo is allowed. Do not add a start-time repo lock.
2. Same ChatGPT project is allowed.
3. Same ChatGPT conversation (`project_title` + `chat_title`) is not.
4. Capture slicing is Stage X, not Stage C.
5. Additional session is explicit; default max is 1.
6. When two sessions are live, ChatGPT handoff always navigates.

## Left to decide inside the named stage

1. Stage E: exact additional-session API field vs route.
2. Stage C: wait backoff (seconds). Must not spin.
3. Stage G: whether Codex must be serialized after seeing real quota
   behavior.
