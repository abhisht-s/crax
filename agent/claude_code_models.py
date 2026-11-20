"""Claude models a Claude Code run can use, with the effort levels each accepts.

Single source for run-profile validation (``agent.ledger``), the Claude Code
adapter (``--model`` / ``--effort``) and the dashboard picker. IDs and effort
support come from Anthropic's model table (claude-api reference, cached
2026-11-03). Mythos models are omitted (verified organizations only).

The stored "default" model token is ``codex_default`` for every agent (the
name predates other agents); for Claude Code it resolves to the user's own
``model``/``effortLevel`` settings, else Claude Code's built-in default.
"""

from __future__ import annotations

from dataclasses import dataclass

CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_NO_XHIGH = ("low", "medium", "high", "max")


@dataclass(frozen=True)
class ClaudeModel:
    model_id: str
    label: str
    description: str
    group: str  # "main" (shown first, like Claude Code's /model) or "more"
    efforts: tuple[str, ...] = CLAUDE_EFFORT_LEVELS


CLAUDE_MODELS: tuple[ClaudeModel, ...] = (
    ClaudeModel("claude-opus-5-5", "Opus 5.5", "For complex work and everyday tasks", "main"),
    ClaudeModel("claude-fable-5-2", "Fable 5.2", "For your toughest challenges", "main"),
    ClaudeModel("claude-sonnet-5-5", "Sonnet 5.5", "Most efficient for simpler tasks", "main"),
    ClaudeModel("claude-haiku-5-5", "Haiku 5.5", "Fastest for quick answers", "main"),
    ClaudeModel("claude-fable-5-1", "Fable 5.1", "Previous Fable", "more"),
    ClaudeModel("claude-fable-5", "Fable 5", "Earlier Fable", "more"),
    ClaudeModel("claude-opus-5", "Opus 5", "Previous Opus", "more"),
    ClaudeModel("claude-opus-4-8", "Opus 4.8", "Earlier Opus", "more"),
    ClaudeModel("claude-opus-4-7", "Opus 4.7", "Earlier Opus", "more"),
    ClaudeModel("claude-opus-4-6", "Opus 4.6", "Earlier Opus (no xhigh effort)", "more", _NO_XHIGH),
    ClaudeModel("claude-sonnet-5", "Sonnet 5", "Previous Sonnet", "more"),
    ClaudeModel("claude-sonnet-4-6", "Sonnet 4.6", "Earlier Sonnet (no xhigh effort)", "more", _NO_XHIGH),
    ClaudeModel("claude-haiku-4-5", "Haiku 4.5", "Earlier Haiku (no effort setting)", "more", ()),
)
CLAUDE_MODELS_BY_ID = {model.model_id: model for model in CLAUDE_MODELS}
CLAUDE_MODEL_IDS = tuple(model.model_id for model in CLAUDE_MODELS)


def claude_model_supports_effort(model_id: str, effort: str) -> bool:
    """True when the model accepts this ``--effort`` level.

    Unknown models (e.g. a model from the user's own settings, such as the
    alias ``opus``) are not restricted here; Claude Code decides.
    """

    model = CLAUDE_MODELS_BY_ID.get(model_id)
    return effort in CLAUDE_EFFORT_LEVELS if model is None else effort in model.efforts
