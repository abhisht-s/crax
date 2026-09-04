# Read-only architectural investigation

Classification used throughout:

- **[C] Confirmed by code or observed repository state**
- **[I] Inferred from code**
- **[D] Documented but not confirmed**
- **[U] Currently unknown; requires a prototype or live-system evidence**

No repository files were modified, created, deleted, renamed, formatted, or generated during the investigation. I did not run tests because even narrow tests can create caches, temporary artifacts, databases, or build output, which would conflict with the strict read-only requirement.

## 1. Repository state

**[C] At the start of the investigation:**

- Branch: `main`
- HEAD: `3f1003f8b525214541103c8f27e499a9a19340dd`
- Upstream: `origin/main`
- Worktree: dirty
- Only changed tracked file: `tests/test_web_static.py`
- Existing change: removal of an assertion requiring `id="event-timeline"`.
- I treated this as pre-existing user work and did not touch it.

**[C] Significant local/generated state includes:**

- `.venv/`
- `build/`
- `build/lib/agent/`
- Python `__pycache__` directories
- package egg-info
- `.DS_Store`
- `.claude/settings.local.json`
- `data/agent_ledger.db`
- `data/runs/`

**[C] The authoritative source is `agent/`.** `build/lib/agent` is tracked build output and differs from `agent/`; it must not be treated as the source of current behavior. This matches the supplied repository instructions and `ARCHITECTURE.md`.

## 2. What the product actually is

**[C] The current product is a Python modular monolith with four major external surfaces:**

1. A large command-line interface.
2. A localhost web dashboard.
3. A Codex CLI subprocess integration.
4. macOS automation targeting the ChatGPT Classic desktop app.

**[C] The dashboard is not merely a remote control for a separate agent daemon.** Its Python server process owns the `LocalController`, starts background run threads, calls Codex, advances the supervision planner, manipulates ChatGPT, and persists the run. Closing that process removes the active in-memory worker and orchestration layer.

**[C] The supplied workflow description is broadly correct, with important qualifications:**

- The dashboard is started explicitly with `python -m agent.local_server`; starting a generic CLI “agent” is not necessarily a prerequisite.
- The user supplies an exact ChatGPT Project title and chat title, but the system does not bind to durable ChatGPT IDs.
- Returning to a conversation is title- and accessibility-evidence-based. It is not URL- or conversation-ID-based.
- Autonomous navigation is optional. Otherwise ChatGPT must already be showing the target conversation.
- ChatGPT must return an exact sentinel-delimited prompt.
- Waiting for ChatGPT has no effective deadline and can continue indefinitely.
- Codex completion is based on subprocess termination plus an output artifact, not on interpreting terminal prose.
- A run marked `completed` can still be continuation-eligible; that word does not consistently mean “the complete ChatGPT↔Codex loop is finished.”
- The dashboard cannot interactively satisfy Codex approval requests. It either relies on Codex’s own default behavior or bypasses approval and sandboxing in full-access mode.

## 3. Repository inventory

### Languages and packaging

**[C] Primary implementation:**

- Python 3.11+
- Static HTML, CSS, and vanilla JavaScript
- SQLite
- JSON and Codex JSONL event streams
- Shell/macOS system executables invoked as argument arrays
- Markdown documentation
- SVG/PWA assets

**[C] Packaging is defined in `pyproject.toml`, with a custom `build_backend.py`.**

Installed console commands:

```text
agent-loop       -> agent.cli:main
agent-loop-local -> agent.local_server:main
```

There are no declared third-party Python runtime dependencies. The application instead relies heavily on the operating system, standard library, external CLI tools, and native frameworks loaded through `ctypes`.

### Runtime dependencies

**[C] Required or assumed runtime components include:**

- macOS
- Python 3.11 or newer
- Codex CLI discoverable on `PATH`
- Git
- ChatGPT Classic desktop app, bundle identifier `com.openai.chat`
- A signed-in ChatGPT session
- `osascript`
- `pbcopy`
- `pgrep`
- `lsappinfo`
- CoreFoundation
- ApplicationServices/Accessibility
- CoreGraphics
- Foundation
- AppKit
- Objective-C runtime libraries

**[C] Optional remote supervision depends on an external Tailscale Serve-style HTTPS endpoint forwarding to the loopback server.**

**[C] There is no evidence of OCR or screenshot-based conversation reading.** Screen pixels are not captured by the current production path. CoreGraphics is used for window metadata and synthetic input, while conversation contents are read through Accessibility.

### Documentation

Relevant architectural/product documents include:

- `README.md`
- `ARCHITECTURE.md`
- `STATE_MACHINE.md`
- `POLICY_MODEL.md`
- `SECURITY_AND_PRIVACY.md`
- `DIAGNOSTICS_VS_PRODUCTION.md`
- `instructions_for_dsktp_app.md`
- `kick_off_prompt_gpt.md`

The SeaView and Firecrawl material at the root is research/support content rather than part of the agent runtime.

## 4. Commands and entry points

**[C] `agent/cli_parser.py` defines these CLI commands:**

Run and ledger operations:

```text
init
start
show
approve
reject
complete-review
can-continue
```

Codex operations:

```text
codex-check
codex-run
run-extracted-codex-prompt
run-shell
```

ChatGPT feedback and prompt operations:

```text
gpt-feedback
paste-feedback
paste-feedback-to-chatgpt
submit-feedback-to-chatgpt
capture-gpt-response-from-chatgpt-ax
extract-next-codex-prompt
submit-feedback
```

ChatGPT activation, inspection, navigation, and diagnostics:

```text
activate-chatgpt
inspect-chatgpt-ui
inspect-chatgpt-navigation-ui
verify-chatgpt-sidebar-destination
open-chatgpt-sidebar-destination
inspect-chatgpt-sidebar-destination
inspect-chatgpt-project-visible-chats
inspect-chatgpt-project-chat-row-ax
diagnose-chatgpt-project-chat-rows
open-chatgpt-project-chat
calibrate-chatgpt-sidebar-coordinate-mapping
verify-chatgpt-sidebar-frame-click
verify-synthetic-click-delivery
verify-current-cursor-click
test-chatgpt-target-paste
release-stale-chatgpt-ui-lease
```

Loop orchestration:

```text
supervise
```

