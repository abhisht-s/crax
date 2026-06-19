# Agent GPT Codex Loop

Local supervised agent loop prototype.

## Setup

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Smoke Test

Initialize the SQLite ledger:

```sh
agent-loop init
```

Create a run:

```sh
agent-loop start "Test instruction"
```

Show the run using the printed run ID:

```sh
agent-loop show <run_id>
```

## Milestone 1B Smoke Test

Run a supervised shell command and inspect the recorded ledger events:

```sh
agent-loop init
RUN_ID=$(agent-loop start "Shell runner test")
agent-loop run-shell "$RUN_ID" -- echo hello
agent-loop show "$RUN_ID"
```

## Milestone 1C Smoke Test

Check the local Codex CLI installation and inspect the recorded ledger events:

```sh
agent-loop init
RUN_ID=$(agent-loop start "Codex check test")
agent-loop codex-check "$RUN_ID"
agent-loop show "$RUN_ID"
```

## Stage 2.4 Smoke Test

Run Codex in exec mode with explicit sandbox handling and inspect the recorded transcript events:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Codex sandbox test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: CODEX_READ_ONLY_OK"
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --sandbox workspace-write --prompt "Say exactly: CODEX_WORKSPACE_WRITE_OK"
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --sandbox danger-full-access --prompt "Should not run"
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --sandbox danger-full-access --confirm-full-access --prompt "Say exactly: CODEX_FULL_ACCESS_OK"
agent-loop show "$RUN_ID"
```

## Stage 3.1 Smoke Test

Capture a Git before snapshot before Codex exec and inspect the recorded ledger event:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Git before snapshot test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: GIT_BEFORE_OK"
agent-loop show "$RUN_ID"
```

Expected ledger event:

```text
git_snapshot_before_codex
```

## Stage 3.2 Smoke Test

Capture Git before and after snapshots around Codex exec and inspect the recorded ledger events:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Git after snapshot test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: GIT_AFTER_OK"
agent-loop show "$RUN_ID"
```

Expected ledger event:

```text
git_snapshot_after_codex
```

## Stage 3.3 Smoke Test

Classify changed files from the Git after snapshot and inspect the recorded ledger event:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Changed-file classifier test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: CLASSIFIER_OK"
agent-loop show "$RUN_ID"
```

Expected ledger event:

```text
changed_file_classification
```

## Stage 3.4 Smoke Test

Record prompt-intent versus repo-impact diagnostics after changed-file classification:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Prompt repo diagnostics test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: PROMPT_REPO_DIAGNOSTICS_OK"
agent-loop show "$RUN_ID"
```

Expected ledger event:

```text
prompt_repo_impact_diagnostics
```

## Stage 4.1 Smoke Test

Record a supervision decision after prompt/repo impact diagnostics:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Supervision decision test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: SUPERVISION_DECISION_OK"
agent-loop show "$RUN_ID"
```

Expected ledger event:

```text
supervision_decision
```

## Stage 4.2 Smoke Test

Update run status from the supervision decision and inspect the recorded transition event:

```sh
REPO_PATH="$(pwd)"
RUN_ID=$(agent-loop start "Run status transition test")
agent-loop codex-run "$RUN_ID" --repo "$REPO_PATH" --prompt "Say exactly: RUN_STATUS_TRANSITION_OK"
agent-loop show "$RUN_ID"
```

Expected:

```text
run_status_transition
```

Run status should be `completed` if no meaningful anomalies are detected, or `completed` with a `record_only` decision if only `repo_dirty_before_codex` exists.

## Stage 4.3 Smoke Test

Resolve flagged runs after human review:

```sh
agent-loop approve <run_id> --note "Reviewed scope issue, acceptable."
agent-loop reject <run_id> --note "Audit-only prompt modified files."
agent-loop complete-review <run_id> --note "Migration change reviewed."
```

Expected ledger events:

```text
human_approval
human_rejection
human_review_completed
```

## Stage 4.4 Smoke Test

Check whether a run can continue to a future automated step:

```sh
agent-loop can-continue <run_id>
```

`completed` and `approved` runs allow continuation. `needs_review`,
`waiting_for_approval`, `rejected`, and `failed` runs block continuation until
the required human or manual action is handled.

Expected ledger event:

```text
continuation_check
```

## Stage 5.1 Smoke Test

Generate the GPT feedback text for a completed Codex run:

```sh
agent-loop gpt-feedback <run_id>
agent-loop gpt-feedback <run_id> --output data/runs/<run_id>/gpt_feedback.md
```

