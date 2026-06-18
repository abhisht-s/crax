from __future__ import annotations

import argparse
import shutil
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.codex_terminal import (
    ALLOWED_CODEX_SANDBOXES,
    check_codex_environment,
    run_codex_exec,
    run_command,
)
from agent import ledger


DEFAULT_SHELL_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 300


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the local run ledger database.")

    start_parser = subparsers.add_parser("start", help="Create a new run.")
    start_parser.add_argument("instruction", help="User instruction for the run.")

    show_parser = subparsers.add_parser("show", help="Show a run and its events.")
    show_parser.add_argument("run_id", help="Run ID to show.")

    codex_check_parser = subparsers.add_parser(
        "codex-check",
        help="Check local Codex CLI availability without running prompts.",
    )
    codex_check_parser.add_argument("run_id", help="Run ID for this Codex check.")

    codex_run_parser = subparsers.add_parser(
        "codex-run",
        help="Run Codex exec and record the transcript.",
    )
    codex_run_parser.add_argument("run_id", help="Run ID for this Codex exec.")
    codex_run_parser.add_argument("--prompt", required=True, help="Prompt to pass to Codex exec.")
    codex_run_parser.add_argument("--repo", help="Repository/workdir for Codex exec. Default: current directory.")
    codex_run_parser.add_argument("--cwd", help=argparse.SUPPRESS)
    codex_run_parser.add_argument(
        "--sandbox",
        default="read-only",
        help="Codex sandbox mode: read-only, workspace-write, or danger-full-access. Default: read-only.",
    )
    codex_run_parser.add_argument(
        "--confirm-full-access",
        action="store_true",
        help="Required with --sandbox danger-full-access.",
    )
    codex_run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
        help=f"Timeout in seconds. Default: {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}.",
    )

    run_shell_parser = subparsers.add_parser(
        "run-shell",
        help="Run a non-interactive shell command and record the transcript.",
    )
    run_shell_parser.add_argument("run_id", help="Run ID for this shell command.")
    run_shell_parser.add_argument(
        "shell_command",
        nargs=argparse.REMAINDER,
        help="Command to execute, usually after --.",
    )

    return parser


def _print_run(run: dict, events: list[dict]) -> None:
    print("Run")
    print(f"  id: {run['id']}")
    print(f"  status: {run['status']}")
    print(f"  created_at: {run['created_at']}")
    print(f"  updated_at: {run['updated_at']}")
    print(f"  user_instruction: {run['user_instruction']}")
    print(f"  final_summary: {run['final_summary'] or ''}")
    print(f"  error: {run['error'] or ''}")
    print()
    print("Events")

    if not events:
        print("  (none)")
        return

    for event in events:
        print(f"  [{event['id']}] {event['created_at']} {event['event_type']}")
        print(f"      message: {event['message']}")
        if event["metadata_json"]:
            print(f"      metadata: {event['metadata_json']}")


def _normalize_shell_command(raw_command: list[str]) -> list[str]:
    if raw_command and raw_command[0] in {"--", "–"}:
        return raw_command[1:]
    return raw_command


def _format_command(command: list[str]) -> str:
    return shlex.join(command)


def _print_shell_result(result: dict) -> None:
    print("stdout:")
    print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
    print("stderr:", file=sys.stderr)
    print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n", file=sys.stderr)
    print(f"exit_code: {result['exit_code']}")
    print(f"timed_out: {result['timed_out']}")


def _first_lines(value: str, count: int = 8) -> str:
    return "\n".join(value.splitlines()[:count])


def _command_output(result: dict | None) -> str:
    if result is None:
        return ""
    return result["stdout"] or result["stderr"]


def _print_codex_check_result(result: dict) -> None:
    print(f"found: {result['found']}")
    print(f"codex_path: {result['codex_path'] or ''}")

    print("help first lines:")
    help_lines = _first_lines(_command_output(result["help"]))
    print(help_lines if help_lines else "  (none)")

    if result["doctor"] is not None:
        doctor = result["doctor"]
        print("doctor:")
        print(f"  exit_code: {doctor['exit_code']}")
        print(f"  timed_out: {doctor['timed_out']}")
        doctor_lines = _first_lines(_command_output(doctor))
        print("  output first lines:")
        if doctor_lines:
            for line in doctor_lines.splitlines():
                print(f"    {line}")
        else:
            print("    (none)")


