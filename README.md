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

## Stage 5.7 Smoke Test

Generate the real GPT feedback text for a run, copy it, activate the ChatGPT
desktop app, verify ChatGPT is frontmost, and paste into the active ChatGPT
input:

```sh
agent-loop paste-feedback-to-chatgpt <run_id> --confirm-paste
agent-loop paste-feedback-to-chatgpt <run_id> --app-name ChatGPT --confirm-paste
agent-loop paste-feedback-to-chatgpt <run_id> --output data/runs/<run_id>/gpt_feedback.md --confirm-paste
```

`--confirm-paste` is required. Without it, no feedback is generated, copied,
pasted, submitted, or sent.

`paste-feedback` pastes to the current frontmost focused app/text field.
`paste-feedback-to-chatgpt` activates ChatGPT first, verifies ChatGPT is
frontmost, then pastes. Both commands only paste; they do not press Enter or
submit the message.

Manual validation:

1. Create or reuse a run with GPT feedback available.
2. Open ChatGPT desktop app.
3. Open the intended project/chat manually.
4. Click the message input manually.
5. Put Terminal/VS Code frontmost.
6. Run `agent-loop paste-feedback-to-chatgpt <run_id> --confirm-paste`.
7. Confirm feedback appears in ChatGPT input.
8. Confirm it was not submitted.
9. Delete it manually or submit manually if appropriate.

`paste-feedback-to-chatgpt` does not press Enter, submit the message, navigate
projects/chats, inspect or scrape ChatGPT content, or use any LLM/API/browser
automation. It still depends on the correct ChatGPT chat and input already being
open and active before the command runs.

## Stage 5.8 Smoke Test

Generate the real GPT feedback text for a run, copy it, activate the ChatGPT
desktop app, verify ChatGPT is frontmost, paste into the active ChatGPT input,
and submit by pressing Enter only after explicit confirmation:

```sh
agent-loop submit-feedback-to-chatgpt <run_id> --confirm-submit
agent-loop submit-feedback-to-chatgpt <run_id> --app-name ChatGPT --confirm-submit
agent-loop submit-feedback-to-chatgpt <run_id> --output data/runs/<run_id>/gpt_feedback.md --confirm-submit
```

`--confirm-submit` is required. Without it, no feedback is generated, copied,
pasted, submitted, or sent.

`paste-feedback-to-chatgpt` activates ChatGPT and pastes the feedback, but does
not submit or press Enter. `submit-feedback-to-chatgpt` activates ChatGPT,
pastes the feedback, and presses Enter only when `--confirm-submit` is present.

Manual validation:

1. Create or reuse a safe run with GPT feedback available.
2. Open ChatGPT desktop app.
3. Open the intended project/chat manually.
4. Click the message input manually.
5. Put Terminal/VS Code frontmost.
6. Run `agent-loop submit-feedback-to-chatgpt <run_id> --confirm-submit`.
7. Confirm feedback appears and is submitted.
8. Confirm the ledger shows `gpt_feedback_submitted`.

`submit-feedback-to-chatgpt` does not navigate projects/chats, inspect or scrape
ChatGPT content, use browser automation, or use any LLM/API automation. It still
depends on the correct ChatGPT chat and input already being open and active
before the command runs.

## Stage 5.9B Smoke Test

Capture ChatGPT's visible assistant response from the desktop app through
macOS Accessibility after feedback was submitted:

```sh
agent-loop capture-gpt-response-from-chatgpt-ax <run_id> --confirm-capture
agent-loop capture-gpt-response-from-chatgpt-ax <run_id> --app-name ChatGPT --confirm-capture
```

`--confirm-capture` is required. Without it, no ChatGPT activation, AX
inspection, ledger write, clipboard access, paste, Enter, submit, or send action
is performed.

This command activates the ChatGPT desktop app, verifies it is frontmost, reads
the focused window's Accessibility tree, matches the previously submitted GPT
feedback from the ledger, waits for the following assistant response text to be
stable, and records `gpt_response_captured`.

It does not click, paste, submit, press Enter, scroll, navigate projects/chats,
inspect browser ChatGPT, or use any LLM/API automation. The correct ChatGPT
project/chat must still be open manually, and the submitted feedback plus the
assistant response must be visible in the current window. This is v0.1 behavior:
it aborts instead of guessing if it cannot match the submitted feedback or
identify the following assistant response.

The captured text is rendered macOS Accessibility text. Markdown and code block
formatting may be lossy compared with the original ChatGPT message.

Manual validation:

1. Submit feedback using `agent-loop submit-feedback-to-chatgpt <run_id> --confirm-submit`.
2. Wait for ChatGPT's response to finish.
3. Run `agent-loop capture-gpt-response-from-chatgpt-ax <run_id> --confirm-capture`.
4. Confirm output says `matched_feedback: true`, `stable: true`, and `ledger_event: gpt_response_captured`.
5. Run `agent-loop show <run_id>` and confirm `gpt_response_captured` exists.

## Stage 5.10B Smoke Test

Extract and preview a candidate next Codex prompt from the latest valid
captured GPT response without running Codex:

```sh
agent-loop extract-next-codex-prompt <run_id>
agent-loop extract-next-codex-prompt <run_id> --confirm-extract
agent-loop extract-next-codex-prompt <run_id> --confirm-extract --output data/runs/<run_id>/next_codex_prompt.md
```

Without `--confirm-extract`, this command is read-only. It reads the ledger,
selects a valid captured GPT response, extracts a candidate prompt if the
response uses strict prompt markers, prints a bounded preview, and does not
write a ledger event or output file.

With `--confirm-extract`, it writes the extracted prompt to
`data/runs/<run_id>/next_codex_prompt.md` by default, or to `--output` when
provided, and records `next_codex_prompt_extracted`.

This command never runs Codex, pastes, submits, presses Enter, clicks, scrolls,
interacts with ChatGPT, performs ChatGPT AX capture, or uses an LLM/API. Every
extracted prompt is recorded as `requires_human_review` and is preview-only
until a later explicit human-confirmed execution step.

Prompt extraction prefers this sentinel contract:

```text
BEGIN_NEXT_CODEX_PROMPT
...
END_NEXT_CODEX_PROMPT
```

If GPT wants a next Codex step, it should include exactly one sentinel block. If
no Codex step should run, it should omit the markers.

If no sentinel block exists, the extractor accepts exactly one triple-backtick
fenced code block only when it is introduced by one of these labels:

```text
Codex prompt:
Next Codex prompt:
Prompt for Codex:
Use this Codex prompt:
```

The command rejects prose-only responses, unlabeled code blocks, empty prompt
blocks, malformed sentinel markers, multiple sentinel blocks, multiple labeled
fenced prompt blocks, SHA mismatches, and stale captures.

Stale-capture protection:

1. The latest successful `gpt_feedback_submitted` event is selected.
2. Only `gpt_response_captured` events whose `matched_submission_event_id`
   matches that latest submission are eligible.
3. The captured response must contain non-empty `response_text`, contain
   `response_sha256`, and match the SHA-256 of the response text.
