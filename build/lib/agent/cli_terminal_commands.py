from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any


def handle_run_shell_command(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
    ledger: Any,
    run_command: Callable[..., dict],
    format_command: Callable[[list[str]], str],
    print_shell_result: Callable[[dict], None],
    normalize_shell_command: Callable[[list[str]], list[str]],
    timeout_seconds: int,
) -> None:
    run = ledger.get_run(args.run_id)
    if run is None:
        parser.exit(1, f"Run not found: {args.run_id}\n")

    command = normalize_shell_command(args.shell_command)
    if not command:
        parser.exit(2, "Missing shell command. Usage: agent-loop run-shell <run_id> -- <command...>\n")

    cwd = None
    ledger.add_event(
        args.run_id,
        "shell_command_started",
        format_command(command),
        {
            "command": command,
            "cwd": cwd,
            "timeout": timeout_seconds,
        },
    )

    result = run_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
    ledger.add_event(
        args.run_id,
        "shell_command_finished",
        f"exit_code={result['exit_code']} timed_out={result['timed_out']}",
        result,
    )
    print_shell_result(result)

    if result["timed_out"]:
        raise SystemExit(124)
    raise SystemExit(result["exit_code"] or 0)


def handle_codex_check_command(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
    ledger: Any,
    check_codex_environment: Callable[..., dict],
    print_codex_check_result: Callable[[dict], None],
    timeout_seconds: int | None,
) -> None:
    run = ledger.get_run(args.run_id)
    if run is None:
        parser.exit(1, f"Run not found: {args.run_id}\n")

    ledger.add_event(
        args.run_id,
        "codex_check_started",
        "Checking local Codex CLI availability.",
        {"timeout": timeout_seconds},
    )

    result = check_codex_environment(timeout_seconds=timeout_seconds)
    if result["found"]:
        message = f"found=True codex_path={result['codex_path']}"
    else:
        message = "found=False"

    ledger.add_event(
        args.run_id,
        "codex_check_finished",
        message,
        result,
    )
    print_codex_check_result(result)

    raise SystemExit(0 if result["found"] else 1)
