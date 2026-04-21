"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map and per-agent default-config overrides. Project config
overrides user config on same-key conflict.

Out of scope (see tickets #30 / #33): hooks, aliases. Their nodes are
skipped at parse time without error for forward compatibility. A
top-level `default-config` block is NOT silently skipped — it raises a
helpful error pointing users at the correct nesting under `agent`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import kdl

from .errors import ActorError, ConfigError
from .types import AgentKind


# Template-level field names — rejected inside `default-config` and
# when dropped bare under an `agent` block. Centralised so the two
# parser checks can't drift apart.
_RESERVED_TEMPLATE_KEYS = ("prompt", "agent")


@dataclass
class Template:
    name: str
    agent: Optional[str] = None
    prompt: Optional[str] = None
    config: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    # Parser invariant: every key present in `agent_defaults` maps to a
    # non-empty dict — an `agent "claude" { default-config {} }` block
    # contributes nothing, so the key is omitted entirely. `__post_init__`
    # enforces this on programmatic construction; `_merge` maintains it
    # across merges.
    agent_defaults: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.agent_defaults = {
            agent: keys for agent, keys in self.agent_defaults.items() if keys
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
        # Catch a common mix-up: `default-config {}` belongs under an
        # `agent "..."` block, not under a template. Check BEFORE the
        # duplicate-key check so a repeated misplacement still surfaces
        # the helpful redirect, and before the args check so that the
        # block shape (no args, only children) doesn't get reported as
        # "needs a value" — which hides the real fix.
        if key == "default-config":
            raise ConfigError(
                f"template '{name}' in {source}: `default-config` blocks "
                f"belong under `agent \"claude\" {{ … }}` / "
                f"`agent \"codex\" {{ … }}`, not inside a template"
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
        if key in ("agent", "prompt") and not isinstance(raw, str):
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' must be a string"
            )
        value_str = _coerce_value(raw)
        if key == "agent":
            # Validate against AgentKind here (not at cmd_new time) so a
            # typo in settings.kdl surfaces as a ConfigError with the
            # offending path — matches the agent-block validator.
            try:
                AgentKind.from_str(value_str)
            except ActorError as e:
                valid = ", ".join(k.value for k in AgentKind)
                raise ConfigError(
                    f"template '{name}' in {source}: unknown agent "
                    f"'{value_str}' (valid: {valid})"
                ) from e
            tpl.agent = value_str
        elif key == "prompt":
            tpl.prompt = value_str
        else:
            tpl.config[key] = value_str
    return tpl


def _parse_default_config(node, agent_name: str, source: Path) -> Dict[str, str]:
    """Parse a `default-config { … }` block inside an `agent` block.

    Same shape rules as template children: each child is `key "value"`
    with exactly one scalar arg, no props, no duplicates. Keys other
    than the template-level reserved names (`prompt`, `agent`) pass
    through — no fixed schema, they're consumed by the agent at spawn
    time. Using `prompt` or `agent` here raises; they belong on the
    template, not inside a block whose scope is already one agent.
    """
    if node.args:
        raise ConfigError(
            f"agent '{agent_name}' in {source}: `default-config` does not "
            f"accept positional arguments"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"agent '{agent_name}' in {source}: `default-config` does not "
            f"accept properties"
        )
    out: Dict[str, str] = {}
    seen_keys: set[str] = set()
    for child in node.nodes:
        key = child.name
        # Reject reserved keys BEFORE the duplicate check so `prompt "a"
        # / prompt "b"` reports the reserved-key error (which tells the
        # user the real fix — move `prompt` to the template) rather than
        # a misleading "duplicate prompt".
        if key in _RESERVED_TEMPLATE_KEYS:
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' is not a "
                f"valid default-config key (it's a template-level field, "
                f"not an agent config key)"
            )
        # Nested `default-config { default-config { … } }` is almost
        # always a misread of the schema — without this branch the
        # user gets a misleading "'default-config' needs a value"
        # message from the args check below.
        if key == "default-config":
            raise ConfigError(
                f"agent '{agent_name}' in {source}: `default-config` "
                f"cannot be nested inside another `default-config` — "
                f"list config keys directly"
            )
        if key in seen_keys:
            raise ConfigError(
                f"agent '{agent_name}' in {source}: duplicate key '{key}' "
                f"in default-config"
            )
        seen_keys.add(key)
        # Props check runs BEFORE the args check so `model name="opus"`
        # (which has no positional args, just a property) reports the
        # real issue — properties are rejected — instead of a
        # misleading "needs a value".
        if getattr(child, "props", None):
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' does not accept "
                f"properties"
            )
        if not child.args:
            # Block-shaped children (`model { nested "x" }`) trip this
            # branch with `args == []`. Distinguish that case so the
            # user isn't told their key "needs a value" when they did
            # supply one — just in an unsupported shape.
            if getattr(child, "nodes", None):
                raise ConfigError(
                    f"agent '{agent_name}' in {source}: '{key}' must be a "
                    f"scalar `{key} \"value\"` entry — nested blocks are "
                    f"not supported inside `default-config`"
                )
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' needs a value"
            )
        if len(child.args) > 1:
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' has extra args "
                f"(only a single value is supported)"
            )
        out[key] = _coerce_value(child.args[0])
    return out


