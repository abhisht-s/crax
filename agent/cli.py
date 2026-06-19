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
from agent.file_classifier import classify_changed_files
from agent.git_snapshot import capture_git_snapshot
from agent.risk_policy import evaluate_supervision_decision
from agent.run_diagnostics import analyze_prompt_repo_impact
from agent.run_state import RunStatus
from agent.run_status_policy import status_from_supervision_decision
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

    approve_parser = subparsers.add_parser("approve", help="Approve a flagged run.")
    approve_parser.add_argument("run_id", help="Run ID to approve.")
    approve_parser.add_argument("--note", default="", help="Optional human approval note.")

    reject_parser = subparsers.add_parser("reject", help="Reject a flagged run.")
    reject_parser.add_argument("run_id", help="Run ID to reject.")
    reject_parser.add_argument("--note", default="", help="Optional human rejection note.")

    complete_review_parser = subparsers.add_parser(
        "complete-review",
        help="Mark a needs_review run as reviewed and completed.",
    )
    complete_review_parser.add_argument("run_id", help="Run ID to complete review for.")
    complete_review_parser.add_argument("--note", default="", help="Optional human review note.")

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


def _short_hash(value: str | None) -> str | None:
    return value[:12] if value else None


def _working_tree_dirty(snapshot: dict) -> bool:
    return bool(snapshot["status_short"].strip())


def _changed_files_count(snapshot: dict) -> int:
    return len([line for line in snapshot["diff_name_only"].splitlines() if line.strip()])


def _changed_file_paths(snapshot: dict) -> list[str]:
    diff_name_only = snapshot.get("diff_name_only") or ""
    return [line.strip() for line in diff_name_only.splitlines() if line.strip()]


def _snapshot_message(snapshot: dict) -> str:
    branch = snapshot["branch"] or "None"
    head = _short_hash(snapshot["head"]) or "None"
    dirty = str(_working_tree_dirty(snapshot)).lower()
    return (
        f"repo_path={snapshot['repo_path']} branch={branch} "
        f"head={head} dirty={dirty}"
    )


def _print_git_snapshot_summary(snapshot: dict, label: str) -> None:
    print(f"Git {label} snapshot:")
    print(f"repo_path: {snapshot['repo_path']}")
    print(f"is_git_repo: {str(snapshot['is_git_repo']).lower()}")
    print(f"branch: {snapshot['branch'] or ''}")
    print(f"head: {_short_hash(snapshot['head']) or ''}")
    print(f"dirty: {str(_working_tree_dirty(snapshot)).lower()}")
    print(f"changed_files_count: {_changed_files_count(snapshot)}")
    if snapshot["validation_error"]:
        print(f"validation_error: {snapshot['validation_error']}")
    sys.stdout.flush()


def _classification_message(classification: dict) -> str:
    return (
        f"total_files={classification['total_files']} "
        f"category_counts={classification['counts_by_category']} "
        f"risk_counts={classification['counts_by_risk_level']} "
        f"high_risk_file_count={len(classification['high_risk_files'])}"
    )


def _print_changed_file_classification(classification: dict) -> None:
    print("Changed-file classification:")
    print(f"total_files: {classification['total_files']}")
    print(f"counts_by_category: {classification['counts_by_category']}")
    print(f"counts_by_risk_level: {classification['counts_by_risk_level']}")
    print(f"high_risk_files: {classification['high_risk_files']}")
    sys.stdout.flush()


def _diagnostics_message(diagnostics: dict | None) -> str:
    if diagnostics is None:
        return "diagnostics_unavailable"
    return (
        f"outcome={diagnostics['outcome']} "
        f"attention_level={diagnostics['attention_level']} "
        f"flags={diagnostics['flags']}"
    )


def _print_prompt_repo_impact_diagnostics(diagnostics: dict | None) -> None:
    print("Prompt/repo impact diagnostics:")
    if diagnostics is None:
        print("unavailable")
        sys.stdout.flush()
        return
    print(f"outcome: {diagnostics['outcome']}")
    print(f"attention_level: {diagnostics['attention_level']}")
    print(f"prompt_intents: {diagnostics['prompt_intents']}")
    print(f"flags: {diagnostics['flags']}")
    print(f"messages: {diagnostics['messages']}")
    sys.stdout.flush()


def _supervision_decision_message(decision: dict) -> str:
    return (
        f"decision={decision['decision']} "
        f"attention_level={decision['attention_level']} "
        f"approval_required={decision['approval_required']} "
        f"reasons={decision['reasons']}"
    )


def _print_supervision_decision(decision: dict) -> None:
    print("Supervision decision:")
    print(f"decision: {decision['decision']}")
    print(f"attention_level: {decision['attention_level']}")
    print(f"approval_required: {decision['approval_required']}")
    print(f"needs_review: {decision['needs_review']}")
    print(f"reasons: {decision['reasons']}")
    print(f"messages: {decision['messages']}")
    sys.stdout.flush()


