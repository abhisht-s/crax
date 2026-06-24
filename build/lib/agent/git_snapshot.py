from __future__ import annotations

import difflib
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from agent.codex_terminal import run_command


GIT_COMMANDS = {
    "is_inside_work_tree": ["git", "rev-parse", "--is-inside-work-tree"],
    "head": ["git", "rev-parse", "HEAD"],
    "branch": ["git", "branch", "--show-current"],
    "status_short": ["git", "status", "--short"],
    "diff_stat": ["git", "diff", "--stat"],
    "diff_name_only": ["git", "diff", "--name-only"],
    "diff_name_status": ["git", "diff", "--name-status"],
    "status_porcelain_z": ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
}

ATTRIBUTION_VERSION = "invocation_delta_v1"
MAX_CAPTURE_BYTES = 1_000_000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _clean_optional(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned if cleaned else None


def _command_value(result: dict | None) -> str:
    if result is None or result["exit_code"] != 0 or result["timed_out"]:
        return ""
    return result["stdout"].rstrip("\n")


def capture_git_snapshot(repo_path: str) -> dict:
    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    repo_path_text = str(resolved_repo_path)

    snapshot = {
        "repo_path": repo_path_text,
        "is_git_repo": False,
        "head": None,
        "branch": None,
        "status_short": "",
        "diff_stat": "",
        "diff_name_only": "",
        "commands": {},
        "validation_error": None,
        "captured_at": _utc_now(),
    }

    if not resolved_repo_path.exists():
        snapshot["validation_error"] = f"Repo path does not exist: {repo_path_text}"
        return snapshot

    if not resolved_repo_path.is_dir():
        snapshot["validation_error"] = f"Repo path is not a directory: {repo_path_text}"
        return snapshot

    commands = {}
    is_inside_result = run_command(
        GIT_COMMANDS["is_inside_work_tree"],
        cwd=repo_path_text,
        timeout_seconds=30,
    )
    commands["is_inside_work_tree"] = is_inside_result
    snapshot["commands"] = commands

    is_inside_work_tree = _command_value(is_inside_result).strip() == "true"
    if not is_inside_work_tree:
        return snapshot

    snapshot["is_git_repo"] = True

    for name in (
        "head",
        "branch",
        "status_short",
        "diff_stat",
        "diff_name_only",
        "diff_name_status",
    ):
        commands[name] = run_command(GIT_COMMANDS[name], cwd=repo_path_text, timeout_seconds=30)

    snapshot["head"] = _clean_optional(_command_value(commands["head"]))
    snapshot["branch"] = _clean_optional(_command_value(commands["branch"]))
    snapshot["status_short"] = _command_value(commands["status_short"])
    snapshot["diff_stat"] = _command_value(commands["diff_stat"])
    snapshot["diff_name_only"] = _command_value(commands["diff_name_only"])
    snapshot["diff_name_status"] = _command_value(commands["diff_name_status"])

    return snapshot


def _paths_from_name_status(name_status: str) -> list[str]:
    paths = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            paths.append(parts[2])
        elif len(parts) >= 2:
            paths.append(parts[1])
    return [path for path in paths if path.strip()]


def capture_git_diff_metadata(repo_path: str, paths: list[str] | None = None) -> dict:
    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    repo_path_text = str(resolved_repo_path)
    metadata = {
        "repo_path": repo_path_text,
        "name_status": "",
        "changed_paths": [],
        "diff_unified_zero": "",
        "commands": {},
        "validation_error": None,
        "captured_at": _utc_now(),
    }
    if not resolved_repo_path.exists():
        metadata["validation_error"] = f"Repo path does not exist: {repo_path_text}"
        return metadata
    if not resolved_repo_path.is_dir():
        metadata["validation_error"] = f"Repo path is not a directory: {repo_path_text}"
        return metadata

    name_status_result = run_command(GIT_COMMANDS["diff_name_status"], cwd=repo_path_text, timeout_seconds=30)
    metadata["commands"]["diff_name_status"] = name_status_result
    if name_status_result["exit_code"] != 0 or name_status_result["timed_out"]:
        metadata["validation_error"] = "git diff --name-status failed or timed out"
        return metadata

    name_status = name_status_result["stdout"].rstrip("\n")
    changed_paths = paths if paths is not None else _paths_from_name_status(name_status)
    metadata["name_status"] = name_status
    metadata["changed_paths"] = changed_paths

    diff_command = ["git", "diff", "--unified=0"]
    if changed_paths:
        diff_command = [*diff_command, "--", *changed_paths]
    diff_result = run_command(diff_command, cwd=repo_path_text, timeout_seconds=30)
    metadata["commands"]["diff_unified_zero"] = diff_result
    if diff_result["exit_code"] != 0 or diff_result["timed_out"]:
        metadata["validation_error"] = "git diff --unified=0 failed or timed out"
        return metadata
    metadata["diff_unified_zero"] = diff_result["stdout"]
    return metadata


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_porcelain_z(status_text: str) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    tokens = [token for token in status_text.split("\0") if token]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if len(token) < 4:
            index += 1
            continue
        xy = token[:2]
        path = token[3:].replace("\\", "/")
        old_path = None
        if xy[0] in {"R", "C"} and index + 1 < len(tokens):
            old_path = tokens[index + 1].replace("\\", "/")
            index += 1
        entries[path] = {
            "path": path,
            "status": xy,
            "index_status": xy[0],
            "worktree_status": xy[1],
            "old_path": old_path,
            "untracked": xy == "??",
        }
        index += 1
    return entries


def _read_worktree_file(repo_path: Path, path: str) -> dict:
    full_path = (repo_path / path).resolve(strict=False)
    try:
        full_path.relative_to(repo_path)
    except ValueError:
        return {
            "exists": False,
            "path_escape": True,
            "sha256": None,
            "size": None,
            "text": None,
            "text_evaluable": False,
        }
    if not full_path.exists() or not full_path.is_file():
        return {
            "exists": False,
            "path_escape": False,
            "sha256": None,
            "size": None,
            "text": None,
            "text_evaluable": False,
        }
    try:
        content = full_path.read_bytes()
    except OSError as exc:
        return {
            "exists": True,
            "path_escape": False,
            "sha256": None,
            "size": None,
            "text": None,
            "text_evaluable": False,
            "error": str(exc),
        }
    size = len(content)
    text = None
    text_evaluable = False
    if size <= MAX_CAPTURE_BYTES:
        try:
            text = content.decode("utf-8")
            text_evaluable = True
        except UnicodeDecodeError:
            text = None
    return {
        "exists": True,
        "path_escape": False,
        "sha256": _sha256_bytes(content),
        "size": size,
        "text": text,
        "text_evaluable": text_evaluable,
        "content_omitted": size > MAX_CAPTURE_BYTES or not text_evaluable,
    }


def _read_head_file(repo_path: str, path: str) -> dict:
    result = run_command(["git", "show", f"HEAD:{path}"], cwd=repo_path, timeout_seconds=30)
    if result["exit_code"] != 0 or result["timed_out"]:
        return {
            "exists": False,
            "sha256": None,
            "size": None,
            "text": None,
            "text_evaluable": False,
        }
    content = result["stdout"].encode("utf-8")
    return {
        "exists": True,
        "sha256": _sha256_bytes(content),
        "size": len(content),
        "text": result["stdout"],
        "text_evaluable": len(content) <= MAX_CAPTURE_BYTES,
    }


def capture_invocation_git_state(repo_path: str) -> dict:
    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    repo_path_text = str(resolved_repo_path)
    state = {
        "repo_path": repo_path_text,
        "captured_at": _utc_now(),
        "status_porcelain": "",
        "paths": {},
        "commands": {},
        "validation_error": None,
    }
    if not resolved_repo_path.exists():
        state["validation_error"] = f"Repo path does not exist: {repo_path_text}"
        return state
    if not resolved_repo_path.is_dir():
        state["validation_error"] = f"Repo path is not a directory: {repo_path_text}"
        return state

    status_result = run_command(GIT_COMMANDS["status_porcelain_z"], cwd=repo_path_text, timeout_seconds=30)
    state["commands"]["status_porcelain_z"] = status_result
    if status_result["exit_code"] != 0 or status_result["timed_out"]:
        state["validation_error"] = "git status --porcelain failed or timed out"
        return state

    state["status_porcelain"] = status_result["stdout"]
    entries = _parse_porcelain_z(status_result["stdout"])
    for path, entry in entries.items():
        file_state = _read_worktree_file(resolved_repo_path, path)
        state["paths"][path] = {**entry, **file_state}
    return state


def _path_changed(before: dict | None, after: dict | None) -> bool:
    if before is None and after is None:
        return False
    if before is None or after is None:
        return True
    return (
        before.get("exists") != after.get("exists")
        or before.get("sha256") != after.get("sha256")
        or before.get("status") != after.get("status")
    )


def _added_lines_from_text_diff(before_text: str, after_text: str, path: str) -> tuple[list[str], str]:
    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"before/{path}",
            tofile=f"after/{path}",
            lineterm="",
            n=0,
        )
    )
    added = [
        line[1:]
        for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    ]
    return added, "\n".join(diff_lines)