**[C] `agent/cli.py:2853` is the main dispatch point.** It remains a 3,875-line compatibility and orchestration facade even after parser and service extraction. It handles command dispatch, validation, output formatting, manual workflows, and some safety logic that overlaps with service modules.

**[C] `run-shell` executes an explicit argument vector without `shell=True`.** It is still intentionally capable of invoking arbitrary programs supplied by the CLI user, but it does not interpolate through a shell.

## 5. Component and responsibility map

| Component | Current responsibility |
|---|---|
| `agent/local_server.py` | Loopback HTTP server, token and remote authentication, REST/SSE routes, static web assets, process lifecycle |
| `agent/local_controller.py` | Run creation, worker threads, automatic progression, approvals, retry, cancellation, restoration |
| `agent/web/app.js` | Browser state, forms, API calls, polling/SSE, progress rendering, controls |
| `agent/cli.py` | Broad CLI facade and legacy/manual workflows |
| `agent/run_services.py` | Run lifecycle services, run creation and execution-related boundaries |
| `agent/supervision_services.py` | Planner action execution, ChatGPT handoff transaction, navigation/gating/capture/extraction |
| `agent/supervise.py` | Supervision planner and action selection |
| `agent/execution_profile.py` | Immutable execution settings and allowed option sets |
| `agent/codex_terminal.py` | Codex command construction, subprocess, JSONL parsing, final artifact, termination |
| `agent/codex_services.py` | Profile resolution and higher-level Codex execution |
| `agent/git_snapshot.py` | Git before/after evidence and invocation dirty-state capture |
| `agent/ledger.py` | SQLite schema, event storage, state reconstruction, lease and remote state |
| `agent/gpt_feedback.py` | Construct outbound Codex completion report and correlation marker |
| `agent/chatgpt_services.py` | App activation, clipboard/paste/submit operations, submission verification |
| `agent/chatgpt_destination.py` | Run destination binding and identity evidence |
| `agent/chatgpt_navigation_diagnostic.py` | AX discovery, project/chat search, scrolling, click actuation, coordinate mapping, manual diagnostics |
| `agent/chatgpt_ax_capture.py` | Accessibility-tree transcript capture and streaming stabilization |
| `agent/prompt_protocol.py` | Sentinel parsing and next-prompt extraction |
| `agent/chatgpt_ui_lease.py` | Cross-process ownership of ChatGPT UI mutations |

## 6. Actual end-to-end runtime sequence

### 6.1 Server and dashboard startup

1. **[C]** The user runs:

   ```bash
   python -m agent.local_server --port 0
   ```

2. **[C]** `local_server.main()` constructs `LocalControllerServer`, binds `127.0.0.1`, starts the HTTP server on a daemon thread, prints a bootstrap URL, and remains in a join loop until interrupted.

3. **[C]** It does not automatically open a browser.

4. **[C]** The URL contains a random controller token in the fragment. Frontend JavaScript reads it, removes it from the visible URL using `history.replaceState`, and holds it in memory.

5. **[C]** The dashboard fetches repository options, execution-profile options, the default greeting, current run state, and lease state.

### 6.2 Run creation

6. **[C]** `POST /api/runs/start` validates:

   - Existing repository directory
   - Initial task
   - Project and chat titles
   - Sandbox
   - Model
   - Navigation preference
   - Remote restrictions where applicable

7. **[C]** Local starts require an existing directory but do not require it to be a Git repository. Remote starts require the resolved path to remain beneath a configured root and contain `.git`.

8. **[C]** `LocalController.start_run()` creates:

   - A UUID run ID
   - Minimal run row
   - Immutable execution profile
   - Exact project/chat title binding
   - Controller event/state
   - Background daemon `_initial_worker`

9. **[C]** Only one active run is allowed inside that controller instance. There is no repository-wide or machine-wide single-controller process lock.

### 6.3 Initial Codex invocation

10. **[C]** The worker captures pre-run Git evidence and current dirty/untracked files.

11. **[C]** It executes Codex using an argument vector, not a shell string.

12. **[C]** Codex receives the user’s initial prompt as a positional argument.

13. **[C]** While Codex runs, JSONL stdout events are normalized and stored as progress events. Stderr is buffered in a temporary file and read only after process exit.

14. **[C]** When Codex exits, the engine requires a nonempty, readable UTF-8 final-message artifact written via `--output-last-message`.

15. **[C]** It then captures post-run Git state, calculates attributable changes, runs file/risk/workspace classifications, and stores governance evidence.

16. **[C]** The worker enters `_automatic_progress_loop()`, repeatedly asking the planner for the next action until it reaches a human gate, retry gate, block, failure, or terminal condition.

### 6.4 Returning Codex output to ChatGPT

17. **[C]** The planner decides whether the Codex result is eligible to send automatically.

18. **[C]** The ChatGPT UI transaction acquires a global SQLite-backed UI lease.

19. **[C]** If autonomous destination navigation is enabled, the navigation module searches for the exact project and chat title and attempts to open them.

20. **[C]** A destination gate independently re-reads the focused ChatGPT window and requires stable evidence that the expected project/chat is active.

21. **[C]** The feedback builder creates an `AGENT_SUBMISSION` envelope containing:

   - Run ID
   - New nonce
   - SHA-256 payload hash
   - Codex completion report
   - Status and change summary
   - Codex final assistant message

22. **[C]** The entire outbound message is copied to the macOS clipboard.

23. **[C]** ChatGPT Classic is activated by bundle ID, and System Events verifies it is frontmost.

24. **[C]** The automation finds one focused composer, selects and deletes its existing contents, pastes the clipboard, and submits using Accessibility press where possible or Enter as a fallback.

25. **[C]** Submission is considered verified when the marker leaves the composer and appears in exactly one transcript candidate.

### 6.5 Waiting for the next prompt

26. **[C]** The same handoff transaction immediately starts Accessibility capture; it does not perform another complete destination gate between submission and capture.

27. **[C]** The capture code identifies the outbound submission by its marker and observes accessibility groups that follow it.

28. **[C]** It waits for a complete response containing:

   ```text
   BEGIN_NEXT_CODEX_PROMPT
   ...
   END_NEXT_CODEX_PROMPT
   ```

29. **[C]** The rendered response must be stable for at least two observations. Polling occurs approximately once per second.

