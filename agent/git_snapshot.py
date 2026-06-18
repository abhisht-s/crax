from __future__ import annotations

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
}


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
    ):
        commands[name] = run_command(GIT_COMMANDS[name], cwd=repo_path_text, timeout_seconds=30)

    snapshot["head"] = _clean_optional(_command_value(commands["head"]))
    snapshot["branch"] = _clean_optional(_command_value(commands["branch"]))
    snapshot["status_short"] = _command_value(commands["status_short"])
    snapshot["diff_stat"] = _command_value(commands["diff_stat"])
    snapshot["diff_name_only"] = _command_value(commands["diff_name_only"])

    return snapshot
