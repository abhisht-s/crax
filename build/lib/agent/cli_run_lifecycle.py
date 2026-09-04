from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from agent.run_services import HumanDecision, HumanDecisionResult


def handle_init_command(args: argparse.Namespace, *, ledger: Any) -> None:
    ledger.init_db()
    print(f"Database initialized: {ledger.DB_PATH}")


def handle_start_command(
    args: argparse.Namespace,
    *,
    ledger: Any,
    create_run_service: Callable[..., Any],
) -> None:
    result = create_run_service(args.instruction, ledger=ledger)
    if not result.ok:
        print(f"error: {result.error_message}", file=sys.stderr)
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
