"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
roles map and per-agent defaults. Project config overrides user
config per-key.

Out of scope (see ticket #33): aliases. Unknown top-level nodes are
skipped at parse time without error for forward compatibility with
follow-up features like `ask { }` (#52).
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
class Role:
    name: str
    agent: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    config: Dict[str, str] = field(default_factory=dict)


@dataclass
class AgentDefaults:
    """Per-agent baseline from a `defaults "<agent>" { ... }` block.

    The block's flat children are partitioned at parse time by the agent
    class's `ACTOR_DEFAULTS` whitelist:
      - `actor_keys` — interpreted by actor-sh (e.g. env filtering).
      - `agent_args` — forwarded to the agent CLI as flags.

    Values of `None` mean "unset" — they cancel a lower-precedence value
    during merge.
    """
    actor_keys: Dict[str, Optional[str]] = field(default_factory=dict)
    agent_args: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class Hooks:
    on_start: Optional[str] = None
    before_run: Optional[str] = None
    after_run: Optional[str] = None
    on_discard: Optional[str] = None


@dataclass
class Ask:
    """User-configurable guidance strings appended to MCP tool descriptions.

    `values` maps a kdl key (e.g. `"on-start"`) to its setting:
      - key absent             → use the hardcoded default for that key
      - value is a string      → append that string verbatim to the tool doc
      - value is `None` or ""  → user opted out; append nothing

    Resolution happens at MCP-server startup; tool descriptions are then
    static for the server's lifetime. Edit settings.kdl + restart `actor
    main` to change them.
    """
    values: Dict[str, Optional[str]] = field(default_factory=dict)

    def resolved(self, key: str, default: str) -> str:
        if key not in self.values:
            return default
        v = self.values[key]
        return v if v else ""


@dataclass
class AppConfig:
    roles: Dict[str, Role] = field(default_factory=dict)
    agent_defaults: Dict[str, AgentDefaults] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)
    ask: Ask = field(default_factory=Ask)


_HOOK_KEYS = {
    "on-start": "on_start",
    "before-run": "before_run",
    "after-run": "after_run",
    "on-discard": "on_discard",
}


# Whitelist of valid keys inside an `ask { }` block. Mirrors the hooks
# vocabulary minus `after-run` — there's no point asking the user a
# question after a run has already finished.
_ASK_KEYS: Tuple[str, ...] = ("on-start", "before-run", "on-discard")


# Hardcoded defaults appended to MCP tool descriptions when the
# user hasn't set a value in `ask { }`. Each one is the orchestrator's
# baseline guidance for "should I ask the user something before this
# tool call?". Users can override per-key in settings.kdl, or silence
# with `null` / `""`.
ASK_DEFAULTS: Dict[str, str] = {
    "on-start": (
        "Before calling `new_actor`, use `AskUserQuestion` to surface any "
        "parameter choices that would meaningfully affect the actor's "
        "behavior. Structural params (`agent`, `base`, `no_worktree`, "
        "`use_subscription`) are visible on the tool itself; for agent "
        "config keys (thinking effort, permission mode, allowed tools, "
        "etc.) consult the agent's config reference — `claude-config.md` "
        "for claude, `codex-config.md` for codex. Only ask when the "
        "user's request leaves the choice genuinely ambiguous — skip "
        "questions whose answer is already clear from context or whose "
        "default is fine. Keep it to at most 2-3 questions; batch them "
        "into a single `AskUserQuestion` call when possible."
    ),
    "before-run": (
        "Before calling `run_actor`, use `AskUserQuestion` only when the "
        "prompt is genuinely vague (e.g. \"try again\" or \"continue\" "
        "with no context) or when the user signals they want per-run "
        "config tweaks — thinking effort, model, etc. — different from "
        "the actor's defaults. Those go through the `config` param and "
        "only apply to this run. Consult the agent's config reference "
        "(`claude-config.md` / `codex-config.md`) for valid overrides. "
        "Default to proceeding without asking — most run calls don't "
        "need a question. Keep it to at most 1-2 questions when you do "
        "ask, batched."
    ),
    "on-discard": (
        "Before calling `discard_actor`, use `AskUserQuestion` only when "
        "the user's target is ambiguous (\"discard it\" with multiple "
        "candidates in play) or when there's a running session they "
        "might want to inspect first (check with `show_actor`). Discard "
        "is usually a cleanup operation — default to proceeding when "
        "the target is explicit."
    ),
}


# Built-in roles that exist without a settings.kdl. Layered as the lowest
# precedence in `load_config` so a user's `role "main" { ... }` block (or a
# project's) replaces the entry wholesale — there's no per-field merge for
# roles. To delete a built-in, redefine it in settings.kdl with the fields
# you want; there is no `null` cancel for whole roles.
#
# Prompt bodies live as separate `.md` files under `actor.role_prompts/`
# rather than as Python string literals — they're long, frequently edited,
# and reading them as Markdown beats wrestling with quote escaping. Loaded
# via `importlib.resources` so they ship as package data with the wheel.


def _load_builtin_prompt(name: str) -> str:
    from importlib.resources import files
    return (files("actor.role_prompts") / f"{name}.md").read_text().rstrip()


def _default_roles() -> Dict[str, "Role"]:
    return {
        "main": Role(
            name="main",
            agent="claude",
            description="Default actor.sh main actor.",
            prompt=_load_builtin_prompt("main"),
        ),
    }


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


def _parse_role(node, source: Path) -> Role:
    if not node.args or not isinstance(node.args[0], str):
        raise ConfigError(
            f"role block in {source} must have a name "
            f"(e.g. `role \"qa\" {{ ... }}`)"
        )
    if not node.args[0]:
        raise ConfigError(
            f"role block in {source} must have a non-empty name"
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"role '{node.args[0]}' in {source} has extra positional args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"role '{node.args[0]}' in {source} does not accept properties"
        )
    name = node.args[0]
    role = Role(name=name)
    seen_keys: set[str] = set()
    for child in node.nodes:
        key = child.name
        if key == "defaults":
            raise ConfigError(
                f"role '{name}' in {source}: `defaults` is reserved "
                f"for per-agent defaults at the top level "
                f"(`defaults \"claude\" {{ ... }}` / "
                f"`defaults \"codex\" {{ ... }}`), not as a child of "
                f"`role`. Roles have a flat namespace already "
                f"— put your keys directly under `role \"{name}\" "
                f"{{ ... }}`."
            )
        if key in seen_keys:
            raise ConfigError(
                f"role '{name}' in {source}: duplicate key '{key}'"
            )
        seen_keys.add(key)
        if not child.args:
            raise ConfigError(
                f"role '{name}' in {source}: '{key}' needs a value"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"role '{name}' in {source}: '{key}' has extra args "
                f"(only a single value is supported)"
            )
        if getattr(child, "props", None):
            raise ConfigError(
                f"role '{name}' in {source}: '{key}' does not accept properties"
            )
        raw = child.args[0]
        if raw is None:
            raise ConfigError(
                f"role '{name}' in {source}: '{key}' cannot be null "
                f"(roles set values; `null` only makes sense inside "
                f"`defaults \"...\" {{ ... }}` blocks as a cancel marker)"
            )
        if key in ("agent", "prompt", "description") and not isinstance(raw, str):
            raise ConfigError(
                f"role '{name}' in {source}: '{key}' must be a string"
            )
        value_str = _coerce_value(raw)
        if key == "agent":
            role.agent = value_str
        elif key == "prompt":
            role.prompt = value_str
        elif key == "description":
            role.description = value_str
        else:
            role.config[key] = value_str
    return role


def _parse_defaults_block(node, source: Path) -> Tuple[str, AgentDefaults]:
    """Parse a `defaults "<agent>" { ... }` node.

    Single flat namespace — every child is a key/value pair, partitioned
    at parse time by the agent class's `ACTOR_DEFAULTS` whitelist:
      - whitelisted keys → `actor_keys` (interpreted by actor-sh)
      - everything else  → `agent_args` (forwarded to the agent CLI)

    Same partition logic that roles use. `null` values survive as Python
    `None` so the merge step can use them to cancel lower-precedence
    values."""
    if not node.args:
        raise ConfigError(
            f"defaults block in {source} must have an agent name "
            f"(e.g. `defaults \"claude\" {{ ... }}`)"
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"defaults block in {source} has extra positional args"
        )
    raw_name = node.args[0]
    if not isinstance(raw_name, str) or not raw_name:
        raise ConfigError(
            f"defaults block in {source} must have a non-empty string name"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"defaults '{raw_name}' in {source} does not accept properties"
        )
    if raw_name not in _VALID_AGENTS:
        raise ConfigError(
            f"unknown agent '{raw_name}' in {source} "
            f"(valid: {', '.join(_VALID_AGENTS)})"
        )

    whitelist = _actor_keys_whitelist(raw_name)
    defaults = AgentDefaults()
    seen: set[str] = set()
    for child in node.nodes:
        key = child.name
        if key in seen:
            raise ConfigError(
                f"defaults '{raw_name}' in {source}: "
                f"duplicate key '{key}'"
            )
        seen.add(key)
        if not child.args:
            raise ConfigError(
                f"defaults '{raw_name}' in {source}: "
                f"'{key}' needs a value (use `null` to unset)"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"defaults '{raw_name}' in {source}: "
                f"'{key}' has extra args"
            )
        if getattr(child, "props", None):
            raise ConfigError(
                f"defaults '{raw_name}' in {source}: "
                f"'{key}' does not accept properties"
            )
        if child.nodes:
            raise ConfigError(
                f"defaults '{raw_name}' in {source}: "
                f"'{key}' must be a leaf value, not a block "
                f"(all keys live in one flat namespace)"
            )
        value = _coerce_value_or_none(child.args[0])
        if key in whitelist:
            defaults.actor_keys[key] = value
        else:
            defaults.agent_args[key] = value
    return raw_name, defaults


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
                f"(valid: {', '.join(_HOOK_KEYS)})"
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


def _parse_ask_block(node, source: Path) -> Ask:
    """Parse a top-level `ask { ... }` block.

    Each child is a single key/value pair where the value is either a
    string (custom guidance) or `null` (silence the default). The
    whitelist (`_ASK_KEYS`) gates which keys are allowed; unknown keys
    are rejected with a hint listing the valid set.
    """
    if node.args:
        raise ConfigError(
            f"ask block in {source} does not accept positional args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"ask block in {source} does not accept properties"
        )
    ask = Ask()
    seen: set[str] = set()
    for child in node.nodes:
        key = child.name
        if key not in _ASK_KEYS:
            raise ConfigError(
                f"ask block in {source}: unknown key '{key}' "
                f"(valid: {', '.join(_ASK_KEYS)})"
            )
        if key in seen:
            raise ConfigError(
                f"ask block in {source}: duplicate key '{key}'"
            )
        seen.add(key)
        if not child.args:
            raise ConfigError(
                f"ask block in {source}: '{key}' needs a value "
                f"(string, or `null` to silence the default)"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"ask block in {source}: '{key}' has extra args "
                f"(only a single value is supported)"
            )
        if getattr(child, "props", None):
            raise ConfigError(
                f"ask block in {source}: '{key}' does not accept properties"
            )
        raw = child.args[0]
        if raw is None:
            ask.values[key] = None
            continue
        if not isinstance(raw, str):
            raise ConfigError(
                f"ask block in {source}: '{key}' must be a string or null"
            )
        ask.values[key] = raw
    return ask


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
    ask_seen = False
    for node in doc.nodes:
        if node.name == "role":
            role = _parse_role(node, path)
            if role.name in cfg.roles:
                raise ConfigError(
                    f"duplicate role '{role.name}' in {path}"
                )
            cfg.roles[role.name] = role
        elif node.name == "defaults":
            name, defaults = _parse_defaults_block(node, path)
            if name in cfg.agent_defaults:
                raise ConfigError(
                    f"duplicate defaults block '{name}' in {path}"
                )
            cfg.agent_defaults[name] = defaults
        elif node.name == "hooks":
            if hooks_seen:
                raise ConfigError(
                    f"duplicate hooks block in {path}"
                )
            hooks_seen = True
            cfg.hooks = _parse_hooks(node, path)
        elif node.name == "ask":
            if ask_seen:
                raise ConfigError(
                    f"duplicate ask block in {path}"
                )
            ask_seen = True
            cfg.ask = _parse_ask_block(node, path)
        # Silently ignore alias — follow-up ticket (#33) — to keep the
        # parser lenient for forward compat with future top-level blocks.
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
    merged_roles = dict(base.roles)
    merged_roles.update(over.roles)
    merged_defaults: Dict[str, AgentDefaults] = {}
    for agent in set(base.agent_defaults) | set(over.agent_defaults):
        b = base.agent_defaults.get(agent, AgentDefaults())
        o = over.agent_defaults.get(agent, AgentDefaults())
        merged_defaults[agent] = AgentDefaults(
            actor_keys=_merge_dict(b.actor_keys, o.actor_keys),
            agent_args=_merge_dict(b.agent_args, o.agent_args),
        )
    # Drop entries that ended up with no keys at all (e.g. a defaults
    # block written as `defaults "claude" { }`) so callers can treat
    # `agent not in cfg.agent_defaults` as "no mention in kdl". Entries
    # whose only values are `None` cancel markers are retained — cmd_new
    # needs them to cancel class-level defaults.
    merged_defaults = {
        k: v for k, v in merged_defaults.items()
        if v.actor_keys or v.agent_args
    }
    merged_hooks = Hooks(
        on_start=over.hooks.on_start if over.hooks.on_start is not None else base.hooks.on_start,
        before_run=over.hooks.before_run if over.hooks.before_run is not None else base.hooks.before_run,
        after_run=over.hooks.after_run if over.hooks.after_run is not None else base.hooks.after_run,
        on_discard=over.hooks.on_discard if over.hooks.on_discard is not None else base.hooks.on_discard,
    )
    # Per-key overlay: project's explicit set of an ask key wins. Absence
    # in `over` means "didn't mention it", so the base value (or ultimately
    # the hardcoded default) is preserved.
    merged_ask = Ask(values={**base.ask.values, **over.ask.values})
    return AppConfig(
        roles=merged_roles,
        agent_defaults=merged_defaults,
        hooks=merged_hooks,
        ask=merged_ask,
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

    merged = AppConfig(roles=_default_roles())

    user_path: Optional[Path] = None
    if home is not None:
        user_path = home / ".actor" / "settings.kdl"
        if user_path.is_file():
            merged = _merge(merged, _parse_kdl_file(user_path))

    project_path = _find_project_config(cwd, user_path=user_path)
    if project_path is not None:
        merged = _merge(merged, _parse_kdl_file(project_path))

    return merged