def _print_codex_exec_result(result: dict) -> None:
    print(f"repo_path: {result['repo_path']}")
    print(f"sandbox: {result['sandbox']}")
    print(f"found: {result['found']}")
    print(f"codex_path: {result['codex_path'] or ''}")
    print(f"exit_code: {result['exit_code']}")
    print(f"timed_out: {result['timed_out']}")
    if result["validation_error"]:
        print(f"validation_error: {result['validation_error']}")
    print("stdout:")
    print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")

    if result["stderr"]:
        print("stderr:", file=sys.stderr)
        print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n", file=sys.stderr)


def _codex_exec_validation_result(
    prompt: str,
    repo_path: str,
    sandbox: str,
    validation_error: str,
) -> dict:
    now = datetime.now(UTC).isoformat()
    codex_path = shutil.which("codex")
    return {
        "mode": "exec",
        "found": codex_path is not None,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "exit_code": 2,
        "stdout": "",
        "stderr": f"{validation_error}\n",
        "timed_out": False,
        "started_at": now,
        "finished_at": now,
        "validation_error": validation_error,
    }


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        ledger.init_db()
        print(f"Database initialized: {ledger.DB_PATH}")
        return

    if args.command == "start":
        run_id = ledger.create_run(args.instruction)
        ledger.add_event(run_id, "run_created", "Run created.")
        print(run_id)
        return

    if args.command == "show":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _print_run(run, ledger.list_events(args.run_id))
        return

    if args.command == "run-shell":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        command = _normalize_shell_command(args.shell_command)
        if not command:
            parser.exit(2, "Missing shell command. Usage: agent-loop run-shell <run_id> -- <command...>\n")

        cwd = None
        timeout_seconds = DEFAULT_SHELL_TIMEOUT_SECONDS
        ledger.add_event(
            args.run_id,
            "shell_command_started",
            _format_command(command),
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
        _print_shell_result(result)

        if result["timed_out"]:
            raise SystemExit(124)
        raise SystemExit(result["exit_code"] or 0)

    if args.command == "codex-run":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        requested_repo_path = args.repo if args.repo is not None else args.cwd
        if requested_repo_path:
            repo_path = Path(requested_repo_path).expanduser().resolve(strict=False)
        else:
            repo_path = Path.cwd().resolve()
        repo_path_text = str(repo_path)
        sandbox = args.sandbox

        ledger.add_event(
            args.run_id,
            "codex_exec_started",
            "Running Codex exec.",
            {
                "prompt": args.prompt,
                "repo_path": repo_path_text,
                "timeout": args.timeout,
                "sandbox": sandbox,
            },
        )

        validation_error = None
        if sandbox not in ALLOWED_CODEX_SANDBOXES:
            validation_error = (
                "Invalid Codex sandbox. Allowed values: "
                f"{', '.join(ALLOWED_CODEX_SANDBOXES)}."
            )
        elif sandbox == "danger-full-access" and not args.confirm_full_access:
            validation_error = "Codex sandbox danger-full-access requires --confirm-full-access."

        if validation_error is None:
            result = run_codex_exec(
                args.prompt,
                repo_path=repo_path_text,
                timeout_seconds=args.timeout,
                sandbox=sandbox,
            )
        else:
            result = _codex_exec_validation_result(
                args.prompt,
                repo_path=repo_path_text,
                sandbox=sandbox,
                validation_error=validation_error,
            )

        validation_message = (
            f" validation_error={result['validation_error']}"
            if result["validation_error"]
            else ""
        )
        ledger.add_event(
            args.run_id,
            "codex_exec_finished",
            (
                f"found={result['found']} exit_code={result['exit_code']} "
                f"timed_out={result['timed_out']} repo_path={result['repo_path']} "
                f"sandbox={result['sandbox']}{validation_message}"
            ),
            result,
        )
        if result["validation_error"]:
            print(f"error: {result['validation_error']}", file=sys.stderr)
        _print_codex_exec_result(result)

        if result["validation_error"]:
            raise SystemExit(2)
        if not result["found"]:
            raise SystemExit(1)
        if result["timed_out"]:
            raise SystemExit(124)
        raise SystemExit(result["exit_code"] or 0)

    if args.command == "codex-check":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        timeout_seconds = DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS
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
        _print_codex_check_result(result)

        raise SystemExit(0 if result["found"] else 1)


if __name__ == "__main__":
    main()
