"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map. Project config overrides user config on same-key conflict.

Out of scope (see tickets #30 / #31 / #33): hooks, per-agent default-config
blocks, aliases. Their nodes are skipped at parse time without error for
forward compatibility.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import kdl

from .errors import ConfigError


@dataclass
class Template:
    name: str
    agent: Optional[str] = None
    prompt: Optional[str] = None
    config: Dict[str, str] = field(default_factory=dict)


@dataclass
class QuestionOption:
    label: str
    description: str = ""


@dataclass
class Question:
    key: str
    prompt: str
    header: str
    options: List[QuestionOption]
    kind: str = "options"
    optional: bool = False
    multi_select: bool = False


@dataclass
class ConfigureBlock:
    model: Optional[str]
    questions: List[Question]


@dataclass
class AgentSettings:
    name: str
    configure_blocks: Dict[Optional[str], ConfigureBlock] = field(default_factory=dict)


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    agents: Dict[str, AgentSettings] = field(default_factory=dict)
    configure_default: str = "on"
    # Did a config file explicitly set `configure_default`? Needed during
    # merge so a project file can override a user file's value in both
    # directions (e.g. user "off" → project "on"), independent of which
    # value happens to equal the hardcoded default.
    configure_default_set: bool = False


def _find_project_config(
    start: Path, user_path: Optional[Path] = None
) -> Optional[Path]:
    """Walk up from start looking for .actor/settings.kdl.

    If user_path is given and the walk-up lands on that exact file, skip it
    — otherwise `cwd` inside `$HOME` (with no closer project config) would
    wrongly treat the user's settings as a 'project' config and parse it
    twice.
    """
    try:
        start = start.resolve(strict=False)
    except OSError:
        return None
    resolved_user = None
    if user_path is not None:
        try:
            resolved_user = user_path.resolve(strict=False)
        except OSError:
            resolved_user = None
    for d in [start, *start.parents]:
        p = d / ".actor" / "settings.kdl"
        if not p.is_file():
            continue
        if resolved_user is not None:
            try:
                resolved_p = p.resolve(strict=False)
            except OSError:
                # Can't tell if this is the user file — skip rather than
                # risk double-loading it.
                continue
            if resolved_p == resolved_user:
                continue
        return p
    return None


def _coerce_value(value: object) -> str:
    """Coerce a KDL-parsed arg (str/bool/float) into a string for the
    stringly-typed actor config pipeline. Unknown types raise ConfigError
    so a future kdl-py change can't silently stringify something
    nonsensical (e.g. a null → "None")."""
    # bool must be checked before int/float (bool is a subclass of int).
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        # kdl-py parses all numbers as float; drop the trailing .0 for
        # integer-valued literals so `effort 5` stays "5" not "5.0".
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, int):
        return str(value)
    raise ConfigError(f"unsupported KDL value type: {type(value).__name__}")


def _parse_template(node, source: Path) -> Template:
    if not node.args or not isinstance(node.args[0], str):
        raise ConfigError(
            f"template block in {source} must have a name "
            f"(e.g. `template \"qa\" {{ ... }}`)"
        )
    if not node.args[0]:
        raise ConfigError(
            f"template block in {source} must have a non-empty name"
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"template '{node.args[0]}' in {source} has extra positional args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"template '{node.args[0]}' in {source} does not accept properties"
        )
    name = node.args[0]
    tpl = Template(name=name)
    seen_keys: set[str] = set()
    for child in node.nodes:
        key = child.name
        if key in seen_keys:
            raise ConfigError(
                f"template '{name}' in {source}: duplicate key '{key}'"
            )
        seen_keys.add(key)
        if not child.args:
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' needs a value"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' has extra args "
                f"(only a single value is supported)"
            )
        if getattr(child, "props", None):
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' does not accept properties"
            )
        raw = child.args[0]
        if key in ("agent", "prompt") and not isinstance(raw, str):
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' must be a string"
            )
        value_str = _coerce_value(raw)
        if key == "agent":
            tpl.agent = value_str
        elif key == "prompt":
            tpl.prompt = value_str
        else:
            tpl.config[key] = value_str
    return tpl