def _run_status_transition_message(transition: dict) -> str:
    return (
        f"previous_status={transition['previous_status']} "
        f"next_status={transition['next_status']} "
        f"reason={transition['reason']}"
    )


def _print_run_status_transition(transition: dict) -> None:
    print("Run status transition:")
    print(f"previous_status: {transition['previous_status']}")
    print(f"next_status: {transition['next_status']}")
    print(f"reason: {transition['reason']}")
    print(f"should_auto_complete: {transition['should_auto_complete']}")
    sys.stdout.flush()


def _print_human_decision(previous_status: str, next_status: str, note: str) -> None:
    print(f"previous_status: {previous_status}")
    print(f"next_status: {next_status}")
    print(f"note: {note}")
    sys.stdout.flush()


def _resolve_flagged_run(
    run_id: str,
    run: dict,
    note: str,
    allowed_statuses: set[str],
    next_status: RunStatus,
    allowed_event_type: str,
    allowed_message: str,
    rejected_event_type: str,
    action_label: str,
) -> None:
    current_status = run["status"]
    if current_status not in allowed_statuses:
        allowed_statuses_text = ", ".join(sorted(allowed_statuses))
        message = (
            f"Cannot {action_label} run from current status "
            f"{current_status!r}. Allowed statuses: {allowed_statuses_text}."
        )
        ledger.add_event(
            run_id,
            rejected_event_type,
            message,
            {
                "current_status": current_status,
                "note": note,
            },
        )
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)

    previous_status = current_status
    ledger.update_run_status(
        run_id,
        next_status,
        final_summary=run["final_summary"],
        error=run["error"],
    )
    ledger.add_event(
        run_id,
        allowed_event_type,
        allowed_message,
        {
            "previous_status": previous_status,
            "next_status": next_status.value,
            "note": note,
        },
    )
    _print_human_decision(previous_status, next_status.value, note)


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

    if args.command == "approve":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
            RunStatus.APPROVED,
            "human_approval",
            "Run approved by user.",
            "human_approval_rejected_by_state",
            "approve",
        )
        return

    if args.command == "reject":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
            RunStatus.REJECTED,
            "human_rejection",
            "Run rejected by user.",
            "human_rejection_rejected_by_state",
            "reject",
        )
        return

    if args.command == "complete-review":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.NEEDS_REVIEW.value},
            RunStatus.COMPLETED,
            "human_review_completed",
            "Run review completed by user.",
            "human_review_completion_rejected_by_state",
            "complete review for",
        )
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

        git_snapshot = capture_git_snapshot(repo_path_text)
        after_git_snapshot = None
        changed_file_classification = None
        ledger.add_event(
            args.run_id,
            "git_snapshot_before_codex",
            _snapshot_message(git_snapshot),
            git_snapshot,
        )
        _print_git_snapshot_summary(git_snapshot, "before")

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

        if not result["validation_error"]:
            after_git_snapshot = capture_git_snapshot(repo_path_text)
            ledger.add_event(
                args.run_id,
                "git_snapshot_after_codex",
                _snapshot_message(after_git_snapshot),
                after_git_snapshot,
            )
            _print_git_snapshot_summary(after_git_snapshot, "after")

            changed_file_classification = classify_changed_files(
                _changed_file_paths(after_git_snapshot)
            )
            ledger.add_event(
                args.run_id,
                "changed_file_classification",
                _classification_message(changed_file_classification),
                changed_file_classification,
            )
            _print_changed_file_classification(changed_file_classification)

        try:
            prompt_repo_impact_diagnostics = analyze_prompt_repo_impact(
                args.prompt,
                result,
                git_snapshot,
                after_git_snapshot,
                changed_file_classification,
            )
        except Exception as exc:
            prompt_repo_impact_diagnostics = None
            print(f"warning: prompt/repo impact diagnostics unavailable: {exc}", file=sys.stderr)
        ledger.add_event(
            args.run_id,
            "prompt_repo_impact_diagnostics",
            _diagnostics_message(prompt_repo_impact_diagnostics),
            prompt_repo_impact_diagnostics,
        )
        _print_prompt_repo_impact_diagnostics(prompt_repo_impact_diagnostics)

        supervision_decision = evaluate_supervision_decision(prompt_repo_impact_diagnostics)
        ledger.add_event(
            args.run_id,
            "supervision_decision",
            _supervision_decision_message(supervision_decision),
            supervision_decision,
        )
        _print_supervision_decision(supervision_decision)

        transition = status_from_supervision_decision(supervision_decision, result)
        transition = {
            **transition,
            "previous_status": run["status"],
            "next_status": transition["next_status"],
        }
        ledger.update_run_status(args.run_id, RunStatus(transition["next_status"]))
        ledger.add_event(
            args.run_id,
            "run_status_transition",
            _run_status_transition_message(transition),
            transition,
        )
        _print_run_status_transition(transition)

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
