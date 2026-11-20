from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.codex_invocation import (
    EXIT_FILENAME,
    FINAL_MESSAGE_FILENAME,
    IDENTITY_FILENAME,
    STDERR_FILENAME,
    STDOUT_FILENAME,
    capture_process_identity,
    read_json_file,
    write_json_atomic,
)


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Durable per-invocation Codex wrapper.")
    parser.add_argument("--manifest", required=True, help="Path to intent.json")
    return parser.parse_args(argv)


def _install_child_signal_handler(child_holder: dict[str, subprocess.Popen | None]) -> None:
    def handler(signum: int, _frame: Any) -> None:
        child = child_holder.get("process")
        if child is None or child.poll() is not None:
            return
        try:
            os.kill(child.pid, signum)
        except OSError:
            try:
                child.terminate()
            except OSError:
                return

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _write_exit(path: Path, invocation_id: str, exit_code: int | None, extra: dict[str, Any] | None = None) -> None:
    signaled = isinstance(exit_code, int) and exit_code < 0
    payload = {
        "codex_invocation_id": invocation_id,
        "exit_code": exit_code,
        "wait_status": exit_code,
        "signaled": signaled,
        "signal": (-exit_code) if signaled and exit_code is not None else None,
        "finished_at": _utc_now(),
    }
    if extra:
        payload.update(extra)
    write_json_atomic(path, payload)


def _extract_final_message(intent: dict[str, Any], stdout_path: Path, artifact_dir: Path) -> dict[str, Any] | None:
    """For agents with no final-message file (Claude Code), copy it out of the stream.

    Runs before exit.json is written, so the final message is on disk whenever
    the exit is, even if the controller restarted mid-run. Codex intents carry
    no ``final_message_source`` and are untouched.
    """

    source = intent.get("final_message_source")
    if not source:
        return None
    try:
        from agent.claude_code_stream import FINAL_MESSAGE_SOURCE_CLAUDE_STREAM, write_final_message_from_stream

        if source != FINAL_MESSAGE_SOURCE_CLAUDE_STREAM:
            return {"final_message_extraction": "unknown_source", "final_message_source": str(source)}
        final_path = Path(str(intent.get("final_message_path") or artifact_dir / FINAL_MESSAGE_FILENAME))
        status = write_final_message_from_stream(stdout_path, final_path)
    except Exception as exc:  # never let extraction lose the exit record
        return {"final_message_extraction": "extraction_error", "final_message_extraction_error": type(exc).__name__}
    return {"final_message_extraction": status, "final_message_source": source}


def run_wrapper(manifest_path: Path) -> int:
    intent = read_json_file(manifest_path)
    if intent is None:
        return 2
    invocation_id = str(intent.get("codex_invocation_id") or "")
    artifact_dir = Path(str(intent.get("artifact_dir") or manifest_path.parent))
    command = intent.get("command")
    if not isinstance(command, list) or not command:
        return 2
    cwd = str(intent.get("cwd") or "") or None
    stdout_path = Path(str(intent.get("stdout_path") or artifact_dir / STDOUT_FILENAME))
    stderr_path = Path(str(intent.get("stderr_path") or artifact_dir / STDERR_FILENAME))
    identity_path = Path(str(intent.get("identity_path") or artifact_dir / IDENTITY_FILENAME))
    exit_path = Path(str(intent.get("exit_path") or artifact_dir / EXIT_FILENAME))

    artifact_dir.mkdir(parents=True, exist_ok=True)
    identity = capture_process_identity(invocation_id)
    write_json_atomic(identity_path, identity)

    child_holder: dict[str, subprocess.Popen | None] = {"process": None}
    _install_child_signal_handler(child_holder)
    argv = [str(item) for item in command]
    try:
        with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            child_holder["process"] = child
            exit_code = child.wait()
    except FileNotFoundError:
        _write_exit(exit_path, invocation_id, 127)
        return 127
    except OSError:
        _write_exit(exit_path, invocation_id, 1)
        return 1
    _write_exit(exit_path, invocation_id, exit_code, _extract_final_message(intent, stdout_path, artifact_dir))
    return 0 if exit_code == 0 else int(exit_code or 1)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_wrapper(Path(args.manifest).expanduser())


if __name__ == "__main__":
    sys.exit(main())