def _parse_question(node, block_desc: str, source: Path) -> Question:
    if not node.args or not isinstance(node.args[0], str) or not node.args[0]:
        raise ConfigError(
            f"question in {block_desc} of {source} must have a string key "
            f'(e.g. `question "model" {{ ... }}`)'
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"question '{node.args[0]}' in {block_desc} of {source} has extra args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"question '{node.args[0]}' in {block_desc} of {source} "
            f"does not accept properties"
        )
    key = node.args[0]
    prompt: Optional[str] = None
    header: Optional[str] = None
    kind = "options"
    optional = False
    multi_select = False
    options: List[QuestionOption] = []
    seen_keys: set[str] = set()
    for child in node.nodes:
        cname = child.name
        if cname in seen_keys:
            raise ConfigError(
                f"question '{key}' in {block_desc} of {source}: "
                f"duplicate child '{cname}'"
            )
        seen_keys.add(cname)
        if cname == "prompt":
            if not child.args or not isinstance(child.args[0], str):
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f"prompt must be a string"
                )
            prompt = child.args[0]
        elif cname == "header":
            if not child.args or not isinstance(child.args[0], str):
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f"header must be a string"
                )
            header = child.args[0]
        elif cname == "kind":
            if not child.args or child.args[0] not in ("options", "text"):
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f'kind must be "options" or "text"'
                )
            kind = child.args[0]
        elif cname == "optional":
            if not child.args or not isinstance(child.args[0], bool):
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f"optional must be a boolean"
                )
            optional = child.args[0]
        elif cname == "multi-select":
            if not child.args or not isinstance(child.args[0], bool):
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f"multi-select must be a boolean"
                )
            multi_select = child.args[0]
        elif cname == "options":
            if not child.args:
                raise ConfigError(
                    f"question '{key}' in {block_desc} of {source}: "
                    f"options needs at least one value"
                )
            for arg in child.args:
                if not isinstance(arg, str):
                    raise ConfigError(
                        f"question '{key}' in {block_desc} of {source}: "
                        f"option values must be strings"
                    )
                options.append(QuestionOption(label=arg))
        else:
            raise ConfigError(
                f"question '{key}' in {block_desc} of {source}: "
                f"unknown child '{cname}'"
            )

    if prompt is None:
        raise ConfigError(
            f"question '{key}' in {block_desc} of {source}: prompt is required"
        )
    if kind == "text" and options:
        raise ConfigError(
            f"question '{key}' in {block_desc} of {source}: "
            f"text-kind questions cannot declare options"
        )
    if kind == "options" and not options:
        raise ConfigError(
            f"question '{key}' in {block_desc} of {source}: "
            f"options-kind questions need an options list"
        )

    if header is None:
        # Default: key, hyphens → spaces, title-cased, trimmed to 12 chars for
        # AskUserQuestion compatibility.
        header = key.replace("-", " ").title()[:12]

    return Question(
        key=key,
        prompt=prompt,
        header=header,
        options=options,
        kind=kind,
        optional=optional,
        multi_select=multi_select,
    )


def _parse_configure_block(node, agent_name: str, source: Path) -> ConfigureBlock:
    model: Optional[str] = None
    if node.args:
        if len(node.args) > 1:
            raise ConfigError(
                f"configure block in agent '{agent_name}' in {source} "
                f"takes at most one model arg"
            )
        if not isinstance(node.args[0], str) or not node.args[0]:
            raise ConfigError(
                f"configure block in agent '{agent_name}' in {source}: "
                f"model must be a non-empty string"
            )
        model = node.args[0]
    block_desc = (
        f'agent "{agent_name}" configure "{model}"'
        if model is not None
        else f'agent "{agent_name}" configure'
    )
    questions: List[Question] = []
    seen_keys: set[str] = set()
    for child in node.nodes:
        if child.name != "question":
            raise ConfigError(
                f"{block_desc} in {source}: unknown child '{child.name}'"
            )
        q = _parse_question(child, block_desc, source)
        if q.key in seen_keys:
            raise ConfigError(
                f"{block_desc} in {source}: duplicate question '{q.key}'"
            )
        seen_keys.add(q.key)
        questions.append(q)
    if not questions:
        raise ConfigError(
            f"{block_desc} in {source}: block is empty — declare at least "
            f"one question, or remove the block to fall back to built-ins"
        )
    return ConfigureBlock(model=model, questions=questions)


