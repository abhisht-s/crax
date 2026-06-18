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
