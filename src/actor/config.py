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
        # Silently ignore hooks / agent / alias — those belong to follow-up
        # tickets #30 / #31 / #33 and are not implemented here.
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
    # configure_default: a non-default value in `over` overrides. If `over`
    # didn't set it, it's still "on" (the field default), so this preserves
    # whatever `base` had.
    merged_default = (
        over.configure_default
        if over.configure_default != "on"
        else base.configure_default
    )
    return AppConfig(
        templates=merged_templates,
        agents=merged_agents,
        configure_default=merged_default,
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
