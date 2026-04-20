"""Configure flow: built-in question defaults, resolver, AskUserQuestion payload shaping.

The outer Claude Code session calls `get_configure_questions(agent, model)` via
MCP, which returns a payload shaped for AskUserQuestion plus extra per-question
metadata (`key`, `kind`, `optional`) so the LLM can map answers back to
`new_actor` config pairs.

Resolution order (model-scoped wins over agent-level, which wins over builtins):
  1. Built-in default for the agent (BUILTIN_QUESTIONS).
  2. `agent "<name>" { configure { ... } }` (agent-level override).
  3. `agent "<name>" { configure "<model>" { ... } }` (model-scoped override).

User + project layering is already handled at load time in `_merge`.

The `configure-default` top-level KDL key toggles the feature: "on" (default)
enables, "off" disables. When "off", `resolve_questions` raises
`ConfigureDisabledError`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .config import AppConfig, Question, QuestionOption
from .errors import ActorError


class ConfigureDisabledError(ActorError):
    """Raised when configure-default is 'off' and resolution is attempted."""

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(
            message
            or 'configure flow is disabled globally (configure-default "off")'
        )


# Built-in defaults. Labels are the actual config values (what `actor config`
# accepts) so the answer strings can be used verbatim.
BUILTIN_QUESTIONS: Dict[str, List[Question]] = {
    "claude": [
        Question(
            key="model",
            prompt="Which Claude model?",
            header="Model",
            options=[
                QuestionOption("opus", "Most capable, highest cost"),
                QuestionOption("sonnet", "Balanced"),
                QuestionOption("haiku", "Fastest, lowest cost"),
            ],
        ),
        Question(
            key="effort",
            prompt="How hard should it think?",
            header="Effort",
            options=[
                QuestionOption("max", "Maximum thinking"),
                QuestionOption("high"),
                QuestionOption("medium"),
                QuestionOption("low"),
            ],
        ),
        Question(
            key="permission-mode",
            prompt="Permission mode?",
            header="Perms",
            options=[
                QuestionOption(
                    "bypassPermissions", "Skip all permission checks (default)"
                ),
                QuestionOption(
                    "acceptEdits", "Auto-approve file edits, ask for other actions"
                ),
                QuestionOption("plan", "Plan mode — no edits"),
            ],
        ),
        Question(
            key="prompt",
            prompt="Initial prompt (optional)?",
            header="Prompt",
            options=[],
            kind="text",
            optional=True,
        ),
    ],
    "codex": [
        Question(
            key="sandbox",
            prompt="Sandbox policy?",
            header="Sandbox",
            options=[
                QuestionOption("danger-full-access", "No sandboxing (default)"),
                QuestionOption(
                    "workspace-write", "Writes only inside the workspace"
                ),
                QuestionOption("read-only", "No writes allowed"),
            ],
        ),
        Question(
            key="prompt",
            prompt="Initial prompt (optional)?",
            header="Prompt",
            options=[],
            kind="text",
            optional=True,
        ),
    ],
}


def resolve_questions(
    config: AppConfig,
    agent: str,
    model: Optional[str],
) -> List[Question]:
    """Resolve the effective question list for (agent, model).

    Raises ConfigureDisabledError if the feature is off globally, and
    ActorError if the agent name is unknown.
    """
    if config.configure_default == "off":
        raise ConfigureDisabledError()

    if agent not in BUILTIN_QUESTIONS:
        raise ActorError(
            f"unknown agent '{agent}' — supported: {sorted(BUILTIN_QUESTIONS)}"
        )

    settings = config.agents.get(agent)
    if settings is not None:
        if model is not None and model in settings.configure_blocks:
            return list(settings.configure_blocks[model].questions)
        if None in settings.configure_blocks:
            return list(settings.configure_blocks[None].questions)

    return list(BUILTIN_QUESTIONS[agent])


# Sentinel labels for synthesized text-question options.
TEXT_ENTER_LABEL = "Enter below"
TEXT_SKIP_LABEL = "Skip"


def questions_to_payload(questions: List[Question]) -> Dict:
    """Convert Question objects into an AskUserQuestion-shaped payload.

    Each payload entry keeps per-question metadata (`key`, `kind`, `optional`)
    so the LLM can map answers back to config keys. The skill in
    `src/actor/_skill/SKILL.md` documents the mapping contract.
    """
    out_questions = []
    for q in questions:
        header = q.header[:12]
        if q.kind == "text":
            options = [
                {
                    "label": TEXT_ENTER_LABEL,
                    "description": "Select 'Other' below to type your answer",
                },
                {
                    "label": TEXT_SKIP_LABEL,
                    "description": "Leave this unset",
                },
            ]
        else:
            options = [
                {"label": opt.label, "description": opt.description}
                for opt in q.options
            ]
        out_questions.append(
            {
                "key": q.key,
                "kind": q.kind,
                "optional": q.optional,
                "question": q.prompt,
                "header": header,
                "options": options,
                "multiSelect": q.multi_select,
            }
        )
    return {"questions": out_questions}
