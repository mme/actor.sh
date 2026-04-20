"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map and lifecycle hooks. Project config overrides user config
on same-key conflict (templates by name, hooks per event).

Out of scope (see tickets #31 / #33): per-agent default-config blocks,
aliases. Their nodes are skipped at parse time without error for forward
compatibility.
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
class Hooks:
    on_start: Optional[str] = None
    on_run: Optional[str] = None
    on_discard: Optional[str] = None


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)


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


_HOOK_KEYS = {
    "on-start": "on_start",
    "on-run": "on_run",
    "on-discard": "on_discard",
}


def _parse_hooks(node, source: Path) -> Hooks:
    if node.args:
        raise ConfigError(
            f"hooks block in {source} does not accept positional args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"hooks block in {source} does not accept properties"
        )
    hooks = Hooks()
    seen: set[str] = set()
    for child in node.nodes:
        key = child.name
        attr = _HOOK_KEYS.get(key)
        if attr is None:
            raise ConfigError(
                f"hooks block in {source}: unknown hook '{key}' "
                f"(valid: on-start, on-run, on-discard)"
            )
        if key in seen:
            raise ConfigError(
                f"hooks block in {source}: duplicate hook '{key}'"
            )
        seen.add(key)
        if not child.args:
            raise ConfigError(
                f"hooks block in {source}: '{key}' needs a value"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"hooks block in {source}: '{key}' has extra args "
                f"(only a single shell command is supported)"
            )
        if getattr(child, "props", None):
            raise ConfigError(
                f"hooks block in {source}: '{key}' does not accept properties"
            )
        raw = child.args[0]
        if not isinstance(raw, str):
            raise ConfigError(
                f"hooks block in {source}: '{key}' must be a string"
            )
        setattr(hooks, attr, raw)
    return hooks


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
    hooks_seen = False
    for node in doc.nodes:
        if node.name == "template":
            tpl = _parse_template(node, path)
            if tpl.name in cfg.templates:
                raise ConfigError(
                    f"duplicate template '{tpl.name}' in {path}"
                )
            cfg.templates[tpl.name] = tpl
        elif node.name == "hooks":
            if hooks_seen:
                raise ConfigError(
                    f"duplicate hooks block in {path}"
                )
            hooks_seen = True
            cfg.hooks = _parse_hooks(node, path)
        # Silently ignore agent / alias — those belong to follow-up
        # tickets #31 / #33 and are not implemented here.
    return cfg


def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)
    merged_hooks = Hooks(
        on_start=over.hooks.on_start if over.hooks.on_start is not None else base.hooks.on_start,
        on_run=over.hooks.on_run if over.hooks.on_run is not None else base.hooks.on_run,
        on_discard=over.hooks.on_discard if over.hooks.on_discard is not None else base.hooks.on_discard,
    )
    return AppConfig(templates=merged_templates, hooks=merged_hooks)


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
