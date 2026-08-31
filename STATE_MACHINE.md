# State Machine

This document records current run/status behavior from the code. It does not
invent a formal state machine that does not yet exist.

## Known Run Statuses

`agent/run_state.py` defines the known run statuses:

- `created`
- `running`
- `needs_review`
- `waiting_for_approval`
- `approved`
- `rejected`
- `completed`
- `failed`

These are represented by `RunStatus`.

## Where Decisions Currently Live

There is no single authoritative state-machine owner yet. Status and transition
authority is distributed across several modules:

- Status constants: `agent/run_state.py`.
- Governance-to-status transition policy:
  `agent/run_status_policy.py::status_from_supervision_decision`.
- Continuation eligibility:
  `agent/continuation_policy.py::can_continue_run`.
- Human decision transitions:
  `agent/run_services.py::resolve_human_decision` through the
  `_HUMAN_DECISION_SPECS` table.
- Supervision planning:
  `agent/supervise.py::detect_next_supervise_action`.
- Supervision step execution and handoff phase recording:
  `agent/supervision_services.py::run_supervision_step`.
- Post-Codex governance and final run status update:
  `agent/governance_services.py::apply_post_codex_governance_service`.
- Local dashboard/controller state:
  `agent/local_controller.py` controller states and read model construction.

## Code-Confirmed Status Transitions

Post-Codex governance maps supervision decisions to run statuses:

- `continue` -> `completed`
- `record_only` -> `completed`
- `needs_review` -> `needs_review`
- `approval_required` -> `waiting_for_approval`
- missing or unknown supervision decision -> `needs_review`

Codex objective failures such as validation failure, Codex not found, timeout,
missing exit code, or nonzero exit change the transition to review-oriented
status. If the decision was `approval_required`, the next status remains
`waiting_for_approval`; otherwise objective failure leads to `needs_review`.

Explicit governance objective failures can also force `needs_review`.

## Human Decisions

Human decisions are represented by `HumanDecision` in `agent/run_services.py`:

- `approve`
- `reject`
- `complete_review`

Current allowed human transitions:

- `approve` is allowed from `waiting_for_approval` or `needs_review` and moves
  the run to `approved`.
- `reject` is allowed from `waiting_for_approval` or `needs_review` and moves
  the run to `rejected`.
- `complete_review` is allowed from `needs_review` and moves the run to
  `completed`.

Invalid human decisions are recorded/rejected by state-specific event types.

## Continuation Rules

`agent/continuation_policy.py::can_continue_run` currently allows continuation
only from:

- `completed`
- `approved`

It denies continuation from:

- `created`: initial Codex step has not run.
- `running`: current step is still active.
- `needs_review`: review must be completed or the run rejected.
- `waiting_for_approval`: approval or rejection is required.
- `rejected`: start a new run or fix manually.
- `failed`: inspect failure.
- unknown status: inspect run.

The supervision planner also has earlier stop checks for statuses such as
`created`, `running`, `failed`, `rejected`, `needs_review`, and
`waiting_for_approval`.

## Supervision And Planner Phases

At a high level, `detect_next_supervise_action` chooses one of these
`SuperviseAction` values:

- `stop`
- `ask_send_to_gpt`
- `capture_gpt_response`
- `extract_next_prompt`
- `ask_run_prompt`

The planner checks, in order, repository/sandbox validity, run existence,
an active Codex quota wait (`waiting_for_quota_reset`), blocking statuses,
latest Codex result validity, incomplete extracted-prompt
runs, required diagnostics/supervision decision, continuation eligibility,
ChatGPT submission state, response capture state, extracted prompt validity,
sentinel format, and whether the extracted prompt has already run.

## Codex quota wait (dashboard / local controller)

This wait path is implemented for the localhost dashboard controller. It is not
a new `RunStatus` and it does not change CLI `codex-run` auto-supervise.

When Codex exec fails, the controller schedules a wait only if
`decide_quota_wait(...).scheduled` is true: usage-limit error text, Codex
`thread_id`, and a future reset time. The run status is restored to `running`,
a `codex_quota_wait_scheduled` event is recorded, and a timer resumes the same
thread with `codex exec resume`. Incomplete signals keep the previous path:
post-Codex governance `needs_review` plus controller `blocked` /
`local_controller_action_failed`.

While a wait is active, the planner returns `STOP` with
`waiting_for_quota_reset`. That reason is not blocked, not terminal, and not
completed. Operator cancel during the wait writes `codex_quota_wait_cancelled`
and returns to `needs_review` plus controller `blocked`.

`run_supervision_step` executes the selected action. For ChatGPT handoff work,
the implementation records bounded phase labels such as:

- `navigation_not_requested`
- `navigation_started`
- `navigation_succeeded`
- `navigation_failed`
- `verification_started`
- `verification_failed`
- `verification_succeeded`
- `submission_started`
- `capture_started`
- `continuation_started`

Those labels are handoff phase evidence, not the global run state machine.

## Known Issue

State authority is distributed. The run status, local controller state, handoff
phase, supervision planner action, policy decision, and ledger event stream are
related but not owned by one explicit state-machine module yet.

## Future Target

The future target is one explicit state-machine owner that:

- Defines statuses, events, allowed transitions, and terminal states in one
  place.
- Treats policy and diagnostics as inputs to transition decisions.
- Keeps controller UI state as a read model over the authoritative run state.
- Preserves existing behavior during migration.
