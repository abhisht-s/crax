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

## Stage 2.2 Smoke Test

Run Codex in exec mode and inspect the recorded transcript events:

```sh
RUN_ID=$(agent-loop start "Codex exec test")
agent-loop codex-run "$RUN_ID" --prompt "Say exactly: CODEX_RUN_OK"
agent-loop show "$RUN_ID"
```
