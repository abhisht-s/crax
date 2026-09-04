from __future__ import annotations

import shlex
import sys


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


def _print_manual_stale_lease_release_result(result) -> None:
    print("Manual ChatGPT UI lease release")
    print(f"  status: {result.status}")
    print(f"  event_written: {str(bool(result.event_written)).lower()}")
    if result.event_id is not None:
        print(f"  event_id: {result.event_id}")
    if result.run_id:
        print(f"  run_id: {result.run_id}")
    if result.run_status:
        print(f"  run_status: {result.run_status}")
    if result.active_event_id is not None:
        print(f"  active_event_id: {result.active_event_id}")
    if result.owning_run_id:
        print(f"  owning_run_id: {result.owning_run_id}")
    if result.owner_pid is not None:
        print(f"  owner_pid: {result.owner_pid}")
    if result.acquired_at:
        print(f"  acquired_at: {result.acquired_at}")
    if result.released_at:
        print(f"  released_at: {result.released_at}")
    if result.reason_code:
        print(f"  reason_code: {result.reason_code}")
    if result.error_message:
        print(f"  error: {result.error_message}")


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


def _print_changed_file_classification(classification: dict) -> None:
    print("Changed-file classification:")
    print(f"total_files: {classification['total_files']}")
    print(f"counts_by_category: {classification['counts_by_category']}")
    print(f"counts_by_risk_level: {classification['counts_by_risk_level']}")
    print(f"high_risk_files: {classification['high_risk_files']}")
    sys.stdout.flush()


def _print_governance_observation(observation: dict) -> None:
    print("Run governance observation:")
    print(f"prompt_contract_confidence: {observation['prompt_contract_confidence']}")
    print(f"scope_observation: {observation['scope_observation']}")
    print(f"attributable_changed_files: {observation['attributable_changed_files']}")
    print(f"preexisting_changed_files: {observation['preexisting_changed_files']}")
    print(f"preexisting_untracked_files: {observation['preexisting_untracked_files']}")
    print(f"contract_mismatches: {observation['contract_mismatches']}")
    print(f"objective_failures: {observation['objective_failures']}")
    sys.stdout.flush()


def _print_workspace_write_human_required(post_run_policy: dict) -> None:
    print("Stopped: Codex completed, but an objective workspace-write post-run failure was detected.")
    print(f"Reason: {post_run_policy.get('reason_code')}")
    print("No ChatGPT submission or further Codex execution was performed.")
    sys.stdout.flush()


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


def _print_supervision_decision(decision: dict) -> None:
    print("Supervision decision:")
    print(f"decision: {decision['decision']}")
    print(f"attention_level: {decision['attention_level']}")
    print(f"approval_required: {decision['approval_required']}")
    print(f"needs_review: {decision['needs_review']}")
    print(f"reasons: {decision['reasons']}")
    print(f"messages: {decision['messages']}")
    sys.stdout.flush()


def _print_run_status_transition(transition: dict) -> None:
    print("Run status transition:")
    print(f"previous_status: {transition['previous_status']}")
    print(f"next_status: {transition['next_status']}")
    print(f"reason: {transition['reason']}")
    print(f"should_auto_complete: {transition['should_auto_complete']}")
    sys.stdout.flush()


def _continuation_check_message(result: dict) -> str:
    return (
        f"can_continue={result['can_continue']} "
        f"status={result['status']} "
        f"reason={result['reason']}"
    )


def _print_continuation_check(run_id: str, result: dict) -> None:
    print(f"run_id: {run_id}")
    print(f"status: {result['status']}")
    print(f"can_continue: {result['can_continue']}")
    print(f"reason: {result['reason']}")
    print(f"required_action: {result['required_action'] or ''}")
    sys.stdout.flush()