30. **[C]** The extraction service requires exactly one ordered, nonempty sentinel pair. Surrounding prose is ignored.

31. **[C]** The resulting prompt is stored as an event and as `data/runs/<run-id>/next_codex_prompt.md`.

32. **[C]** The planner evaluates whether the prompt may run automatically. Current prompt safety classification effectively permits every nonempty prompt; the meaningful controls are the sandbox/profile and higher-level governance gates.

33. **[C]** If allowed, the next Codex invocation begins and the loop repeats.

### 6.6 Completion, shutdown, and interruption

**[C]** A planner stop can result from already-consumed prompt evidence, non-continuable governance, explicit rejection, failure, or an otherwise terminal plan. There is no explicit, robust ChatGPT protocol for “the task is complete; stop waiting.”

**[C]** HTTP server shutdown stops the server but does not explicitly join controller workers or guarantee cleanup of active child processes.

**[C]** Emergency cancellation:

- Sets a cancellation flag.
- Marks the controller/run blocked or failed.
- Terminates a registered Codex process, escalating to kill after roughly two seconds.
- Is checked between supervision steps.

**[C]** Cancellation does not currently propagate into an Accessibility capture wait. A run waiting indefinitely for ChatGPT can therefore remain occupied until the capture operation returns.

**[C]** After restart, an in-progress `starting` or `running` controller snapshot is restored as blocked, not automatically resumed. Pending approval can be reconstructed if its event and hashes remain valid.

## 7. Run, state, and data model

### Run representation

**[C]** The `runs` table is deliberately small:

- `id`
- timestamps
- `status`
- `user_instruction`
- `final_summary`
- `error`

Most meaningful state is stored as append-only event metadata in `events`.

**[C]** A run combines several distinct state dimensions:

- Run status
- Controller state
- Planner’s next action
- Governance decision
- Handoff phase
- Pending approval snapshot
- Execution profile
- ChatGPT destination binding
- Codex subprocess/progress state

**[I]** This distributed representation is a major source of cognitive and recovery complexity. No single state machine owns the authoritative full lifecycle.

### Controller states

**[C]** `local_controller.py` defines:

```text
idle
starting_initial_codex
running_routine_action
waiting_for_approval
waiting_for_retry
blocked
failed
completed
```

**[C]** Planner actions include:

```text
STOP
ASK_SEND_TO_GPT
CAPTURE
EXTRACT
ASK_RUN
```

**[C]** `completed` is overloaded. A Codex step can be completed while a next ChatGPT prompt is still expected and the loop remains active.

### Execution settings and what reaches Codex

| Setting | Stored/displayed | Actually passed to Codex |
|---|---|---|
| Repository | Yes | **[C]** `-C <repository>` |
| Sandbox: read-only/workspace-write | Yes | **[C]** `-s <sandbox>` |
| Sandbox: danger-full-access | Yes | **[C]** `--dangerously-bypass-approvals-and-sandbox` |
| Model | Yes | **[C]** `-m <model>` when nondefault |
| Reasoning effort | Represented as `codex_default` | **[C]** Not passed |
| Approval mode | Stored as default or never | **[C]** No normal approval flag; full access bypasses approvals |
| Project/chat titles | Yes | Used only by ChatGPT automation |
| Autonomous navigation | Yes | Used only by the controller/navigation layer |
| Environment variables | Not modeled as run configuration | **[C]** Child inherits the Python process environment |

Allowed models are hardcoded:

```text
codex_default
gpt-5
gpt-5-codex
```

Reasoning effort is currently locked to `codex_default`.

**[U]** Whether these hardcoded model names remain valid for the user’s installed Codex CLI cannot be guaranteed without querying that version.

### Local persistence

**[C]** The default database path is relative:

```python
Path("data/agent_ledger.db")
```

The working directory therefore changes where the application reads and writes its state.

**[C]** At investigation time, the local database was approximately 2.4 GiB:

- 126 runs
- 20,085 events
- 12,144 progress events
- 42 run artifact directories

Observed statuses:

```text
67 completed
26 created
20 needs_review
5 waiting approval
3 approved
3 rejected
2 failed
```

**[C]** The dominant storage cost is full pre- and post-invocation repository evidence. `capture_invocation_git_state()` records up to 1,000,000 bytes of UTF-8 content for every dirty or untracked file and stores that evidence before and after each invocation.

Observed aggregate event metadata included approximately:

- 1.197 GB of `invocation_git_state_before`
- 1.201 GB of `invocation_git_state_after`
- Individual events approaching 13.9 MB
- `codex_exec_finished` data totaling well over 100 MB

**[C]** There is no retention policy, compaction, encryption, content-addressed deduplication, or artifact/blob separation.

**[I]** This is already an operational blocker for an always-running native application. Merely embedding or daemonizing the current ledger behavior would cause continuing storage and privacy growth.

**[C]** SQLite connections are opened frequently, without an evident WAL configuration, foreign-key enforcement, indexing strategy, or explicit busy timeout.

### Handoff queue

**[C]** Queue-related schema and protocol operations exist in `ledger.py`, including enqueue/claim/complete/block concepts.

**[C]** The current controller and supervision path does not use them.

**[I]** This is incomplete architectural groundwork, not a functioning multi-workstream queue.

## 8. Codex integration

### Command construction

**[C]** `run_codex_exec()` in `agent/codex_terminal.py:586` constructs approximately:

```text
codex exec
  --json
  -C <repository>
  -s <sandbox>
  -m <model>
  --output-last-message <temporary-artifact>
  <prompt>
```

For full access, `-s` is replaced by:

```text
--dangerously-bypass-approvals-and-sandbox
```

Arguments are passed directly to `subprocess.Popen`; no shell is involved.

### Lifecycle and completion

**[C]**

- Codex is located using `shutil.which("codex")`.
- Repository, sandbox, and model are validated.
- Progress mode adds `--json`.
- Stdout is consumed line by line as JSONL.
- Stderr is written to a temporary file.
- Active processes are held in an in-memory map keyed by run ID.
- There is no effective execution timeout.
- Completion requires process exit plus a valid final artifact.
- Exit code zero with a missing, empty, unreadable, or invalid UTF-8 artifact is treated as failure.
- The final answer is not inferred from stdout.

**[I]** This artifact-based completion contract is one of the strongest reusable boundaries in the system. It avoids mistaking tool logs or progress commentary for the final response.