def _parse_agent_block(node, source: Path) -> Tuple[str, Dict[str, str]]:
    """Parse an `agent "name" { … }` block. Returns (agent_name, defaults).

    Validates the agent name via `AgentKind.from_str` so typos
    (`agent "cluade"`) fail fast with the same canonical allowlist the
    rest of the codebase uses. Children with no positional args
    (`hooks { … }`, bare `alias`) are silently accepted as forward-compat
    no-ops for follow-up tickets. Key-with-value children directly under
    `agent` (e.g. `model "opus"` instead of
    `default-config { model "opus" }`) raise so the user's keys aren't
    silently dropped. Reserved template names (`prompt`, `agent`) raise
    regardless of shape and point the user at `template` blocks.
    """
    if not node.args:
        raise ConfigError(
            f"agent block in {source} must have a name "
            f"(e.g. `agent \"claude\" {{ … }}`)"
        )
    if not isinstance(node.args[0], str):
        raise ConfigError(
            f"agent block in {source}: name must be a string, "
            f"got {type(node.args[0]).__name__}"
        )
    if not node.args[0]:
        raise ConfigError(
            f"agent block in {source} must have a non-empty name"
        )
    if len(node.args) > 1:
        raise ConfigError(
            f"agent '{node.args[0]}' in {source} has extra positional args"
        )
    if getattr(node, "props", None):
        raise ConfigError(
            f"agent '{node.args[0]}' in {source} does not accept properties"
        )
    name = node.args[0]
    try:
        AgentKind.from_str(name)
    except ActorError as e:
        valid = ", ".join(k.value for k in AgentKind)
        raise ConfigError(
            f"agent block in {source}: unknown agent '{name}' "
            f"(valid: {valid})"
        ) from e

    defaults: Dict[str, str] = {}
    seen_default_config = False
    for child in node.nodes:
        if child.name == "default-config":
            if seen_default_config:
                raise ConfigError(
                    f"agent '{name}' in {source}: multiple `default-config` "
                    f"blocks (only one is allowed)"
                )
            seen_default_config = True
            defaults = _parse_default_config(child, name, source)
            continue
        # `prompt` / `agent` are template-level fields, not config keys.
        # Catch them regardless of shape (`prompt "x"`, `prompt { ... }`,
        # or bare `prompt`) — pointing the user at `default-config` is
        # actively wrong because those names are rejected there too.
        if child.name in _RESERVED_TEMPLATE_KEYS:
            raise ConfigError(
                f"agent '{name}' in {source}: `{child.name}` is a "
                f"template-level field, not an agent config key — "
                f"put it on a `template` block instead"
            )
        # Mirror of the template-side guard: a user who writes
        # `model "opus"` directly under `agent "claude" { … }` (forgetting
        # the `default-config { … }` wrapper) would otherwise see their
        # key silently dropped. Children with no args (bare `hooks` or
        # `hooks { … }` blocks) stay silently accepted as forward-compat
        # no-ops for follow-up tickets (e.g. #30).
        if child.args:
            raise ConfigError(
                f"agent '{name}' in {source}: `{child.name}` cannot sit "
                f"directly under an `agent` block — nest config keys "
                f"inside `default-config {{ … }}`"
            )
    return name, defaults


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
    # Track seen agent names separately from `cfg.agent_defaults`, which
    # only records agents that contributed at least one key. Without the
    # separate set, a first empty `agent "claude" { … }` block would not
    # be detected as a duplicate when a later block declared real keys.
    seen_agents: set[str] = set()
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
            if name in seen_agents:
                raise ConfigError(
                    f"duplicate agent block '{name}' in {path}"
                )
            seen_agents.add(name)
            # An agent block with no `default-config` child contributes
            # nothing — preserve the invariant "presence in
            # agent_defaults implies at least one declared key".
            if defaults:
                cfg.agent_defaults[name] = defaults
        elif node.name == "default-config":
            # Symmetric with the template-side guard: a top-level
            # `default-config` block is a common misread of the schema.
            # Without this branch it would fall through to the
            # "forward-compat no-op" bucket and the user's keys would
            # silently vanish.
            raise ConfigError(
                f"top-level `default-config` block in {path} is not "
                f"supported — nest it inside `agent \"claude\" {{ … }}` "
                f"or `agent \"codex\" {{ … }}`"
            )
        # Any other top-level node (e.g. `hooks`, `alias` for follow-up
        # tickets #30 / #33, or anything else not listed above) parses
        # as a no-op. Template and agent blocks are the only two shapes
        # this ticket knows about.
    return cfg


def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)

    # Agent defaults merge per key, not per agent block: a user-wide
    # `model` plus a project-scoped `effort` both survive. Same-key
    # conflicts resolve project-wins (over beats base). Empty incoming
    # dicts are skipped on BOTH sides so the parser invariant "presence
    # in agent_defaults implies at least one declared key" survives
    # programmatic AppConfig construction too.
    merged_agent_defaults: Dict[str, Dict[str, str]] = {
        agent: dict(keys)
        for agent, keys in base.agent_defaults.items()
        if keys
    }
    for agent, keys in over.agent_defaults.items():
        if not keys:
            continue
        existing = merged_agent_defaults.setdefault(agent, {})
        existing.update(keys)

    return AppConfig(
        templates=merged_templates,
        agent_defaults=merged_agent_defaults,
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

    Merge semantics (see `_merge`): templates merge by name, with
    project overriding user on collision. Per-agent `default-config`
    merges per-key — user + project contribute distinct keys for the
    same agent, and project wins on same-key conflict.
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
