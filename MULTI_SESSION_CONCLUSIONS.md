# Multi-session CRAX: conclusions

This document is the design conclusion from a read-only audit of
`/Users/abhisht/Documents/crax-multi-session` on branch `feature/multi-session`.
It says how we will build concurrent work sessions without breaking the
single-loop product that already runs from
`/Users/abhisht/Documents/agent-gpt-codex-loop` on `main`.

It is not an implementation. Target behavior is labeled **[T]** so it cannot be
mistaken for code that exists today.

Classification used throughout:

- **[C]** Confirmed in authoritative `agent/` (not `build/lib/agent`)
- **[I]** Inferred from that code
- **[T]** Target architecture; not implemented
- **[U]** Unknown without a live prototype (ChatGPT Desktop, Codex quota)

---

## 1. Isolation (already in place)

| | Live loop | This work |
|---|---|---|
| Folder | `/Users/abhisht/Documents/agent-gpt-codex-loop` | `/Users/abhisht/Documents/crax-multi-session` |
| Branch | `main` | `feature/multi-session` |
| Role | Keep using | Plan, then implement, then prove |

**[C]** The ledger path is `data/agent_ledger.db` relative to process cwd. Two
folders means two databases if each dashboard is started from its own folder.

Disk isolation does **not** isolate Classic ChatGPT Desktop, the clipboard, or
Accessibility. Do not start an experimental handoff loop while the live loop is
using ChatGPT.

---

## 2. What we are building

**[T]** CRAX should support more than one supervised work session at once on
the same Mac. Example:

- Session A: ChatGPT project `craxii`, chat `Dev Internal App`, repo A
- Session B: ChatGPT project `PTG Assistant`, chat `Moderaiton Backend`, repo B

Each session keeps its own ChatGPT conversation, Codex process, ledger run,
approvals, and stop control. Sessions may share a ChatGPT project and may
share a repository. They may not share a ChatGPT chat. Starting B must not
abort A.

**[T]** This is not two ChatGPT Desktops. Classic ChatGPT is one frontmost
window, one visible conversation, one clipboard, and one Accessibility tree.
The product meaning is:

> Independent Codex loops that time-share one ChatGPT Desktop handoff lane.

Codex work may overlap. Paste, navigate, destination proof, and capture must
be exclusive.

---

## 3. How the current loop actually works

End-to-end, one dashboard process owns the loop.

1. Operator starts a run: repo, kickoff instruction, sandbox, exact ChatGPT
   project title, exact chat title, optional autonomous navigation.
2. **[C]** `LocalController.start_run` creates one ledger run, binds that
   destination, stores one `active_run_id`, and starts one worker thread.
3. Codex runs as a subprocess in that repo (`codex exec -C <repo> -s <sandbox>`).
4. After Codex finishes, governance records evidence and a run status.
5. The supervision planner (`detect_next_supervise_action`) picks one of:
   `ask_send_to_gpt`, `capture_gpt_response`, `extract_next_prompt`,
   `ask_run_prompt`, or `stop`.
6. ChatGPT handoff acquires the **process-global UI lease**, optionally
   navigates, destination-gates the open conversation, pastes Codex output,
   submits, then **captures ChatGPT’s reply in the same leased transaction**,
   then extracts the next `BEGIN_NEXT_CODEX_PROMPT` / `END_NEXT_CODEX_PROMPT`
   block.
7. The extracted prompt becomes the next Codex job. Repeat.

ChatGPT is the planner/reviewer. Codex is the implementer. CRAX is transport,
policy, and evidence.

---

## 4. What already scales, and what does not

### Already multi-run at the data layer **[C]**

- Table `runs` stores many UUID rows.
- Events are per `run_id`.
- Destination binding and execution profile are per-run events.
- Prompt artifacts live under `data/runs/<run_id>/`.
- Codex process table `_ACTIVE_CODEX_PROCESSES` is `run_id -> Popen`.
- `terminate_codex_run(run_id)` kills only that process.
- `get_run_progress(run_id)` already exists on the controller.

The ledger can remember many loops. The orchestrator will not run two of them.

### Single-session locks **[C]**