### Approval behavior

**[C]** There is no PTY or stdin control protocol for responding to an approval request from a live Codex subprocess.

**[C]** The dashboard’s approval controls approve the supervision planner’s next action, not a request emitted interactively by Codex.

**[U]** Exact behavior when the installed Codex CLI requires an approval depends on its defaults and configuration. The Python engine cannot currently surface and resolve such a request as a first-class state.

### Progress exposure

**[C]** JSON events are normalized into categories including:

- Assistant commentary
- Command started/finished
- Tool activity
- File changes
- Blocked/error state
- Generic JSON events

Command details are bounded and hashed in the progress model. Fields with names suggesting prompt, response, reasoning, analysis, secrets, stdout, or raw content are denied by the dashboard progress sanitizer.

**[C]** The frontend displays only a small number of recent assistant commentary summaries. Commands, tool activity, and file changes are stored but not rendered as equivalent live working notes.

**[C]** Full raw Codex JSON stdout is still persisted in the final execution event.

**[I]** The architecture can expose safe observable progress without exposing hidden reasoning, but it needs a stricter durable event boundary. Storing the raw stream and sanitizing only the UI is weaker than sanitizing before durable persistence.

## 9. ChatGPT destination identity and navigation

### What identifies a conversation

**[C]** A durable destination binding contains only:

```text
project_title
chat_title
```

It does not contain:

- Conversation URL
- ChatGPT project ID
- Conversation ID
- Persistent Accessibility reference
- Window ID
- Sidebar position
- Stored navigation route
- Transcript fingerprint
- ChatGPT account/workspace identity

**[C]** The authoritative fast path looks for a toolbar management-action element whose description or title is exactly:

```text
<chat title>, <project title>
```

It also requires exactly one composer.

**[C]** The fallback attempts to prove:

- Exact active project evidence
- Exact selected chat row
- Exact conversation header
- A confirmed Chats list
- One composer/transcript surface

Evidence is read more than once and must be stable.

### Identity defects

**[C]** Run validation allows commas in titles, while the fast-path parser splits the combined toolbar identity on commas and expects exactly two parts. A comma in either title can disable the authoritative path.

**[C]** Matching is exact and title-based. Renaming either title invalidates the binding.

**[C]** Duplicate project/chat names cannot be robustly distinguished.

**[C]** The current Accessibility snapshot targets the focused ChatGPT Classic window. It does not bind to a stored window identity.

**[C]** Geometry heuristics use absolute bands such as approximate sidebar and main-content coordinates. These assumptions are sensitive to window size, screen arrangement, UI redesign, and scaling.

**[I] Robustness under common changes:**

| Scenario | Current behavior |
|---|---|
| Sidebar reorder | Usually survivable if the exact title remains discoverable |
| Renamed project/chat | Binding fails or navigates by stale title |
| Duplicate titles | Ambiguous; no durable discriminator |
| App restart | May work if the same conversation is restored, but there is no durable conversation identity |
| Window movement | AX frames update, but absolute heuristics remain fragile |
| Multiple ChatGPT windows | Risk of inspecting or acting in the wrong focused window |
| Focus change during transaction | Gate reduces risk, but later actions can race with the change |
| Simultaneous human activity | UI lease does not constrain the human; focus or draft can change |
| Hidden/virtualized sidebar row | Navigation must scroll/search and may fail |
| Different ChatGPT workspace/account | Not explicitly represented in the binding |

**[I]** The destination gate is meaningfully better than blind paste automation, but it is not a security-grade conversation identity. The highest-risk interval is between the last successful gate and the UI mutation.

### Navigation implementation

**[C]** `agent/chatgpt_navigation_diagnostic.py` is 11,810 lines and combines:

- Accessibility-tree acquisition and classification
- Project list discovery
- Chat list discovery
- Scrolling and virtualized-row search
- AXPress actions
- Coordinate click fallback
- CoreGraphics mouse and scroll event generation
- Window-server geometry
- Cursor and Calculator test probes
- Manual diagnostic output
- Production navigation support

**[C]** Despite its name, production supervision imports this module when autonomous navigation is enabled.

**[I]** This is genuine architectural coupling rather than cosmetic file size: observation, identity, search planning, actuation, validation, native-framework adapters, and interactive diagnostics share implementation and state vocabulary.

## 10. Prompt detection protocol

### Correlation with the submitted Codex result

**[C]** Outbound feedback begins with:

```text
AGENT_SUBMISSION
run_id=<uuid>
nonce=<uuid>
payload_sha256=<hash>
END_AGENT_SUBMISSION
```

The marker lets the capture code locate the submitted message and inspect later Accessibility groups.

**[C]** The final feedback body is limited to roughly 100,000 characters.

**[C]** The feedback builder takes the latest Codex completion and separately retrieves the latest classifications, diagnostics, and governance decision.

**[I]** Because these later pieces are selected by recency rather than one explicit invocation ID, abnormal or partially written histories could theoretically combine evidence from different invocations.

### Next-prompt format

**[C]** The automatic supervised path requires exactly:

```text
BEGIN_NEXT_CODEX_PROMPT
<prompt>
END_NEXT_CODEX_PROMPT
```

Rules:

- Exactly one start marker
- Exactly one end marker
- Start precedes end
- Nonempty content
- Surrounding prose allowed and discarded

**[C]** A manual extraction fallback accepts one fenced block preceded by `Codex prompt:`, but the automatic supervised path requires `sentinel_block`, so that fallback does not power autonomous continuation.

### Streaming and malformed responses

**[C]**

- A start marker without an end marker is considered incomplete streaming.
- A complete but changing prompt waits for stability.
- Two stable observations are required.
- Multiple complete candidates fail.
- Stable malformed markers fail.
- Ordinary prose without markers continues polling.
- No response continues polling.
- An interrupted response without a complete sentinel continues polling.
- There is no effective capture timeout; the timeout argument is ignored or removed inside the supervised capture path.

**[I]** The system cannot currently distinguish “ChatGPT is still working” from “ChatGPT intentionally answered without another prompt” from “the UI/capture is broken.”

### Can ordinary conversation be executed?

**[C]** Ordinary prose without sentinels is not automatically sent to Codex.

