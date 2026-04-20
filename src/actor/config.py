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
from typing import Dict, Optional

import kdl

from .errors import ConfigError


@dataclass
class Template:
    name: str
    agent: Optional[str] = None
    prompt: Optional[str] = None
    config: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)


def _find_project_config(start: Path) -> Optional[Path]:
    """Walk up from start looking for .actor/settings.kdl."""
    try:
        start = start.resolve(strict=False)
    except OSError:
        return None
    for d in [start, *start.parents]:
        p = d / ".actor" / "settings.kdl"
        if p.is_file():
            return p
    return None


def _coerce_value(value: object) -> str:
    """Coerce a KDL-parsed arg (bool/float/str) into a string for the
    stringly-typed actor config pipeline."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # kdl-py parses all numbers as float; drop the trailing .0 for
        # integer-valued literals so `effort 5` stays "5" not "5.0".
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _parse_template(node, source: Path) -> Template:
    if not node.args or not isinstance(node.args[0], str):
        raise ConfigError(
            f"template block in {source} must have a name "
            f"(e.g. `template \"qa\" {{ ... }}`)"
        )
    name = node.args[0]
    tpl = Template(name=name)
    for child in node.nodes:
        key = child.name
        if not child.args:
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' needs a value"
            )
        value_str = _coerce_value(child.args[0])
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
        raise ConfigError(f"could not read {path}: {e}")
    try:
        doc = kdl.parse(text)
    except Exception as e:
        raise ConfigError(f"parse error in {path}: {e}")
    cfg = AppConfig()
    for node in doc.nodes:
        if node.name == "template":
            tpl = _parse_template(node, path)
            cfg.templates[tpl.name] = tpl
        # Silently ignore hooks / agent / alias — those belong to follow-up
        # tickets #30 / #31 / #33 and are not implemented here.
    return cfg


def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)
    return AppConfig(templates=merged_templates)


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

    if home is not None:
        user_path = home / ".actor" / "settings.kdl"
        if user_path.is_file():
            merged = _merge(merged, _parse_kdl_file(user_path))

    project_path = _find_project_config(cwd)
    if project_path is not None:
        merged = _merge(merged, _parse_kdl_file(project_path))

    return merged
