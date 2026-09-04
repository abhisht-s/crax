from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

INTENT_FILENAME = "intent.json"
IDENTITY_FILENAME = "identity.json"
STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"
FINAL_MESSAGE_FILENAME = "final_message.md"
EXIT_FILENAME = "exit.json"
CANCEL_FILENAME = "cancel_requested.json"
PROGRESS_FAILURE_FILENAME = "progress_persistence_failed.json"

STATUS_NEVER_SPAWNED = "never_spawned"
STATUS_LIVE = "live"
STATUS_COMPLETE = "complete"
STATUS_UNCERTAIN = "uncertain"
STATUS_ALREADY_FINISHED = "already_finished"

TERMINATION_EXITED = "exited"
TERMINATION_OPERATOR_CANCELLED = "operator_cancelled"
TERMINATION_SIGNALED = "signaled"
TERMINATION_UNCERTAIN = "uncertain"

WRAPPER_MODULE = "agent.codex_invocation_wrapper"
_TAIL_POLL_SECONDS = 0.05
_EXIT_GRACE_POLLS = 40


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_run_id(value: str | None) -> str:
    text = str(value or "unscoped-run").strip() or "unscoped-run"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)
    return safe[:80] or "unscoped-run"


def default_runs_root(base: str | Path | None = None) -> Path:
    if base is None:
        return Path("data") / "runs"
    return Path(base)


def invocation_artifact_dir(
    run_id: str,
    invocation_id: str,
    *,
    runs_root: str | Path | None = None,
) -> Path:
    return (default_runs_root(runs_root) / safe_run_id(run_id) / "codex" / invocation_id).resolve()


@dataclass(frozen=True)
class InvocationArtifactPaths:
    artifact_dir: Path
    intent_path: Path
    identity_path: Path
    stdout_path: Path
    stderr_path: Path
    final_message_path: Path
    exit_path: Path
    cancel_path: Path
    progress_failure_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "artifact_dir": str(self.artifact_dir),
            "intent_path": str(self.intent_path),
            "identity_path": str(self.identity_path),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "final_message_path": str(self.final_message_path),
            "exit_path": str(self.exit_path),
            "cancel_path": str(self.cancel_path),
            "progress_failure_path": str(self.progress_failure_path),
        }