**[C]** A single valid sentinel pair anywhere in the captured response is accepted. It is not cryptographically bound to a particular message role or structured response object.

**[I]** Quoted instructions, examples, or malicious content containing one sentinel pair could be treated as executable. Multiple pairs fail closed, but one accidental or adversarial pair does not.

**[C]** The extracted prompt event initially says it requires human review, but the current auto-run prompt classifier returns safe for any nonempty prompt. In practice, approval behavior is controlled by the selected profile and planner state, not a substantive prompt-content classifier.

## 11. Clipboard, keyboard, and Accessibility operations

**[C]** The outbound message is written to the global clipboard with `pbcopy`.

**[C]** Previous clipboard contents are not preserved, restored, or cleared.

**[C]** The app activates ChatGPT Classic and uses System Events to ensure it is frontmost.

**[C]** Before pasting, it issues select-all and delete in the composer. Any user draft in that composer is destroyed.

**[C]** Submission prefers an identifiable Accessibility action and falls back to an Enter keystroke.

**[C]** Coordinate click and CoreGraphics input paths exist in the navigation module.

**[C]** A SQLite-backed UI lease serializes cooperating agent processes, but it does not prevent the user or another automation tool from changing the window, focus, or conversation.

**[I]** Accessibility permission is required. Apple Events/Automation permission is likely required for System Events and application activation/control. Synthetic event posting may also trigger Accessibility or Input Monitoring constraints depending on the exact path and OS release.

**[U]** The precise TCC permission sequence must be measured on the supported macOS versions. The implementation contains no production-grade onboarding flow for it.

## 12. Dashboard architecture

### Backend

**[C]** The server uses `ThreadingHTTPServer` and rejects bind hosts other than `127.0.0.1`.

Principal routes include:

```text
GET  /api/health
GET  /api/session
GET  /api/repositories
GET  /api/remote/devices
GET  /api/execution-profile/options
GET  /api/default-greeting
GET  /api/runs/current
GET  /api/runs/current/progress
GET  /api/runs/current/events
GET  /api/chatgpt-ui-lease

POST /api/repository/pick
POST /api/runs/start
POST /api/approval
POST /api/tick
POST /api/runs/current/retry
POST /api/runs/current/cancel
POST /api/runs/current/quota-resume
POST /api/remote/revoke
POST /api/remote/rotate
POST /api/remote/pair
POST /api/chatgpt-ui-lease/release-stale
```

**[C]** Responses set no-store, MIME-sniffing, referrer, and CSP protections. API calls use a controller token or a secure remote cookie.

**[C]** Request bodies are bounded, and origins are validated when supplied. There is no permissive CORS configuration.

### Frontend

**[C]** The frontend is a static PWA with no framework and no external runtime assets.

It provides:

- Repository selection
- Initial task
- Exact project/chat titles
- Optional autonomous navigation
- Sandbox choice
- Model choice
- Locked/default reasoning and approval values
- Start, approve, reject, continue, retry, stop, and stale-lease recovery
- Current state and limited recent progress

**[C]** It does not use `localStorage`, `sessionStorage`, or IndexedDB.

**[C]** Current-run state polling occurs approximately:

- Every second while an action is running
- Every two seconds for an active run
- Every five seconds while idle
- With bounded exponential delay after transient failure

**[C]** Progress uses SSE backed by two-second database polling, with frontend polling fallback. Each SSE connection occupies a server request thread.

**[C]** There is no dashboard run-history API despite the ledger retaining all historical runs.

**[C]** The service worker caches only the static application shell; API traffic is excluded.

### Local versus remote controls

**[C]** All paired remote devices currently receive broad read/control/admin capability.

**[C]** Remote full access requires an owner-enablement flag and an exact typed confirmation phrase. Local full access does not.

**[C]** Selecting `danger-full-access` locally is implicitly translated into confirmation and immediately permits the bypass flag.

**[C] Documentation drift:** `POLICY_MODEL.md` says the local controller/dashboard does not expose full access. The frontend, controller code, and tests do expose it. The implementation is authoritative.

## 13. Error handling and recovery

**[C] Stronger mechanisms:**

- Argument-vector subprocess construction
- Exact destination evidence checks
- Stable repeated AX observations
- UI lease with expected fingerprint and lease token
- Planner action/event IDs and hashes revalidated after approval
- Codex final artifact contract
- Explicit dirty-worktree attribution
- Retry classification for selected failure categories
- Terminate-then-kill Codex cancellation
- Fail-closed handling for ambiguous prompt candidates

**[C] Weak or missing mechanisms:**

- No Codex deadline
- No ChatGPT capture deadline
- No capture cancellation token
- No worker heartbeat/watchdog
- No automatic recovery of interrupted workers
- No process singleton
- No durable active subprocess identity after restart
- No durable ChatGPT conversation ID
- No protection against concurrent human UI activity
- No clipboard restoration
- No user-draft preservation
- No graceful “ChatGPT has no next prompt” protocol
- No first-class Codex approval state
- No database retention or compaction
- No clean worker shutdown guarantee
- No reliable orphan-process cleanup after server loss

## 14. Tests and validation evidence

**[C]** The repository contains approximately 721 `test_*` methods. Substantial coverage exists for:

- CLI shape and validation
- Codex argument construction
- Fake Codex subprocess and final artifacts
- JSONL progress normalization
- Planner actions and governance
- Run/controller transitions
- Pending approval revalidation
- Retry and restore behavior
- Cancellation boundaries
- SQLite UI lease races
- Local server tokens and routes
- Remote authentication and repository restrictions
- Dashboard static contracts
- Sentinel parsing
- Incremental/streaming capture simulation
- Destination-gate fail-closed behavior
- Navigation geometry and scrolling
- Synthetic Accessibility snapshots

**[C]** The navigation diagnostic alone has more than 200 tests. Controller, supervision, and ledger areas also have extensive characterization coverage.

**[C]** Most platform tests inject fake readers, windows, AX trees, clickers, clipboard handlers, or subprocesses. Tests that use words such as “live” generally mean incrementally changing simulated observations, not an actual ChatGPT application session.

**[U] Critical behavior not proven automatically:**

