"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map and per-agent defaults. Project config overrides user
config per-key.

Out of scope (see tickets #30 / #33): hooks, aliases. Their nodes are
skipped at parse time without error for forward compatibility.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

import kdl

from .errors import ConfigError


_VALID_AGENTS: Tuple[str, ...] = ("claude", "codex")


def _actor_keys_whitelist(agent_name: str) -> FrozenSet[str]:
    """Return the set of valid flat (non-defaults) keys for an agent.

    Deferred import so config.py stays importable before agents subpackage
    finishes loading, and to keep the whitelist in a single source of truth
    (the agent class's `ACTOR_DEFAULTS` dict)."""
    from .agents.claude import ClaudeAgent
    from .agents.codex import CodexAgent

    if agent_name == "claude":
        return frozenset(ClaudeAgent.ACTOR_DEFAULTS.keys())
    if agent_name == "codex":
        return frozenset(CodexAgent.ACTOR_DEFAULTS.keys())
    return frozenset()


@dataclass
class Template:
    name: str
    agent: Optional[str] = None
    prompt: Optional[str] = None
    config: Dict[str, str] = field(default_factory=dict)


@dataclass
class AgentDefaults:
    """Per-agent baseline from settings.kdl.

    `actor_keys` are flat keys interpreted by actor-sh (e.g. env filtering).
    `agent_args` come from the `defaults { }` sub-block and map directly to
    the agent binary's CLI flags. Values of `None` mean "unset" — they cancel
    a lower-precedence value during merge.
    """
    actor_keys: Dict[str, Optional[str]] = field(default_factory=dict)
    agent_args: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    agent_defaults: Dict[str, AgentDefaults] = field(default_factory=dict)


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


def _coerce_value_or_none(value: object) -> Optional[str]:
    """Like `_coerce_value` but maps KDL `null` (Python `None`) → `None`.

    Only used inside `agent { }` blocks, where null explicitly means "unset"
    and cancels a lower-precedence value during merge."""
    if value is None:
        return None
    return _coerce_value(value)


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
        if key == "defaults":
            raise ConfigError(
                f"template '{name}' in {source}: `defaults {{ ... }}` "
                f"blocks belong under `agent \"claude\" {{ ... }}` / "
                f"`agent \"codex\" {{ ... }}`, not inside a template"
            )
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
        if raw is None:
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' cannot be null "
                f"(templates set values; `null` only makes sense inside "
                f"`agent \"...\" {{ ... }}` blocks as a cancel marker)"
            )
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


