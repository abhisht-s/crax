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