- Actual ChatGPT Classic Accessibility hierarchy
- UI compatibility across ChatGPT releases
- Real project/chat navigation
- Multiple ChatGPT windows
- Duplicate and renamed conversations
- Window movement, Stage Manager, and Spaces
- ChatGPT streaming notifications
- Real Codex CLI approval behavior
- Actual Accessibility/Automation onboarding
- Long-running process recovery
- Multi-process SQLite behavior at current database size
- Storage retention
- Signing and notarization
- End-to-end ChatGPT→Codex→ChatGPT operation
- Simultaneous user interaction
- Orphaned subprocess recovery

**[I]** The suite protects internal contracts well but does not prove the external system on which the product most depends.

## 15. Architectural hotspots

### `agent/cli.py`

**[C]** Responsibilities include dispatch, legacy workflows, service invocation, validation, human-readable rendering, direct ChatGPT actions, supervision commands, and safety checks.

**[I]** Its problem is not merely length. It duplicates boundary logic that also lives in controller and service modules, making it unclear which entry point defines canonical behavior.

### `agent/chatgpt_navigation_diagnostic.py`

**[C]** It combines read-only discovery, production navigation, native-framework bindings, input actuation, geometry, search algorithms, outcome types, and manual diagnostics.

**[I]** It should not be split by arbitrary line count. The coherent seams are:

1. Read-only AX observation adapter
2. Conversation identity evidence
3. Navigation/search planner
4. Input actuator
5. Post-action verifier
6. Manual diagnostic applications

Each seam needs characterization tests before extraction.

### Existing abstractions worth preserving

**[C/I]**

- Codex final-message artifact contract
- Direct argument-vector subprocess construction
- Normalized progress-event model
- Git before/after attribution concepts
- Governance and classification algorithms
- Event ID/hash approval revalidation
- Prompt-envelope parsing as a discrete service
- Destination gate as a fail-closed concept
- Cross-process UI lease concept
- Immutable execution profile
- Planner/service separation
- Static frontend’s lack of external runtime dependencies

### Responsibilities dangerously mixed

**[I]**

- Dashboard server and orchestration engine
- Run state, planner state, governance, controller state, and handoff phase
- Diagnostic navigation and production UI mutation
- Observation and actuation
- Persistence and huge evidence blobs
- User approval policy and Codex sandbox selection
- Prompt extraction and permissive auto-run policy
- Process lifecycle and daemon-thread lifecycle
- UI titles and conversation identity

## 16. Security and privacy assessment

### Positive properties

**[C]**

- Local backend binds only to loopback.
- API uses an unguessable session token.
- Token begins in the URL fragment rather than a query string.
- No broad CORS.
- Shell interpolation is avoided in key command paths.
- Repository paths are resolved before remote allowlist checks.
- UI actions require destination evidence.
- Prompt candidates fail on ambiguity.
- Full access is represented explicitly in the profile and Codex command.
- Remote tokens are stored hashed.
- Remote cookies are Secure, HttpOnly, and SameSite Strict.

### Serious risks

**[C] Plaintext sensitive persistence:** Prompts, Codex responses, raw JSONL, file contents, Git evidence, AX-derived text, repository paths, diagnostics, and diffs are stored in a plaintext database or files. There is no encryption or expiry.

**[C] Clipboard exposure:** Full Codex output is placed in the global clipboard and can be observed by clipboard history tools or other processes.

**[C] Wrong-conversation risk:** Titles plus focused-window evidence are not a durable identity. A focus or conversation change after validation can redirect output.

**[C] Draft destruction:** The current composer is selected and deleted before paste.

**[C] Full-access risk:** Local selection of full access is effectively treated as sufficient confirmation for unrestricted Codex execution.

**[C] Prompt-trust risk:** The application executes arbitrary extracted natural-language instructions. With full access, sandboxing and Codex approval are bypassed.

**[C] Sentinel spoofing:** One valid sentinel pair in an otherwise ordinary response can be accepted.

**[C] Local malicious process risk:** Any local process running as the same user can potentially inspect browser state, tokens in process memory, the local database, or the clipboard. Loopback binding is not a defense against same-user malware.

**[C] Stale-process risk:** Active workers and subprocess identity are not durable, and server shutdown is not coordinated with workers.

**[C] Database-growth risk:** Sensitive repository contents are duplicated aggressively and have already produced a multi-gigabyte ledger.

**[I]** The most consequential current failure is not a crash; it is a plausible but wrong action: returning output to the wrong chat or executing a prompt under a stronger permission policy than the user intended.

## 17. Feasibility of the proposed native macOS companion

**[I] Overall judgment: feasible as an incremental companion, but not safe as a thin native skin over the current orchestration.**

The proven Codex/git/governance core can remain Python initially. The ChatGPT identity, observation, user authorization, window tracking, process supervision, and lifecycle model need a new boundary before the product can responsibly become persistent and always-ready.

**[U] There is no repository evidence that ChatGPT Classic exposes a supported extension or plugin API.** The design should assume a separate companion application unless OpenAI supplies a documented API later.

### Recommended product form

**[I]** Use a separate signed macOS application with:

- A normal settings/inspection window
- A menu-bar status item
- An optional nonactivating contextual panel near the ChatGPT window
- A supervised Python execution helper
- A single explicit binding between conversation, repository, execution profile, and loop generation

A menu-bar-only application is too constrained for permission onboarding, repository binding, full-access warnings, audit detail, failure recovery, and run inspection.

### SwiftUI and AppKit

**[I]**

- SwiftUI is appropriate for settings, run history, approval screens, progress, and state visualization.
- AppKit should own `NSPanel`, focus behavior, screen/window positioning, activation policy, and low-level Accessibility integration.
- The result should be a mixed SwiftUI/AppKit application, not an ideological all-SwiftUI implementation.

### Panel placement

**[I]** The contextual control should be an independent companion panel that tracks the ChatGPT window frame. It should not be modeled as a true child or extension of the other process’s window.

Apple exposes window collection behaviors for Spaces and full-screen participation, but those options do not guarantee that an independent panel will visually remain attached to another application in every Space, full-screen mode, Stage Manager configuration, or focus transition. This needs direct prototyping against ChatGPT. See Apple’s `NSWindow.CollectionBehavior` documentation:

https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct

### Accessibility observer versus polling

**[I]** Prefer:

1. `AXObserver` notifications as wake-up signals.
2. Debounced Accessibility snapshots for validation.
3. Low-frequency watchdog polling for missed notifications.
4. Explicit revalidation immediately before every mutation.

