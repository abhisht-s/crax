# Policy Model

This document records current governance and policy behavior. It does not change
policy behavior.

## Terms

- Enforcement: a rule directly blocks or changes execution/status.
- Review requirement: a rule allows the run to stop in `needs_review` or
  `waiting_for_approval` until a human decision is recorded.
- Observation: a rule records evidence, warnings, mismatches, classifications,
  or flags but does not itself block execution.
- Diagnostics: analysis output used as policy input or operator evidence. A
  diagnostic is not enforcement unless another policy consumes it and blocks.

## Current Governance Flow

After Codex execution, `agent/governance_services.py` captures git/evidence,
classifies changed files, runs prompt/repo impact diagnostics, evaluates
supervision policy, builds governance observations, and records a run status
transition.

The main status transition comes from
`agent/run_status_policy.py::status_from_supervision_decision`:

- `continue` and `record_only` can complete the run.
- `needs_review` moves the run to `needs_review`.
- `approval_required` moves the run to `waiting_for_approval`.
- objective Codex failures move the run toward review.

Explicit governance objective failures can force `needs_review`.

## Risk Policy Is Observation-Heavy

`agent/risk_policy.py` is explicitly versioned as
`risk_policy_v2_observation_only`. Its current behavior is observation-heavy:

- Missing diagnostics produce a `needs_review` decision.
- If diagnostics contain flags, the current policy returns `record_only`, not
  review or approval.
- If diagnostics contain no flags, it returns `continue`.

This means diagnostic flags are currently recorded as context in this policy,
not automatically elevated to review.

## Workspace-Write Policy Is Permissive In Places

Document workspace-write honestly. Do not claim it blocks every concerning
change.

`agent/workspace_write_policy.py` defines tiers and scope concepts, but current
pre-run classification for `workspace-write` returns
`workspace_write_scoped_auto` with `allowed=True`. If explicit contract paths
are absent, it records a permissive expected scope over auto-allowed categories
with a large changed-file limit and includes the matched rule
`safety_classifiers_disabled`.

Post-run verification also returns `allowed=True` in multiple concerning cases,
including unexpected files, prohibited files, invalid paths, and content flags.
Those results are recorded as observations with reason codes such as
`post_run_observations_recorded`, and matched rules can include
`safety_classifiers_disabled`.

There is still some enforcement outside workspace-write post-run verification:
prompt contract path-safety failures can preflight-fail Codex execution, and
governance objective failures such as explicit read-only changed files,
read-only sandbox attributable writes, invalid contract paths, and high
confidence secret literals can force review.

## Danger-Full-Access Expectations

`danger-full-access` exists as an allowed Codex sandbox value in
`agent/codex_terminal.py`, but it has explicit confirmation and availability
limits:

- `agent/cli.py` requires `--confirm-full-access` for `codex-run` with
  `--sandbox danger-full-access`.
- `agent/extracted_prompt_services.py` blocks danger-full-access by default
  unless full access is explicitly allowed and confirmed.
- `agent/supervise.py` rejects danger-full-access for supervise in the current
  version.
- `agent/local_controller.py` and `agent/local_server.py` do not expose
  danger-full-access through the local dashboard.

## Approval And Review Concepts

Approval/review concepts are present in several places:

- Supervision decisions may carry `needs_review` or `approval_required`.
- Run statuses include `needs_review`, `waiting_for_approval`, and `approved`.
- Human decisions include approve, reject, and complete review.
- Supervision can require approval before sending Codex results to ChatGPT or
  before running an extracted Codex prompt.
- The local controller can store a pending approval snapshot to make sure the
  approved planner action and prompt have not changed before execution.

## Future Decision Needed

The next policy design decision is what should block automatic handoff versus
what should only be recorded. In particular, future work must decide how to
treat:

- diagnostic flags,
- workspace-write unexpected/prohibited files,
- low-confidence secret-like content,
- dirty repository state before Codex,
- prompt contract mismatches,
- non-sentinel or malformed ChatGPT output,
- UI destination/navigation uncertainty.

Any change from observation to enforcement is a behavior change and should be
implemented separately with characterization tests.