def _parse_agent(node, source: Path) -> AgentSettings:
    if not node.args or not isinstance(node.args[0], str) or not node.args[0]:
        raise ConfigError(
            f"agent block in {source} needs a non-empty name "
            f'(e.g. `agent "claude" {{ ... }}`)'
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"agent '{node.args[0]}' in {source} has extra positional args"
        )
    name = node.args[0]
    s = AgentSettings(name=name)
    for child in node.nodes:
        if child.name == "configure":
            block = _parse_configure_block(child, name, source)
            if block.model in s.configure_blocks:
                label = f'"{block.model}"' if block.model else "(default)"
                raise ConfigError(
                    f"agent '{name}' in {source}: duplicate configure block {label}"
                )
            s.configure_blocks[block.model] = block
        # Other children (default-config, hooks, alias) — silently ignored
        # for forward-compat with tickets #30 / #31 / #33.
    return s


def _parse_kdl_file(path: Path) -> AppConfig:
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        doc = kdl.parse(text)
    except kdl.ParseError as e:
        raise ConfigError(f"parse error in {path}: {e}") from e
    cfg = AppConfig()
    for node in doc.nodes:
        if node.name == "template":
            tpl = _parse_template(node, path)
            if tpl.name in cfg.templates:
                raise ConfigError(
                    f"duplicate template '{tpl.name}' in {path}"
                )
            cfg.templates[tpl.name] = tpl
        elif node.name == "configure-default":
            if not node.args or not isinstance(node.args[0], str):
                raise ConfigError(
                    f"configure-default in {path} must be a string: "
                    f'configure-default "on" | "off"'
                )
            if len(node.args) > 1:
                raise ConfigError(
                    f"configure-default in {path} accepts exactly one argument"
                )
            if getattr(node, "props", None):
                raise ConfigError(
                    f"configure-default in {path} does not accept properties"
                )
            value = node.args[0]
            if value not in ("on", "off"):
                raise ConfigError(
                    f"configure-default in {path} must be \"on\" or \"off\", "
                    f"got {value!r}"
                )
            cfg.configure_default = value
            cfg.configure_default_set = True
        elif node.name == "agent":
            agent = _parse_agent(node, path)
            if agent.name in cfg.agents:
                raise ConfigError(
                    f"duplicate agent block for '{agent.name}' in {path}"
                )
            cfg.agents[agent.name] = agent
        # Silently ignore hooks / alias — those belong to follow-up tickets
        # #30 / #33 and are not implemented here.
    return cfg


def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)
    merged_agents: Dict[str, AgentSettings] = dict(base.agents)
    for name, over_agent in over.agents.items():
        if name in merged_agents:
            merged_blocks = dict(merged_agents[name].configure_blocks)
            merged_blocks.update(over_agent.configure_blocks)
            merged_agents[name] = AgentSettings(
                name=name, configure_blocks=merged_blocks
            )
        else:
            merged_agents[name] = over_agent
    # configure_default: if `over` explicitly set it, that wins (either
    # value). Otherwise inherit from `base` including its "set" flag.
    if over.configure_default_set:
        merged_default = over.configure_default
        merged_default_set = True
    else:
        merged_default = base.configure_default
        merged_default_set = base.configure_default_set
    return AppConfig(
        templates=merged_templates,
        agents=merged_agents,
        configure_default=merged_default,
        configure_default_set=merged_default_set,
    )


def load_config(
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
) -> AppConfig:
    """Load user + project settings.kdl and return the merged AppConfig.

    - cwd defaults to Path.cwd()
    - home defaults to Path(os.environ['HOME']); if HOME is unset,
      the user config step is skipped.

    Missing files are skipped silently. Malformed files raise ConfigError.
    """
    if cwd is None:
        cwd = Path.cwd()
    if home is None:
        env_home = os.environ.get("HOME")
        home = Path(env_home) if env_home else None

    merged = AppConfig()

    user_path: Optional[Path] = None
    if home is not None:
        user_path = home / ".actor" / "settings.kdl"
        if user_path.is_file():
            merged = _merge(merged, _parse_kdl_file(user_path))

    project_path = _find_project_config(cwd, user_path=user_path)
    if project_path is not None:
        merged = _merge(merged, _parse_kdl_file(project_path))

    return merged