Apple supplies `AXObserver`, but the existence of the API does not prove that ChatGPT’s web-rendered conversation elements emit useful notifications for message streaming or tree changes:

https://developer.apple.com/documentation/applicationservices/axobserver

**[U]** A prototype must determine which notifications ChatGPT emits for:

- Focused window change
- Window movement and resizing
- Conversation navigation
- Composer edits
- Transcript child additions
- Streaming text changes
- Response completion

### Direct distribution

**[C—Apple platform]** A directly distributed app should be Developer ID signed, use the hardened runtime, and be notarized. Embedded executables and helper components must also have valid signatures and compatible runtime settings.

Official references:

- https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution?changes=_5
- https://developer.apple.com/documentation/security/hardened-runtime
- https://developer.apple.com/documentation/Xcode/preparing-your-app-for-distribution

**[I]** A fully App-Sandboxed design is likely awkward for arbitrary repository access, Accessibility, external Codex invocation, and a bundled Python runtime. Direct distribution permits evaluating an unsandboxed but hardened app, though that increases the application’s responsibility to implement least privilege itself.

Apple App Sandbox guidance:

https://developer.apple.com/documentation/xcode/configuring-the-macos-app-sandbox

**[U]** Packaging the Python interpreter, Python helper, native libraries, and possibly Codex itself under hardened-runtime and notarization rules requires a packaging spike. The first release may instead require an installed Python/Codex engine and treat the native app as a signed controller.

## 18. Recommended target architecture

```text
ChatGPT Classic
     │
     │ AX notifications + verified snapshots
     ▼
Swift macOS companion
  ├─ Conversation binding and fingerprint
  ├─ Window/panel tracking
  ├─ Permission onboarding
  ├─ Explicit arming and approvals
  ├─ Authoritative session state machine
  ├─ Serialized ChatGPT UI actor
  └─ Progress/audit UI
     │
     │ Versioned authenticated local IPC
     ▼
Python execution helper
  ├─ CodexExecutor
  ├─ Git evidence and attribution
  ├─ Governance/policy engine
  ├─ Prompt/output envelope validation
  ├─ Bounded event/artifact store
  └─ Process cancellation and recovery
     │
     ▼
Codex CLI + repository
```

### State ownership

**[I]** The Swift application should own the user-facing and supervision state machine:

```text
unbound
idle
observing
prompt_detected
awaiting_approval
queued
starting_codex
running
waiting_for_codex_permission
returning_output
waiting_for_chatgpt
paused
stopping
blocked
failed
complete
```

These should be transitions in one reducer or actor, not separately inferred from run status, controller strings, planner actions, and handoff events.

### Conversation binding

**[I]** A binding record should include at least:

- ChatGPT bundle identifier
- App process generation
- Window identity where available
- Project and chat titles
- Toolbar identity evidence
- Transcript/composer structural fingerprint
- Last observed message fingerprints and ordering
- Last outbound submission nonce/hash
- Repository identity and canonical path
- Execution profile
- Binding generation
- Armed mode and expiry
- Last verified timestamp

Titles remain useful evidence but must not be the identity.

Renaming, duplicate titles, workspace changes, lost window identity, or contradictory fingerprints should move the session to `blocked` or `needs_rebind`, never trigger automatic navigation.

### UI mutation

**[I]** One serialized Swift actor should own all ChatGPT mutations.

Before each action it should:

1. Confirm the bound application and window.
2. Confirm project/chat fingerprint.
3. Confirm no unexpected message transition.
4. Confirm the session is still armed.
5. Confirm the exact payload hash and action generation.
6. Mutate.
7. Verify the result and conversation identity again.

Prefer setting the composer’s Accessibility value and invoking an Accessibility action if ChatGPT exposes those operations reliably. Clipboard and keyboard input should be explicit fallbacks. A fallback should warn about draft replacement and restore prior clipboard contents where practicable.

### Prompt envelope

**[I]** The sentinel contract should evolve into a structured, correlated envelope. At minimum, a next prompt should bind to:

- Previous submission nonce
- Run/session ID
- Conversation binding generation
- Prompt ID
- Explicit action type
- Prompt content
- Optional stop/no-more-work result
- Integrity hash over canonical content

This does not make ChatGPT output trusted, but it prevents stale, quoted, or cross-run prompts from being silently treated as current.

### Arming and approval

**[I]**

- One-shot execution should be the default.
- Continuous looping must be explicitly armed.
- Arming should be scoped to one conversation binding and repository.
- It should have a visible indicator and preferably an expiry or maximum-step limit.
- Any identity uncertainty, permission increase, repository change, ordinary human navigation, or unrecognized response should pause the loop.
- Full access should require a separate explicit confirmation and should never be inferred merely from a stored sandbox selection.

### IPC

**[I]** Do not import Python into the Swift process.

Use either:

- A signed helper managed through XPC, or
- A supervised Python helper using a Unix-domain socket with mode `0600`

The IPC protocol should be versioned and typed, with:

- Request and event IDs
- Run and invocation IDs
- Idempotency keys
- Monotonic event sequence numbers
- Bounded message sizes
- Capability negotiation
- Execution-profile schema version
- Cancellation request and acknowledgment
- Heartbeats
- Helper version
- Codex version and supported-option discovery
- Resumable progress subscription
- Explicit terminal results

Localhost HTTP could be retained temporarily for development, but it is not the best long-term private helper boundary.

## 19. Reuse and migration boundaries

### Reuse substantially unchanged

**[I]**

- Codex argument-vector execution
- Final-message artifact validation
- JSONL progress normalization, after tightening persistence
- Git before/after attribution algorithms
- File and governance classification logic
- Event/hash approval revalidation
- Prompt parser as a pure component
- Feedback envelope construction, with stronger correlation
- Execution-profile immutability
- Destination-gate fail-closed principle

### Wrap behind explicit interfaces

**[I]**

```text
CodexExecutor
RunStore / EventStore
ArtifactStore
PolicyEngine
PromptEnvelopeParser
ProgressSink
CancellationToken
ExecutionProfile
UIHandoffPort
Clock / Deadline
ProcessSupervisor
```

The Python engine must not know whether ChatGPT is controlled through today’s Python Accessibility code or a future Swift adapter.

### Refactor before native integration

