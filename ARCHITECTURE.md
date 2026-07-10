# Architecture

## Product Purpose

This project is a local supervised agent loop for macOS. In plain English: it
lets a user start a local run, have Codex work in a local repository, send Codex
results into ChatGPT Desktop for review, capture ChatGPT's next instruction, and
continue the loop under policy and human supervision.

This is a prototype. The code works, but module boundaries are imperfect.

## Current Shape

The target architecture is a modular monolith: one Python package with clear
internal boundaries, one local ledger, one local controller/dashboard, and
adapter modules for Codex, git, ChatGPT Desktop, and macOS diagnostics.

The current code is organized around these layers:

- Harness core: run creation, continuation checks, supervision planning, and
  supervision step execution live mainly in `agent/run_services.py`,
  `agent/continuation_policy.py`, `agent/supervise.py`, and
  `agent/supervision_services.py`.
- Policy/governance: prompt contracts, file classification, prompt/repo impact
  diagnostics, risk policy, workspace-write policy, and post-Codex governance
  live in `agent/prompt_contract.py`, `agent/file_classifier.py`,
  `agent/run_diagnostics.py`, `agent/risk_policy.py`,
  `agent/workspace_write_policy.py`, `agent/run_status_policy.py`, and
  `agent/governance_services.py`.
- Ledger/evidence: durable runs, events, Codex progress, destination binding,
  execution profile, ChatGPT UI leases, and handoff queue state are stored by
  `agent/ledger.py` in local SQLite.
- Codex/Git execution adapters: Codex subprocess execution and git evidence
  capture live in `agent/codex_terminal.py`, `agent/codex_services.py`,
  `agent/initial_codex_run_services.py`, `agent/extracted_prompt_services.py`,
  and `agent/git_snapshot.py`.
- ChatGPT handoff/capture adapters: feedback generation, clipboard transfer,
  ChatGPT activation, paste/submit, Accessibility response capture, destination
  binding, and destination verification live in `agent/gpt_feedback.py`,
  `agent/chatgpt_services.py`, `agent/chatgpt_ax_capture.py`,
  `agent/chatgpt_ax_destination_snapshot.py`,
  `agent/chatgpt_destination_gate.py`, `agent/mac_app_control.py`,
  `agent/mac_paste.py`, and `agent/mac_ui_inspect.py`.
- Local dashboard/controller: the localhost HTTP server, browser-facing API, run
  read model, approval workflow, and background controller worker live in
  `agent/local_server.py`, `agent/local_controller.py`, and
  `agent/web_static/`.
- CLI facade/dispatch: `agent/cli.py` remains the compatibility CLI facade and
  dispatch entry point; parser construction now lives in `agent/cli_parser.py`.
- Diagnostics/manual-only desktop automation: ChatGPT navigation inspection,
  frame-click verification, coordinate calibration, scroll search, project/chat
  navigation, current-cursor click probes, and Calculator click probes live
  mostly in `agent/chatgpt_navigation_diagnostic.py`.

## Current Reality

The current implementation is a modular monolith in progress, not a cleanly
layered system. Some important behavior crosses boundaries:

- `agent/cli.py` remains the compatibility CLI facade and dispatch/orchestration
  entry point. Parser construction has moved to `agent/cli_parser.py`, while
  command handlers, printing, and adapters for service calls still mostly live
  in `agent/cli.py`.
- `agent/chatgpt_navigation_diagnostic.py` contains read-only diagnostics,
  explicit action probes, autonomous navigation experiments, geometry helpers,
  CoreGraphics click/scroll services, and Calculator probes in one file.
- State decisions are split across status constants, status policy, continuation
  policy, the supervision planner, governance, the controller read model, and
  human decision services.
- Workspace-write policy currently includes permissive/observation-heavy paths,
  so architecture docs must not imply stronger blocking than the code provides.

## Why Not Microservices

Do not move this project to microservices as the next stabilization step.

The core problems are local orchestration, evidence consistency, UI safety, and
clear control boundaries. Splitting into services would add process, networking,
authentication, deployment, and synchronization complexity before the internal
state model is clean. A modular monolith keeps the local SQLite ledger,
supervision flow, and macOS desktop constraints in one debuggable process while
still allowing clear module ownership.

## God Files And Future Split Direction

The two current god-file refactor targets are:

- `agent/cli.py`: parser construction has been extracted to
  `agent/cli_parser.py`. Future split should separate command handlers,
  print/report formatting, and orchestration adapters. Keep CLI behavior stable
  while moving logic behind service functions.
- `agent/chatgpt_navigation_diagnostic.py`: future split should separate
  read-only AX inventory, destination selection, frame geometry/click planning,
  CoreGraphics click/scroll services, project chat list logic, calibration, and
  manual probes. Promote only proven behavior into production adapters.

These splits should be behavior-preserving. Do not split them casually, and do
not treat a smaller file count as success without characterization coverage.

## Build Output

`pyproject.toml` packages from `agent/`, not from `build/lib/agent`. Static
inspection also shows multiple differences between `agent/` and
`build/lib/agent`. Therefore `build/lib/agent` is non-authoritative and stale
generated output for architecture purposes.

## Refactor Principle

Do not change behavior during refactor. First document and characterize current
behavior, then move code behind equivalent interfaces, then change behavior only
in a separate, explicit behavior-change task.