def _pre_text_for_path(repo_path: str, path: str, before_entry: dict | None) -> tuple[str | None, str]:
    if before_entry is not None:
        if before_entry.get("text_evaluable") and isinstance(before_entry.get("text"), str):
            return before_entry["text"], "pre_run_worktree"
        return None, "pre_run_not_evaluable"
    head_entry = _read_head_file(repo_path, path)
    if head_entry.get("text_evaluable") and isinstance(head_entry.get("text"), str):
        return head_entry["text"], "head"
    return "", "new_file"


def _status_paths(paths: dict[str, dict], key: str) -> list[str]:
    values = []
    for path, entry in paths.items():
        if str(entry.get(key) or " ") != " ":
            values.append(path)
    return sorted(values)


def compute_invocation_delta(before_state: dict | None, after_state: dict | None) -> dict:
    before_state = before_state or {}
    after_state = after_state or {}
    before_paths = before_state.get("paths") if isinstance(before_state.get("paths"), dict) else {}
    after_paths = after_state.get("paths") if isinstance(after_state.get("paths"), dict) else {}
    repo_path = str(after_state.get("repo_path") or before_state.get("repo_path") or "")

    result = {
        "attribution_version": ATTRIBUTION_VERSION,
        "attributable_changed_files": [],
        "attributable_added_files": [],
        "attributable_deleted_files": [],
        "attributable_renamed_files": [],
        "attributable_staged_paths": [],
        "attributable_worktree_paths": [],
        "preexisting_changed_files": sorted(path for path, entry in before_paths.items() if not entry.get("untracked")),
        "preexisting_untracked_files": sorted(path for path, entry in before_paths.items() if entry.get("untracked")),
        "path_delta_details": [],
        "not_evaluable_paths": [],
        "validation_error": before_state.get("validation_error") or after_state.get("validation_error"),
        "rename_evaluation": "not_evaluable",
    }
    if result["validation_error"]:
        return result

    all_paths = sorted(set(before_paths) | set(after_paths))
    for path in all_paths:
        before_entry = before_paths.get(path)
        after_entry = after_paths.get(path)
        changed_during_run = _path_changed(before_entry, after_entry)
        if not changed_during_run:
            continue

        after_is_tracked_change = after_entry is not None and not after_entry.get("untracked")
        existed_before = bool(before_entry and before_entry.get("exists")) or (
            before_entry is None and after_is_tracked_change
        )
        exists_after = bool(after_entry and after_entry.get("exists"))
        was_tracked_or_dirty_before = before_entry is not None and not before_entry.get("untracked")
        was_untracked_before = before_entry is not None and bool(before_entry.get("untracked"))
        is_new_path = before_entry is None and after_entry is not None

        change_type = "modified"
        if not existed_before and exists_after:
            change_type = "added"
        elif existed_before and not exists_after:
            change_type = "deleted"

        added_lines: list[str] = []
        diff_text = ""
        hunk_evaluable = False
        if exists_after:
            before_text, before_source = _pre_text_for_path(repo_path, path, before_entry)
            after_text = after_entry.get("text") if after_entry else None
            if isinstance(before_text, str) and isinstance(after_text, str) and after_entry.get("text_evaluable"):
                added_lines, diff_text = _added_lines_from_text_diff(before_text, after_text, path)
                hunk_evaluable = True
            else:
                before_source = before_source if "before_source" not in locals() else before_source
                result["not_evaluable_paths"].append(path)
        else:
            before_source = "pre_run_worktree" if before_entry else "head"

        detail = {
            "path": path,
            "change_type": change_type,
            "preexisting": before_entry is not None,
            "preexisting_tracked_change": was_tracked_or_dirty_before,
            "preexisting_untracked": was_untracked_before,
            "new_path": is_new_path,
            "before_status": before_entry.get("status") if before_entry else None,
            "after_status": after_entry.get("status") if after_entry else None,
            "before_sha256": before_entry.get("sha256") if before_entry else None,
            "after_sha256": after_entry.get("sha256") if after_entry else None,
            "hunk_evaluable": hunk_evaluable,
            "before_source": before_source,
            "added_lines": added_lines,
            "diff_unified_zero": diff_text,
        }
        result["path_delta_details"].append(detail)

        if change_type == "added":
            result["attributable_added_files"].append(path)
        elif change_type == "deleted":
            result["attributable_deleted_files"].append(path)
        else:
            result["attributable_changed_files"].append(path)

        if after_entry is not None:
            if str(after_entry.get("index_status") or " ") != " ":
                result["attributable_staged_paths"].append(path)
            if str(after_entry.get("worktree_status") or " ") != " ":
                result["attributable_worktree_paths"].append(path)

    for key in (
        "attributable_changed_files",
        "attributable_added_files",
        "attributable_deleted_files",
        "attributable_renamed_files",
        "attributable_staged_paths",
        "attributable_worktree_paths",
        "not_evaluable_paths",
    ):
        result[key] = sorted(set(result[key]))
    return result


def attributable_paths(delta: dict | None) -> list[str]:
    if not isinstance(delta, dict):
        return []
    paths: set[str] = set()
    for key in (
        "attributable_changed_files",
        "attributable_added_files",
        "attributable_deleted_files",
        "attributable_renamed_files",
    ):
        values = delta.get(key)
        if isinstance(values, list):
            paths.update(str(path) for path in values if str(path).strip())
    return sorted(paths)