def artifact_paths_for(
    run_id: str,
    invocation_id: str,
    *,
    runs_root: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> InvocationArtifactPaths:
    directory = (
        Path(artifact_dir).expanduser().resolve(strict=False)
        if artifact_dir is not None
        else invocation_artifact_dir(run_id, invocation_id, runs_root=runs_root)
    )
    return InvocationArtifactPaths(
        artifact_dir=directory,
        intent_path=directory / INTENT_FILENAME,
        identity_path=directory / IDENTITY_FILENAME,
        stdout_path=directory / STDOUT_FILENAME,
        stderr_path=directory / STDERR_FILENAME,
        final_message_path=directory / FINAL_MESSAGE_FILENAME,
        exit_path=directory / EXIT_FILENAME,
        cancel_path=directory / CANCEL_FILENAME,
        progress_failure_path=directory / PROGRESS_FAILURE_FILENAME,
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)
    encoded = encoded if encoded.endswith("\n") else f"{encoded}\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def boot_identity() -> str:
    for name in ("kern.bootsessionuuid", "kern.boottime"):
        try:
            completed = subprocess.run(
                ["sysctl", "-n", name],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        text = completed.stdout.strip()
        if completed.returncode == 0 and text:
            return f"{name}={text}"
    return "boot-unknown"


def process_start_identity(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    text = completed.stdout.strip()
    return text or None


def process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def capture_process_identity(invocation_id: str) -> dict[str, Any]:
    pid = os.getpid()
    pgid = process_group_id(pid)
    return {
        "codex_invocation_id": invocation_id,
        "boot_id": boot_identity(),
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "start_identity": process_start_identity(pid),
        "created_at": _utc_now(),
    }


def identity_matches_live_process(identity: dict[str, Any] | None) -> bool:
    if not isinstance(identity, dict):
        return False
    pid = identity.get("pid")
    if not isinstance(pid, int) or not pid_is_alive(pid):
        return False
    expected_boot = identity.get("boot_id")
    if expected_boot != boot_identity():
        return False
    expected_start = identity.get("start_identity")
    live_start = process_start_identity(pid)
    if not expected_start or live_start != expected_start:
        return False
    expected_pgid = identity.get("pgid")
    if isinstance(expected_pgid, int):
        live_pgid = process_group_id(pid)
        if live_pgid != expected_pgid:
            return False
    expected_invocation = identity.get("codex_invocation_id")
    if not isinstance(expected_invocation, str) or not expected_invocation:
        return False
    return True


def _pgrep_invocation_pids(invocation_id: str) -> list[int]:
    if not invocation_id:
        return []
    try:
        completed = subprocess.run(
            ["pgrep", "-f", invocation_id],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            pid = int(text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        pids.append(pid)
    return pids


def _command_line_for_pid(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip()


def live_wrapper_pids_for_invocation(invocation_id: str, artifact_dir: Path | None = None) -> list[int]:
    markers = [invocation_id, WRAPPER_MODULE]
    if artifact_dir is not None:
        markers.append(str(artifact_dir))
    matches: list[int] = []
    for pid in _pgrep_invocation_pids(invocation_id):
        command = _command_line_for_pid(pid)
        if WRAPPER_MODULE not in command:
            continue
        if not any(marker in command for marker in markers):
            continue
        matches.append(pid)
    return matches


def wrapper_argv(manifest_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        WRAPPER_MODULE,
        "--manifest",
        str(manifest_path),
    ]


def wrapper_python_environment() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root if not existing else f"{repo_root}{os.pathsep}{existing}"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def prepare_invocation_directory(paths: InvocationArtifactPaths) -> None:
    paths.artifact_dir.mkdir(parents=True, exist_ok=True)
    for path in (paths.stdout_path, paths.stderr_path):
        path.touch(exist_ok=True)


def build_intent_payload(
    *,
    run_id: str,
    invocation_id: str,
    prompt: str,
    repo_path: str,
    cwd: str,
    sandbox: str,
    model: str | None,
    command: list[str],
    json_mode: bool,
    paths: InvocationArtifactPaths,
    extraction_event_id: int | None = None,
    extraction_prompt_sha256: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "codex_invocation_id": invocation_id,
        "run_id": run_id,
        "prompt": prompt,
        "prompt_sha256": _sha256_text(prompt),
        "repo_path": repo_path,
        "cwd": cwd,
        "sandbox": sandbox,
        "model": model,
        "command": list(command),
        "command_sha256": _sha256_text(json.dumps(list(command), separators=(",", ":"))),
        "json_mode": bool(json_mode),
        "state": "spawn_pending",
        "created_at": _utc_now(),
        **paths.as_dict(),
    }
    if extraction_event_id is not None:
        payload["extraction_event_id"] = extraction_event_id
    if extraction_prompt_sha256 is not None:
        payload["extraction_prompt_sha256"] = extraction_prompt_sha256
    return payload


def write_intent(paths: InvocationArtifactPaths, payload: dict[str, Any]) -> None:
    prepare_invocation_directory(paths)
    write_json_atomic(paths.intent_path, payload)


def write_cancel_marker(
    paths: InvocationArtifactPaths,
    *,
    invocation_id: str,
    source: str,
) -> None:
    write_json_atomic(
        paths.cancel_path,
        {
            "codex_invocation_id": invocation_id,
            "source": source,
            "requested_at": _utc_now(),
        },
    )


def write_progress_persistence_failure(paths: InvocationArtifactPaths, error: str) -> None:
    write_json_atomic(
        paths.progress_failure_path,
        {
            "failed": True,
            "error": error,
            "recorded_at": _utc_now(),
        },
    )


def event_metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    raw = event.get("metadata_json")
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _event_id(event: dict[str, Any]) -> int | None:
    value = event.get("id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def ledger_invocation_records(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for event in events:
        metadata = event_metadata(event)
        invocation_id = metadata.get("codex_invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            continue
        record = records.setdefault(
            invocation_id,
            {
                "codex_invocation_id": invocation_id,
                "started_event": None,
                "finished_event": None,
            },
        )
        event_type = event.get("event_type")
        if event_type == "codex_exec_started":
            record["started_event"] = event
        elif event_type == "codex_exec_finished":
            record["finished_event"] = event
    return records


def disk_invocation_ids(run_id: str, *, runs_root: str | Path | None = None) -> list[str]:
    root = default_runs_root(runs_root) / safe_run_id(run_id) / "codex"
    if not root.is_dir():
        return []
    found: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / INTENT_FILENAME).is_file():
            found.append(child.name)
    return found


@dataclass
class InvocationClassification:
    status: str
    invocation_id: str
    run_id: str
    paths: InvocationArtifactPaths
    reason: str
    identity: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    exit_payload: dict[str, Any] | None = None
    started_event: dict[str, Any] | None = None
    finished_event: dict[str, Any] | None = None
    live_pids: list[int] = field(default_factory=list)


def classify_invocation(
    run_id: str,
    invocation_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
    runs_root: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> InvocationClassification:
    paths = artifact_paths_for(
        run_id,
        invocation_id,
        runs_root=runs_root,
        artifact_dir=artifact_dir,
    )
    records = ledger_invocation_records(events or [])
    record = records.get(invocation_id, {})
    started_event = record.get("started_event")
    finished_event = record.get("finished_event")
    intent = read_json_file(paths.intent_path)
    identity = read_json_file(paths.identity_path)
    exit_payload = read_json_file(paths.exit_path)
    has_start_evidence = started_event is not None or intent is not None or identity is not None
    if finished_event is not None:
        return InvocationClassification(
            status=STATUS_ALREADY_FINISHED,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="codex_exec_finished already exists for this invocation",
            identity=identity,
            intent=intent,
            exit_payload=exit_payload,
            started_event=started_event,
            finished_event=finished_event,
        )
    if not has_start_evidence:
        return InvocationClassification(
            status=STATUS_NEVER_SPAWNED,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="no durable start or intent evidence",
        )

    live_identity = identity_matches_live_process(identity)
    argv_pids = live_wrapper_pids_for_invocation(invocation_id, paths.artifact_dir)
    if live_identity:
        return InvocationClassification(
            status=STATUS_LIVE,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="persisted wrapper identity matches a live process",
            identity=identity,
            intent=intent,
            exit_payload=exit_payload,
            started_event=started_event,
            live_pids=argv_pids,
        )
    if identity is None and argv_pids:
        return InvocationClassification(
            status=STATUS_LIVE,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="wrapper argv is live before identity persistence",
            intent=intent,
            started_event=started_event,
            live_pids=argv_pids,
        )
    if identity is not None and not live_identity and argv_pids:
        return InvocationClassification(
            status=STATUS_UNCERTAIN,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="live pid does not match persisted wrapper identity",
            identity=identity,
            intent=intent,
            started_event=started_event,
            live_pids=argv_pids,
        )

    if _exit_evidence_is_complete(paths, exit_payload):
        return InvocationClassification(
            status=STATUS_COMPLETE,
            invocation_id=invocation_id,
            run_id=run_id,
            paths=paths,
            reason="wrapper is gone and exit.json plus stream artifacts are complete",
            identity=identity,
            intent=intent,
            exit_payload=exit_payload,
            started_event=started_event,
        )
    return InvocationClassification(
        status=STATUS_UNCERTAIN,
        invocation_id=invocation_id,
        run_id=run_id,
        paths=paths,
        reason="start evidence exists but completion is not proven",
        identity=identity,
        intent=intent,
        exit_payload=exit_payload,
        started_event=started_event,
        live_pids=argv_pids,
    )


def _exit_evidence_is_complete(
    paths: InvocationArtifactPaths,
    exit_payload: dict[str, Any] | None,
) -> bool:
    if not isinstance(exit_payload, dict):
        return False
    if "exit_code" not in exit_payload:
        return False
    if not paths.stdout_path.is_file() or not paths.stderr_path.is_file():
        return False
    return True


def discover_open_invocations(
    run_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
    runs_root: str | Path | None = None,
) -> list[InvocationClassification]:
    event_list = events or []
    records = ledger_invocation_records(event_list)
    invocation_ids = list(records.keys())
    for disk_id in disk_invocation_ids(run_id, runs_root=runs_root):
        if disk_id not in invocation_ids:
            invocation_ids.append(disk_id)
    classified = [
        classify_invocation(
            run_id,
            invocation_id,
            events=event_list,
            runs_root=runs_root,
        )
        for invocation_id in invocation_ids
    ]
    return [
        item
        for item in classified
        if item.status in {STATUS_LIVE, STATUS_COMPLETE, STATUS_UNCERTAIN}
    ]


def latest_open_invocation(
    run_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
    runs_root: str | Path | None = None,
) -> InvocationClassification | None:
    open_items = discover_open_invocations(run_id, events=events, runs_root=runs_root)
    return open_items[-1] if open_items else None


@dataclass
class ObservationResult:
    stdout: str
    stderr: str
    exit_payload: dict[str, Any] | None
    wrapper_returncode: int | None
    complete: bool
    progress_persistence_failed: bool = False


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def tail_stdout_and_wait(
    *,
    paths: InvocationArtifactPaths,
    wrapper: subprocess.Popen | None,
    invocation_id: str,
    progress_callback: Callable[[str], None] | None = None,
    poll_seconds: float = _TAIL_POLL_SECONDS,
) -> ObservationResult:
    offset = 0
    remainder = ""
    wrapper_returncode: int | None = None

    def drain() -> None:
        nonlocal offset, remainder
        if not paths.stdout_path.exists():
            return
        with paths.stdout_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
        if not chunk:
            return
        remainder += chunk
        while "\n" in remainder:
            line, remainder = remainder.split("\n", 1)
            text = _decode_output(f"{line}\n")
            if progress_callback is not None and text.strip():
                progress_callback(text)

    while True:
        drain()
        exit_payload = read_json_file(paths.exit_path)
        if wrapper is not None:
            wrapper_returncode = wrapper.poll()
        live = False
        if wrapper is None:
            identity = read_json_file(paths.identity_path)
            live = identity_matches_live_process(identity) or bool(
                live_wrapper_pids_for_invocation(invocation_id, paths.artifact_dir)
            )
        if exit_payload is not None and _exit_evidence_is_complete(paths, exit_payload):
            drain()
            break
        finished_waiting = (
            wrapper is not None and wrapper_returncode is not None
        ) or (wrapper is None and not live)
        if finished_waiting:
            for _ in range(_EXIT_GRACE_POLLS):
                drain()
                exit_payload = read_json_file(paths.exit_path)
                if exit_payload is not None and _exit_evidence_is_complete(paths, exit_payload):
                    break
                time.sleep(poll_seconds)
            drain()
            break
        time.sleep(poll_seconds)

    exit_payload = read_json_file(paths.exit_path)
    if wrapper is not None:
        try:
            wrapper_returncode = wrapper.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            wrapper_returncode = wrapper.poll()
    return ObservationResult(
        stdout=_read_text_file(paths.stdout_path),
        stderr=_read_text_file(paths.stderr_path),
        exit_payload=exit_payload,
        wrapper_returncode=wrapper_returncode,
        complete=_exit_evidence_is_complete(paths, exit_payload),
        progress_persistence_failed=paths.progress_failure_path.is_file(),
    )


def spawn_invocation_wrapper(
    paths: InvocationArtifactPaths,
    *,
    cwd: str | None = None,
) -> subprocess.Popen:
    return subprocess.Popen(
        wrapper_argv(paths.intent_path),
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=wrapper_python_environment(),
    )


def result_from_artifacts(
    *,
    paths: InvocationArtifactPaths,
    command: list[str],
    cwd: str,
    started_at: str,
    observation: ObservationResult | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observation = observation or ObservationResult(
        stdout=_read_text_file(paths.stdout_path),
        stderr=_read_text_file(paths.stderr_path),
        exit_payload=read_json_file(paths.exit_path),
        wrapper_returncode=None,
        complete=_exit_evidence_is_complete(paths, read_json_file(paths.exit_path)),
        progress_persistence_failed=paths.progress_failure_path.is_file(),
    )
    exit_payload = observation.exit_payload or {}
    cancel_payload = read_json_file(paths.cancel_path)
    exit_code = exit_payload.get("exit_code")
    signaled = bool(exit_payload.get("signaled"))
    if cancel_payload is not None:
        termination_reason = TERMINATION_OPERATOR_CANCELLED
    elif signaled:
        termination_reason = TERMINATION_SIGNALED
    elif observation.complete:
        termination_reason = TERMINATION_EXITED
    else:
        termination_reason = TERMINATION_UNCERTAIN
    payload = {
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "stdout": observation.stdout,
        "stderr": observation.stderr,
        "timed_out": False,
        "started_at": started_at,
        "finished_at": str(exit_payload.get("finished_at") or _utc_now()),
        "progress_persistence_failed": bool(observation.progress_persistence_failed),
        "termination_reason": termination_reason,
        "invocation_complete": observation.complete,
        "cancel_requested": cancel_payload is not None,
        **paths.as_dict(),
    }
    if extra:
        payload.update(extra)
    return payload


def signal_process_group(pgid: int, sig: int) -> None:
    os.killpg(pgid, sig)


def terminate_verified_identity(
    identity: dict[str, Any],
    *,
    timeout_seconds: float = 2.0,
    cancel_paths: InvocationArtifactPaths | None = None,
    source: str = "operator_cancel",
) -> dict[str, Any]:
    if not identity_matches_live_process(identity):
        return {
            "terminated": False,
            "reason_code": "identity_mismatch",
        }
    pid = int(identity["pid"])
    pgid = identity.get("pgid")
    if not isinstance(pgid, int):
        pgid = process_group_id(pid) or pid
    if cancel_paths is not None:
        write_cancel_marker(
            cancel_paths,
            invocation_id=str(identity.get("codex_invocation_id") or ""),
            source=source,
        )
    try:
        signal_process_group(pgid, signal.SIGTERM)
    except OSError as exc:
        return {
            "terminated": False,
            "reason_code": "codex_process_termination_failed",
            "error_message": str(exc),
        }
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not identity_matches_live_process(identity) and not pid_is_alive(pid):
            return {
                "terminated": True,
                "reason_code": "codex_process_terminated",
                "signal": "SIGTERM",
            }
        time.sleep(0.05)
    if identity_matches_live_process(identity) or pid_is_alive(pid):
        try:
            if identity_matches_live_process(identity):
                signal_process_group(pgid, signal.SIGKILL)
        except OSError as exc:
            return {
                "terminated": False,
                "reason_code": "codex_process_termination_failed",
                "error_message": str(exc),
            }
    return {
        "terminated": True,
        "reason_code": "codex_process_terminated",
        "signal": "SIGKILL",
    }
