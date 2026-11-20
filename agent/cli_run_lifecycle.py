from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from agent.run_services import HumanDecision, HumanDecisionResult


def handle_init_command(args: argparse.Namespace, *, ledger: Any) -> None:
    ledger.init_db()
    print(f"Database initialized: {ledger.DB_PATH}")


def _requested_start_profile(args: argparse.Namespace) -> Any:
    """The run profile asked for by ``start`` flags, or None when no flag was given.

    Without flags ``start`` behaves as it always has: no profile is recorded and
    the run uses Codex. Raises ValueError for an invalid combination (for
    example a Codex model with ``--coding-agent claude_code``).
    """

    from agent.run_services import CODEX_DEFAULT_SELECTION, DEFAULT_CODING_AGENT, RunExecutionProfile

    coding_agent = getattr(args, "coding_agent", None)
    sandbox = getattr(args, "sandbox", None)
    model = getattr(args, "model", None)
    effort = getattr(args, "effort", None)
    if coding_agent is None and sandbox is None and model is None and effort is None:
        return None
    sandbox = sandbox or "read-only"
    return RunExecutionProfile(
        sandbox=sandbox,
        model=model or CODEX_DEFAULT_SELECTION,
        reasoning_effort=effort or CODEX_DEFAULT_SELECTION,
        approval_policy="never" if sandbox == "danger-full-access" else CODEX_DEFAULT_SELECTION,
        profile_source="explicit_user_selection",
        coding_agent=coding_agent or DEFAULT_CODING_AGENT,
    )


def handle_start_command(
    args: argparse.Namespace,
    *,
    ledger: Any,
    create_run_service: Callable[..., Any],
) -> None:
    try:
        profile = _requested_start_profile(args)
    except (TypeError, ValueError) as exc:
        # Checked before the run exists, so a bad flag never leaves an orphan run.
        print(f"error: invalid execution profile: {exc}", file=sys.stderr)
        raise SystemExit(2)
    result = create_run_service(args.instruction, ledger=ledger)
    if not result.ok:
        print(f"error: {result.error_message}", file=sys.stderr)
        raise SystemExit(1)
    if profile is not None:
        from agent.run_services import select_run_execution_profile

        selected = select_run_execution_profile(result.run_id, profile, ledger=ledger)
        if not selected.ok:
            print(
                f"error: run {result.run_id} was created but its execution profile was not recorded: "
                f"{selected.error_message or selected.reason_code}",
                file=sys.stderr,
            )
            raise SystemExit(1)
    print(result.run_id)


def handle_show_command(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
    ledger: Any,
    print_run: Callable[[dict, list[dict]], None],
) -> None:
    run = ledger.get_run(args.run_id)
    if run is None:
        parser.exit(1, f"Run not found: {args.run_id}\n")

    print_run(run, ledger.list_events(args.run_id))


def handle_can_continue_command(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
    ledger: Any,
    can_continue_run: Callable[[str], dict],
    continuation_check_message: Callable[[dict], str],
    print_continuation_check: Callable[[str, dict], None],
) -> None:
    run = ledger.get_run(args.run_id)
    if run is None:
        parser.exit(1, f"Run not found: {args.run_id}\n")

    result = can_continue_run(run["status"])
    ledger.add_event(
        args.run_id,
        "continuation_check",
        continuation_check_message(result),
        result,
    )
    print_continuation_check(args.run_id, result)
    raise SystemExit(0 if result["can_continue"] else 2)


def handle_human_decision_command(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
    decision: HumanDecision,
    resolve_human_decision: Callable[..., HumanDecisionResult],
    handle_human_decision_result: Callable[[argparse.ArgumentParser, HumanDecisionResult], None],
) -> None:
    handle_human_decision_result(
        parser,
        resolve_human_decision(
            args.run_id,
            decision,
            note=args.note,
        ),
    )