| Lock | Where | Effect |
|---|---|---|
| One `active_run_id` | `LocalControllerSession` | Second start returns `active_run_exists` unless the current run is terminal (`completed`, `failed`, `needs_review`, `rejected`) and no action is running |
| One `current_worker` / `action_running` | `LocalController` | Approve, tick, retry, and start cannot overlap |
| One `pending_approval` | session | One approval snapshot |
| One `cancel_requested` | controller Event | Cancel is not addressed to a run id |
| Singleton snapshot | `local_controller_snapshot` (`singleton_id = 1`) | Restart restores at most one current run |
| HTTP `/api/runs/current*` | `local_server.py` | No run-id routes; no `list_runs` |
| Dashboard | `web_static/` | One “Current run” panel, one progress stream |
| ChatGPT UI lease | `ledger.acquire_chatgpt_ui_lease` | One holder process-wide; second acquire is `chatgpt_ui_lease_already_held` |
| Handoff transaction | `_run_chatgpt_handoff_transaction` | Lease wraps navigate, gate, submit, **unbounded capture**, and extract |
| Clipboard | `pbcopy` | Machine-global |
| Frontmost paste | `mac_paste.py` | Classic ChatGPT (`com.openai.chat`) must be frontmost |

`start_run` does not clear `active_run_id` when a run completes. The id stays
until a replaceable new start, or until restore-on-restart drops a terminal run.

---

## 5. The ChatGPT critical section (the real bottleneck)

**[C]** Submit, capture, and extract all enter `_run_chatgpt_handoff_transaction`,
which acquires the UI lease first and releases it only in `finally`.

On a successful send, the same transaction then captures and extracts. Capture
is `while True` with `DEFAULT_CAPTURE_TIMEOUT_SECONDS = None` (the timeout
argument is discarded). It polls Accessibility until:

1. the unique submission marker is visible in the focused ChatGPT window,
2. a complete sentinel block exists after that marker,
3. the text is stable for two successful polls and `stable_seconds` (default 2s).

Until ChatGPT finishes thinking, that session owns the Mac UI. A second session
cannot paste.

**[C]** `extract_next_codex_prompt_service` is ledger-only: it parses captured
text. It still rides the leased gate path today, so extraction occupies the
desktop even though it does not need ChatGPT.

**[C]** The destination gate is a pure verifier. Authoritative identity is the
window title `"<chat>, <project>"` plus a single composer. Navigation, when
enabled, runs *before* the gate. The gate never navigates. After a successful
submit, capture does **not** re-check destination while it waits.

**[C]** Destination binding is per-run, not unique among live sessions. Two runs
may bind the same project and chat. Binding code strips titles and rejects
empties; it does **not** reject commas, even though the identity parser splits
on a single comma and the snapshot comment claims commas are “enforced at
binding time.”

**[C]** Autonomous navigation defaults to off (`allow_destination_navigation`
checkbox). Single-loop often assumes ChatGPT is already on the bound chat.
Two sessions cannot both be on-screen. Multi-session therefore depends on
project-then-chat switching, which is still the brittle seam.

**[C]** Paste verification is also an unbounded `while True`. Submission
verification is bounded (max 40 polls). The lease does not time-expire.
Stale release is a manual operator action with PID and run-status guards.

---

## 6. Unused handoff queue (do not reinvent)

**[C]** `ledger.py` already defines a serialized ChatGPT handoff queue:
`enqueue_chatgpt_handoff`, `claim_next_chatgpt_handoff`,
`complete_chatgpt_handoff`, `block_chatgpt_handoff`. Reconstruction is global.
A run may not have two active queue entries.

**[C]** Nothing in `supervision_services.py`, `local_controller.py`, `cli.py`,
or tests calls those functions. Production handoff uses the UI lease directly.

**[T]** We will characterize this queue with tests, then wire it, rather than
build a second queue. If characterization finds it unfit, we will say so and
change it in a dedicated slice. We will not leave two competing schedulers.

---

## 7. Codex, git, and SQLite

**[C]** Two JSONL Codex processes with different `run_id`s can exist in the
process table. The controller never starts the second one.

**[C]** Git snapshots are repo-wide before/after windows, not process-isolated.
Two workspace-write Codex jobs in the same working tree can interleave dirty
files in each run’s before/after window. **[T]** That is allowed as a product
choice (same repo, different chats). Evidence attribution will be messier;
we do not block the start. We still block the same ChatGPT chat.

**[C]** SQLite uses default rollback journal, no WAL, no busy timeout, and
`BEGIN IMMEDIATE` on atomic paths. Connections are per-call. A handful of
writers will serialize or hit `SQLITE_BUSY`. Fine for one loop; fragile for N
workers.

**[U]** Whether two Codex CLIs on one Mac share auth or rate limits is unproven.

---

## 8. How we will do it

### 8.1 Compatibility is a default, not a hope

**[T]** After merge, one active session must behave as today:

- Second start still returns `active_run_exists` unless the operator opts into
  additional sessions.
- `/api/runs/current*` and the current dashboard keep working.
- Navigation stays opt-in for a single session.
- Destination gate, UI lease, sentinel contract, and fail-closed paste stay.

The additive trigger is an explicit additional session (UI/API), not a silent
change to `start_run`. Default `max_active_sessions = 1`.

