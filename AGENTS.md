# Repository Agent Instructions

This repository is a local Python/macOS supervised agent prototype. It connects
Codex CLI execution, local git evidence, a local SQLite ledger, a localhost
dashboard, and ChatGPT Desktop handoff/capture helpers.

## Operating Principles

- Preserve current behavior over perfect architecture. If a cleaner design would
  change behavior, defer the design or add characterization coverage first.
- Audit and design before implementation for risky changes, especially changes
  touching supervision flow, policy, Codex execution, ChatGPT Desktop automation,
  ledger persistence, or local dashboard control.
- Do not do broad refactors without characterization tests that preserve the
  observed behavior being moved.
- Do not invoke Codex from inside Codex or start a nested Codex run while working
  in this repository.
- Do not perform live desktop or UI automation unless the user explicitly asks
  for that live action. This includes ChatGPT Desktop, Calculator, browser UI,
  macOS Accessibility, CoreGraphics events, AppleScript/System Events,
  clipboard operations, and paste/keypress/click actions.
- Do not install dependencies unless the user explicitly approves the install.
- Use focused tests only when tests are needed. Prefer narrow tests around the
  behavior being changed instead of broad test runs by default.
- Commands and shell snippets in docs, comments, and reports must use literal
  ASCII flags such as `--help` and `--confirm-run`.

## Source Of Truth

- The current authoritative source tree is `agent/`.
- `build/lib/agent` is build output and is not authoritative. Static comparison
  currently shows multiple files under `build/lib/agent` differ from `agent/`, so
  do not treat `build/lib/agent` as the source to edit or document.
- Do not modify `build/lib` unless the task explicitly concerns generated build
  output.

## Current Refactor Hotspots

- Treat `agent/cli.py` and `agent/chatgpt_navigation_diagnostic.py` as god-file
  refactor targets.
- Do not split either file casually. First characterize the existing behavior and
  extract only a coherent, low-risk seam with tests.
- Prefer moving stable service boundaries into existing service modules when that
  clearly preserves behavior.

## Documentation Rule

Documentation must distinguish code-confirmed behavior from intended future
architecture. If a document describes a target that the code does not yet
implement, say so explicitly.
