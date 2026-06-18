from __future__ import annotations

import argparse

from agent import ledger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the local run ledger database.")

    start_parser = subparsers.add_parser("start", help="Create a new run.")
    start_parser.add_argument("instruction", help="User instruction for the run.")

    show_parser = subparsers.add_parser("show", help="Show a run and its events.")
    show_parser.add_argument("run_id", help="Run ID to show.")

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


if __name__ == "__main__":
    main()