First usable target is **two** concurrent sessions. The internals should allow
N, but we will not ship an unbounded session list before two sessions are
proven overnight.

### 8.2 One ChatGPT lane, many Codex workers

**[T]** Keep the process-global UI lease. It is the correct desktop mutex.

**[T]** MVP does **not** split capture off the lease. Two session workers plus
“lease already held is a wait, not a failure” is enough: B’s Codex runs while
A holds ChatGPT, and B waits to paste instead of going to manual retry.
Extract must not take the lease.

**[T]** Releasing the lease during ChatGPT think, then switching chats, is
optional (implementation plan Stage X). Do it only after a live probe shows
Classic ChatGPT keeps generating when the conversation is not focused. If it
does not, slicing capture would abort replies.

### 8.3 Session registry in the controller

**[T]** Replace “one current run” with a registry of live sessions, each with
its own worker, approval snapshot, cancel flag, and read model.

Keep `active_run_id` as the **focused** run so old APIs remain valid. Persist
the full registry, not a singleton current id only.

Start of session B must reject:

- the same live ChatGPT conversation: duplicate `(project_title, chat_title)`
- titles containing `,` (make the snapshot comment true)

The same ChatGPT project is allowed. The same repository is allowed, including
two workspace-write sessions on one path. The operator is choosing to let two
loops edit the same tree. Do not reject that start.

Stop/approve/retry must take a `run_id`. Stopping A must not terminate B’s
Codex process.

### 8.4 Scheduler, not busy-spin

**[T]** When a session needs ChatGPT, it enqueues on the existing handoff
queue (after characterization). One claim owner acquires the lease, does one
short UI slice, completes or re-queues if capture is not ready. Other sessions
keep running Codex.

Lease denial must not become a tight retry loop.

### 8.5 Dashboard last, live prove after that

**[T]** Do not start with a session-list UI.

Order: characterize → ChatGPT wait is not a failure → registry with default
max 1 → second session + queue → dashboard → live proof in this worktree
with the production loop stopped. Capture slicing is optional after that
probe.

### 8.6 What we will not do

- Do not edit the live `main` checkout to “just try” multi-session.
- Do not drop the destination gate or paste into ChatGPT without exact proof.
- Do not assume two ChatGPT windows **[U]**.
- Do not attach two sessions to the same ChatGPT chat.
- Do not treat unused handoff-queue functions as production until tested.
- Do not split `cli.py` or `chatgpt_navigation_diagnostic.py` as part of this
  feature. Navigation is used through the existing production actor.
- Do not run experimental paste/capture while the live loop holds ChatGPT.

---

## 9. Edge cases we are solving for

| Edge | Rule |
|---|---|
| Unbounded capture holds the desktop | MVP: sibling waits, does not fail. Slicing that wait is optional Stage X after a generation-unfocus probe. |
| Wrong-chat paste/capture | Gate before every submit and every capture slice; fail-closed |
| Capture wait with no mid-wait re-gate | Do not wait minutes on a conversation that is no longer proven |
| Same repo, two writers | Allowed. Two chats may drive one codebase. Git evidence may interleave; that is accepted. |
| Same ChatGPT project | Allowed. A project can host many looping chats. |
| Same chat bound twice | Reject. The conversation is the session. |
| Comma in titles | Reject at bind time; identity parse requires exactly one comma |
| Operator using ChatGPT / the pointer | Keep send retry budget (5); park that session on repeated gate failure |
| Accidental double-start | Default max 1; additional session is explicit |
| Stop the wrong Codex | Terminate by `run_id` only |
| Clipboard mix | Clipboard only while the lease is held; copy-paste-send is one slice |
| Extract occupying ChatGPT | Extract is ledger-only after capture exists |
| Stale lease with two sessions | Stale-release must name owning `run_id`; do not release another session’s lease |
| SQLite busy under N writers | Busy timeout, then WAL, in a dedicated slice |
| Navigation failure | Multi-session cannot pretend ChatGPT is already on the right chat |
| Remote phone dashboard | Same session list as local UI; no second protocol |
| Codex quota **[U]** | Prove two concurrent Codex jobs in different repos before overnight dual loops |

---

## 10. Success

The feature is actually usable when:

1. The live `main` loop is still untouched and still the daily driver until we
   choose to merge.
2. In this worktree, two sessions with different ChatGPT chats can run Codex
   at the same time (same project or not, same repo or not), take turns at
   ChatGPT, and continue their own sentinel loops.
3. With max sessions = 1, automated tests show the old start/reject/cancel/
   approve/current-run contracts still hold.
4. Destination mistakes fail closed rather than pasting into the other chat.
5. Stopping one session leaves the other running.

Until (2) is proven live, this stays a branch. It is not a replacement for the
loop already in use.
