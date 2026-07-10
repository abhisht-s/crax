# Diagnostics Vs Production

This document classifies current capabilities. It is intentionally conservative:
manual probes and experimental navigation must not be treated as normal
production loop behavior unless they are explicitly promoted in a separate
behavior-change task.

## Normal Loop Capabilities

Normal loop behavior currently includes:

- Creating and showing local runs.
- Running Codex through the Codex CLI adapter.
- Capturing git snapshots, invocation deltas, changed-file classification,
  diagnostics, governance observations, and run status transitions.
- Building a ChatGPT feedback payload from Codex output.
- Supervision planning over ledger events.
- Local dashboard state/read-model display.
- Human approval, rejection, and review completion.
- Extracting a sentinel-wrapped next Codex prompt from a captured ChatGPT
  response.

The production loop may use ChatGPT handoff/capture only through the supervised
service path, with the destination gate, UI lease, and required confirmation or
auto-safety rules in place.

## Manual Diagnostic Capabilities

Manual diagnostics are tools for local investigation. They should not become
routine autonomous behavior without explicit promotion.

Manual diagnostic capabilities include:

- `inspect-chatgpt-ui`
- `inspect-chatgpt-navigation-ui`
- `inspect-chatgpt-sidebar-destination`
- `inspect-chatgpt-project-visible-chats`
- `inspect-chatgpt-project-chat-row-ax`
- `diagnose-chatgpt-project-chat-rows`
- `calibrate-chatgpt-sidebar-coordinate-mapping`
- `verify-chatgpt-sidebar-frame-click`
- `verify-synthetic-click-delivery`
- `verify-current-cursor-click`
- `test-chatgpt-target-paste`

These commands can read or manipulate macOS UI state depending on flags. Their
presence in the codebase does not make them safe production loop primitives.

## Confirmed-Action Capabilities

Some commands are dry-run by default and only perform actions when an explicit
confirmation flag is present. Examples include:

- `paste-feedback-to-chatgpt --confirm-paste`
- `submit-feedback-to-chatgpt --confirm-submit`
- `capture-gpt-response-from-chatgpt-ax --confirm-capture`
- `extract-next-codex-prompt --confirm-extract`
- `run-extracted-codex-prompt --confirm-run`
- `open-chatgpt-sidebar-destination --confirm-open-destination`
- `open-chatgpt-project-chat --confirm-open-chat`
- `verify-chatgpt-sidebar-frame-click --confirm-frame-click`
- `calibrate-chatgpt-sidebar-coordinate-mapping --confirm-calibration-click`
- `verify-synthetic-click-delivery --confirm-synthetic-click-probe`
- `verify-current-cursor-click --confirm-current-cursor-click`

The confirmation flag only means the user authorized that command path. It does
not by itself make the capability production-grade.

## Experimental Or Unproven Capabilities

ChatGPT navigation and synthetic input remain diagnostic/manual-only until they
are proven and explicitly promoted.

The following must not be treated as normal production loop behavior:

- frame-click verification,
- coordinate calibration,
- Calculator synthetic-click probe,
- current-cursor click probe,
- CoreGraphics scroll search,
- project/chat navigation,
- sidebar destination opening through geometry click,
- autonomous project chat opening through scroll/search.

Some of this functionality is used behind an operator-approved navigation flag
in the supervision handoff path. Even there, the authoritative proof remains the
read-only destination gate after navigation, not the navigator's own heuristic.

## Safety Rules

- Do not convert diagnostics into production behavior casually.
- Promotion requires an explicit task, characterization tests, safety review,
  and documentation update.
- Keep read-only inspection separate from action-posting code.
- Keep AXPress, CoreGraphics click, CoreGraphics scroll, AppleScript keypress,
  clipboard paste, and app activation behind explicit gates.
- Re-resolve UI targets immediately before an action.
- Fail closed on ambiguous targets, unstable UI, missing permissions, or
  unverifiable post-action state.
- Do not infer that a click or AXPress reached the intended ChatGPT destination
  unless fresh post-action evidence verifies it.