This prepares the text that will later be pasted back into ChatGPT after Codex
finishes. It includes Codex's final captured output plus concise run metadata;
it does not include full changed code files or full diffs.

## Stage 5.2 Smoke Test

Copy the same GPT feedback text to the macOS clipboard:

```sh
agent-loop gpt-feedback <run_id> --copy
agent-loop gpt-feedback <run_id> --output data/runs/<run_id>/gpt_feedback.md --copy
```

`--copy` uses macOS `pbcopy`. It copies the generated GPT feedback message,
not source files or diffs.

## Stage 5.3 Smoke Test

Paste the same GPT feedback text into the currently focused macOS app/text
field:

```sh
agent-loop paste-feedback <run_id> --copy-first
agent-loop paste-feedback <run_id> --copy-first --output data/runs/<run_id>/gpt_feedback.md
```

This is macOS-only. It copies the generated GPT feedback message first, then
uses `osascript` / System Events to send Command-V to the frontmost app. The
correct ChatGPT chat and input box must already be focused.

`paste-feedback` only pastes. It does not press Enter, submit the message,
choose a chat, or navigate ChatGPT. macOS may require Accessibility permission
for Terminal, iTerm, or the Python process before System Events can paste.

## Stage 5.4 Smoke Test

Paste the same GPT feedback text into the currently focused macOS app/text
field and press Enter only after explicit confirmation:

```sh
agent-loop submit-feedback <run_id> --copy-first --confirm-submit
```

This is macOS-only. The correct ChatGPT chat and input box must already be
focused. The command copies the generated GPT feedback message, uses
`osascript` / System Events to send Command-V, then sends Enter.

`--confirm-submit` is required, and `--copy-first` is required. Without both
flags, no copy, paste, or Enter is sent. This is still not chat navigation,
project selection, response capture, Codex resume, or any LLM API call.

## Stage 5.5 Smoke Test

Bring the ChatGPT desktop app to the front and verify it is frontmost:

```sh
agent-loop activate-chatgpt
agent-loop activate-chatgpt --app-name ChatGPT
```

This is macOS-only. It activates the named desktop app with `osascript`, then
checks the frontmost app with System Events.

`activate-chatgpt` does not paste, press Enter, submit a message, focus the
message input, choose a chat, or navigate projects. It assumes the ChatGPT
desktop app is installed and the correct chat/project is already open. Browser
support is not implemented yet.

## Stage 5.6A Smoke Test

Activate the ChatGPT desktop app and run a minimal read-only UI diagnostic:

```sh
agent-loop inspect-chatgpt-ui
agent-loop inspect-chatgpt-ui --app-name ChatGPT
```

This is a read-only macOS diagnostic. It activates ChatGPT, verifies it is
frontmost, then uses `osascript` / System Events to attempt safe shallow window
inspection. It exits successfully if activation and frontmost verification
succeed, even when deeper accessibility details are unavailable.

`inspect-chatgpt-ui` does not paste, press Enter, submit a message, click,
focus the message input, choose a chat, or navigate projects. It reports
warnings when focused-element or text-input candidate inspection is unavailable.
macOS may require Accessibility permission for Terminal, iTerm, VS Code, or the
Python process before System Events can inspect ChatGPT.

## Stage 5.6B Smoke Test

Activate the ChatGPT desktop app, verify it is frontmost, copy a fixed harmless
marker, and paste it into the active ChatGPT input:

```sh
agent-loop test-chatgpt-target-paste --confirm-paste
agent-loop test-chatgpt-target-paste --app-name ChatGPT --confirm-paste
```

The pasted marker is:

```text
WATCH_TO_CODEX_STAGE_5_6B_TARGET_PASTE_TEST_DO_NOT_SUBMIT
```

`--confirm-paste` is required. Without it, no app activation, copy, paste, or
Enter is sent.

Manual validation:

1. Open ChatGPT desktop app.
2. Open the intended project/chat manually.
3. Click the message input manually.
4. Put Terminal/VS Code frontmost.
5. Run `agent-loop test-chatgpt-target-paste --confirm-paste`.
6. Confirm marker appears in input.
7. Confirm it was not submitted.
8. Delete marker manually.

`test-chatgpt-target-paste` does not press Enter, submit the message, navigate
projects/chats, inspect ChatGPT content, or use any LLM/API/browser automation.
It still depends on the correct ChatGPT chat and input being active before the
command runs.