def _parse_agent_block(node, source: Path) -> Tuple[str, AgentDefaults]:
    """Parse an `agent "<name>" { ... }` node.

    Shape: flat children are actor-keys (whitelisted against the agent's
    `ACTOR_DEFAULTS`), a single nested `defaults { ... }` block holds
    agent-arg keys, and null values survive as Python `None` so the merge
    step can use them to cancel lower-precedence values."""
    if not node.args:
        raise ConfigError(
            f"agent block in {source} must have a name "
            f"(e.g. `agent \"claude\" {{ ... }}`)"
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"agent block in {source} has extra positional args"
        )
    raw_name = node.args[0]
    if not isinstance(raw_name, str) or not raw_name:
        raise ConfigError(
            f"agent block in {source} must have a non-empty string name"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"agent '{raw_name}' in {source} does not accept properties"
        )
    if raw_name not in _VALID_AGENTS:
        raise ConfigError(
            f"unknown agent '{raw_name}' in {source} "
            f"(valid: {', '.join(_VALID_AGENTS)})"
        )

    whitelist = _actor_keys_whitelist(raw_name)
    defaults = AgentDefaults()
    seen_flat: set[str] = set()
    seen_defaults_block = False
    for child in node.nodes:
        key = child.name
        if key == "defaults":
            if seen_defaults_block:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"duplicate `defaults` block"
                )
            seen_defaults_block = True
            if child.args:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"`defaults` block must not have positional args"
                )
            if getattr(child, "props", None):
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"`defaults` block does not accept properties"
                )
            if not child.nodes:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"`defaults` block must have at least one key "
                    f"(remove the block if you meant to unset nothing)"
                )
            seen_args: set[str] = set()
            for gc in child.nodes:
                if gc.name == "defaults":
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"nested `defaults` block not allowed"
                    )
                if gc.name in seen_args:
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"duplicate defaults key '{gc.name}'"
                    )
                seen_args.add(gc.name)
                if not gc.args:
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"defaults key '{gc.name}' needs a value "
                        f"(use `null` to unset)"
                    )
                if len(gc.args) > 1:
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"defaults key '{gc.name}' has extra args"
                    )
                if getattr(gc, "props", None):
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"defaults key '{gc.name}' does not accept properties"
                    )
                if gc.nodes:
                    raise ConfigError(
                        f"agent '{raw_name}' in {source}: "
                        f"defaults key '{gc.name}' must be a leaf value, "
                        f"not a block"
                    )
                defaults.agent_args[gc.name] = _coerce_value_or_none(gc.args[0])
        else:
            if key in seen_flat:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"duplicate key '{key}'"
                )
            seen_flat.add(key)
            if key not in whitelist:
                valid = ", ".join(sorted(whitelist)) if whitelist else "(none)"
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"unknown flat key '{key}' "
                    f"(valid flat keys: {valid}; agent CLI flags belong "
                    f"under `defaults {{ ... }}`)"
                )
            if not child.args:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"'{key}' needs a value (use `null` to unset)"
                )
            if len(child.args) > 1:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"'{key}' has extra args"
                )
            if getattr(child, "props", None):
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"'{key}' does not accept properties"
                )
            if child.nodes:
                raise ConfigError(
                    f"agent '{raw_name}' in {source}: "
                    f"'{key}' must be a leaf value, not a block"
                )
            defaults.actor_keys[key] = _coerce_value_or_none(child.args[0])
    return raw_name, defaults


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
        elif node.name == "agent":
            name, defaults = _parse_agent_block(node, path)
            if name in cfg.agent_defaults:
                raise ConfigError(
                    f"duplicate agent block '{name}' in {path}"
                )
            cfg.agent_defaults[name] = defaults
        # Silently ignore hooks / alias — those belong to follow-up
        # tickets #30 / #33 and are not implemented here.
    return cfg


def _merge_dict(
    low: Dict[str, Optional[str]],
    high: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """Per-key overlay that preserves `None` as a cancel marker.

    Higher-layer values overwrite lower-layer values. `None` is kept in the
    result so the kdl layer can cancel values set further down the precedence
    ladder (specifically the class-level `AGENT_DEFAULTS` / `ACTOR_DEFAULTS`
    baked into `cmd_new`). The actual cancel happens when `cmd_new` walks the
    merged dict and pops any key whose value is `None`."""
    out: Dict[str, Optional[str]] = dict(low)
    for k, v in high.items():
        out[k] = v
    return out


def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)
    merged_defaults: Dict[str, AgentDefaults] = {}
    for agent in set(base.agent_defaults) | set(over.agent_defaults):
        b = base.agent_defaults.get(agent, AgentDefaults())
        o = over.agent_defaults.get(agent, AgentDefaults())
        merged_defaults[agent] = AgentDefaults(
            actor_keys=_merge_dict(b.actor_keys, o.actor_keys),
            agent_args=_merge_dict(b.agent_args, o.agent_args),
        )
    # Drop entries where everything cancelled out so callers can use
    # `agent not in cfg.agent_defaults` as "no overrides from kdl".
    merged_defaults = {
        k: v for k, v in merged_defaults.items()
        if v.actor_keys or v.agent_args
    }
    return AppConfig(
        templates=merged_templates,
        agent_defaults=merged_defaults,
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