**[I]**

1. Create one explicit lifecycle state machine.
2. Add deadlines and cancellation to Codex, capture, navigation, and handoff services.
3. Separate event metadata from large artifacts.
4. Add database indexes, WAL/busy handling, retention, and schema migration.
5. Correlate all evidence to an invocation ID.
6. Move the database to an explicit Application Support path.
7. Make active process lifecycle durable enough to detect and clean stale children.
8. Extract production navigation from the diagnostic god file.
9. Remove or formalize the unused handoff queue.
10. Negotiate Codex CLI capabilities instead of hardcoding models/options.
11. Separate planner approval from Codex subprocess approval.
12. Ensure shutdown waits for or terminates workers cleanly.

### Move into Swift early

**[I]**

- Accessibility observation
- Conversation/window binding
- Panel positioning
- Focus preservation
- Spaces/full-screen handling
- Permission onboarding
- ChatGPT UI action serialization
- User arming, pause, and stop
- Native settings and repository selection
- Visible state and progress
- Local notifications

### Do not preserve

**[I]**

- Title-only identity
- Focused-window-as-session identity
- Absolute coordinate bands
- Infinite capture polling
- Global clipboard as primary transport
- Silent draft deletion
- Implicit full-access confirmation
- “Any nonempty prompt is safe”
- Relative database path
- Multi-megabyte JSON blobs in SQLite events
- Daemon workers without a supervisor
- In-memory-only subprocess ownership
- Dashboard server as the essential controller
- Distributed, overloaded lifecycle states

## 20. Principal blockers and required prototypes

| Question | Status | Evidence needed |
|---|---|---|
| Does ChatGPT expose stable project, chat, message-role, composer, and transcript AX elements? | **[U]** | Read-only AX captures across versions and account layouts |
| Are there useful AX notifications during streaming? | **[U]** | AXObserver trace covering start, delta, stop, interrupt, regenerate |
| Can a conversation be identified beyond visible titles? | **[U]** | Inspect identifiers, attributes, window metadata, and stable transcript fingerprints |
| Can the composer be set and submitted without clipboard/keystrokes? | **[U]** | AX settable/action prototype |
| Can completion be distinguished from a paused/interrupted response? | **[U]** | Streaming state matrix and explicit prompt-envelope protocol |
| Can a contextual panel track ChatGPT through Spaces/full-screen/Stage Manager? | **[U]** | AppKit panel prototype on supported OS versions |
| Can focus be preserved during safe mutation? | **[U]** | Nonactivating-panel and AX mutation prototype |
| Which TCC permissions are actually required? | **[U]** | Clean-machine onboarding tests |
| Can Python and its dependencies be notarized as a bundled helper? | **[U]** | Signed/hardened/notarized packaging spike |
| Can Codex approvals be surfaced through a stable machine protocol? | **[U]** | Inspect supported Codex CLI protocols/version behavior |
| Can the helper safely survive app restart and sleep/wake? | **[U]** | Process-supervision and crash-recovery prototype |
| Can multiple workstreams be supported safely? | **[I/U]** | First establish durable conversation binding and serialized per-window ownership |
| Is phone/watch supervision ready to design? | **[I] No** | Stabilize local state, authentication, approval, and event protocols first |

## 21. Chronological recommendation

1. **Freeze and characterize the current boundaries.** Add no architecture rewrite yet. Capture pure fixtures for execution profiles, planner decisions, Codex events, prompt envelopes, destination evidence, and cancellation behavior.

2. **Run a read-only ChatGPT Accessibility feasibility study.** Test multiple projects, duplicate titles, renamed chats, multiple windows, streaming, interruptions, app restart, Spaces, full screen, and simultaneous navigation. This is the proposal’s most important unknown.

3. **Prototype the native panel independently.** Prove frame tracking, focus behavior, AXObserver usefulness, permission onboarding, and behavior across supported macOS versions.

4. **Specify conversation identity and the prompt envelope.** Decide what evidence constitutes a binding, when it becomes invalid, how stale prompts are rejected, and how ChatGPT communicates `next_prompt`, `stop`, `needs_user`, and malformed output.

5. **Specify the authoritative state machine.** Include arming, expiry, interruption, human takeover, Codex approval, cancellation acknowledgment, helper loss, and rebind behavior.

6. **Define the versioned Swift↔Python IPC protocol.** Include capability negotiation, event ordering, idempotency, cancellation, heartbeats, and restart recovery.

7. **Refactor the Python core behind those ports.** Prioritize bounded waits, process supervision, invocation correlation, and storage redesign. Do not begin by splitting large files cosmetically.

8. **Ship an observe-only Swift companion.** It should bind a conversation, detect a candidate prompt, and show why it believes the prompt belongs to that chat. It should not execute yet.

9. **Add explicit one-shot execution.** Require an approval card showing conversation, repository, prompt hash, model, sandbox, and permission consequences.

10. **Add verified output return.** Revalidate immediately before and after composer mutation. Keep clipboard/keyboard fallback visibly marked.

11. **Add continuous-loop arming only after one-shot behavior is reliable.** Scope it tightly, show persistent state, expire it, and pause on every identity anomaly.

12. **Address direct distribution.** Package and notarize the app/helper, test clean-machine permissions, implement upgrades, and decide whether Codex/Python are bundled or prerequisites.

13. **Only then design remote phone/watch supervision.** It should consume the same authenticated state/event protocol, not control ChatGPT or Codex through a second orchestration path.

## Bottom line

**[C]** The repository already contains valuable execution, evidence, governance, and fail-closed concepts. It is not a disposable prototype.

**[C]** Its weakest foundation is the part most central to the proposed native companion: identifying and controlling the correct ChatGPT conversation over time.

**[I]** A separate Swift companion with an initially retained Python engine is the right incremental direction. A full Swift rewrite would discard proven Codex and Git behavior without solving the harder ChatGPT identity problem.

**[I]** The native app must not simply reproduce today’s title matching, focused-window assumptions, polling, clipboard paste, and distributed controller state. Making those behaviors persistent would amplify their risks.

**[U]** The proposal’s go/no-go question is whether ChatGPT Classic exposes enough stable Accessibility evidence and notifications to create a durable, revalidated conversation binding. That must be proven before committing to the overlay experience or an autonomous always-ready loop.
