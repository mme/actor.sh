# Per-Agent Default Config — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement ticket #31. Add `agent "claude" { default-config { … } }` blocks to `settings.kdl` so per-agent defaults slot into the 6-layer precedence chain between project config and templates. Launching a Claude actor auto-applies the claude block; launching a Codex actor auto-applies the codex block.

**Architecture:** Extend `AppConfig` with `agent_defaults: Dict[str, Dict[str, str]]` keyed by agent name. Parser learns the new block shape, validates the agent name via `AgentKind.from_str` (catches typos early), and merges across user + project files (project wins per-key). `cmd_new` applies the chosen agent's defaults as the lowest config layer, below the template and below CLI `--config` pairs. Existing templates / CLI behavior untouched.

**Tech Stack:** Python 3.10+, unittest, `kdl-py` (already a dep). No new runtime dependencies.

---

### Precedence recap (from #31)

Merge order in `cmd_new` (lowest → highest):

1. Empty dict
2. **`app_config.agent_defaults[chosen_agent]`** ← new
3. `template.config` (if `--template X`)
4. `parse_config(config_pairs)` (CLI `--config` / `--model` / `--strip-api-keys`)

Later layers overwrite earlier on same key. User vs. project settings.kdl merging happens once in `load_config` before any of this runs (project wins per-key).

The chosen agent is resolved **before** the merge, using today's rule: CLI `--agent` > template's `agent` > `"claude"`. So the defaults follow the agent kind that actually wins — not the template's agent when CLI overrides it.

---

### Task 1: Add `agent_defaults` field + parse minimal agent block

**Files:**
- Modify: `src/actor/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for minimal + multi-key + multi-agent parsing**

Append to `tests/test_config.py` after `TestLoadConfigCwdUnderHome`:

```python
class TestLoadConfigAgentDefaults(unittest.TestCase):

    def _load(self, body: str) -> AppConfig:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(body)
            return load_config(cwd=Path(cwd), home=Path(home))

    def test_single_key_default_config(self):
        cfg = self._load(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(cfg.agent_defaults, {"claude": {"model": "opus"}})

    def test_multiple_keys_in_default_config(self):
        cfg = self._load(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus"\n'
            '        effort "max"\n'
            '        strip-api-keys true\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(cfg.agent_defaults["claude"], {
            "model": "opus", "effort": "max", "strip-api-keys": "true",
        })

    def test_blocks_for_multiple_agents(self):
        cfg = self._load(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n'
            'agent "codex" {\n'
            '    default-config {\n'
            '        sandbox "danger-full-access"\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})
        self.assertEqual(cfg.agent_defaults["codex"], {"sandbox": "danger-full-access"})

    def test_empty_default_config_block_omits_agent_from_defaults(self):
        cfg = self._load(
            'agent "claude" {\n'
            '    default-config {\n'
            '    }\n'
            '}\n'
        )
        # Empty default-config contributes no keys; presence in
        # agent_defaults implies at least one declared key.
        self.assertNotIn("claude", cfg.agent_defaults)

    def test_agent_block_without_default_config_is_ignored(self):
        # Forward-compat: hooks etc. may live under `agent` in future
        # tickets; for now an agent block with no `default-config` child
        # parses without error and contributes no defaults.
        cfg = self._load('agent "claude" {\n}\n')
        self.assertNotIn("claude", cfg.agent_defaults)

    def test_non_default_config_children_of_agent_are_ignored(self):
        # Forward-compat: #30 will add `hooks {}` under `agent`. Unknown
        # children parse as no-ops today.
        cfg = self._load(
            'agent "claude" {\n'
            '    hooks {\n'
            '        on-start "echo hi"\n'
            '    }\n'
            '    default-config {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})
```

- [ ] **Step 2: Run the new tests — expect failure**

Run: `uv run python -m unittest tests.test_config.TestLoadConfigAgentDefaults -v`
Expected: every test FAILs with `AttributeError: 'AppConfig' object has no attribute 'agent_defaults'`.

- [ ] **Step 3: Extend AppConfig and add minimal parser for agent blocks**

In `src/actor/config.py`:

Update the top-level docstring (lines 7-9): change the "Out of scope" comment to reflect that per-agent defaults are now implemented:

```python
"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map and per-agent default-config overrides. Project config
overrides user config on same-key conflict.

Out of scope (see tickets #30 / #33): hooks, aliases. Their nodes are
skipped at parse time without error for forward compatibility.
"""
```

Replace the import line `from typing import Dict, Optional` with:

```python
from typing import Dict, Optional, Tuple
```

Widen the errors import and add an AgentKind import:

```python
from .errors import ActorError, ConfigError
from .types import AgentKind
```

(`ActorError` is the base class `AgentKind.from_str` raises; we catch
it when re-raising as `ConfigError` so downstream typo messages stay
uniform across the codebase.)

Replace the `AppConfig` dataclass (currently around lines 31-33):

```python
@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    agent_defaults: Dict[str, Dict[str, str]] = field(default_factory=dict)
```

Add a new helper above `_parse_kdl_file` (after `_parse_template`):

```python
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
        # Reject reserved keys BEFORE the duplicate check so a malformed
        # `prompt "a" / prompt "b"` reports the reserved-key error
        # (which tells the user to move `prompt` up to the template)
        # instead of a misleading "duplicate prompt".
        if key in ("prompt", "agent"):
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' is not a "
                f"valid default-config key (it's a template-level field, "
                f"not an agent config key)"
            )
        if key in seen_keys:
            raise ConfigError(
                f"agent '{agent_name}' in {source}: duplicate key '{key}' "
                f"in default-config"
            )
        seen_keys.add(key)
        # Props check runs before the args check so `model name="opus"`
        # reports "does not accept properties" rather than a misleading
        # "needs a value".
        if getattr(child, "props", None):
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' does not accept "
                f"properties"
            )
        if not child.args:
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

    Validates the agent name against AgentKind so typos (`agent "cluade"`)
    fail fast. Block-style children other than `default-config` (no args)
    are silently accepted as forward-compat no-ops for follow-up tickets
    (hooks #30 etc.). Key-with-value children directly under `agent` —
    e.g. `model "opus"` instead of `default-config { model "opus" }` —
    raise so the user's keys aren't silently dropped.
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
        if child.args:
            raise ConfigError(
                f"agent '{name}' in {source}: `{child.name}` cannot sit "
                f"directly under an `agent` block — nest config keys "
                f"inside `default-config {{ … }}`"
            )
    return name, defaults
```

Change `_parse_kdl_file` to dispatch `agent` nodes to the new parser (currently its loop silently skips them):

```python
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
            # A top-level `default-config` block is a common misread of
            # the schema; without this branch the user's keys would
            # silently vanish.
            raise ConfigError(
                f"top-level `default-config` block in {path} is not "
                f"supported — nest it inside `agent \"claude\" {{ … }}` "
                f"or `agent \"codex\" {{ … }}`"
            )
        # Silently ignore hooks / alias — those belong to follow-up
        # tickets #30 / #33 and are not implemented here.
    return cfg
```

- [ ] **Step 4: Run the new tests — expect pass**

Run: `uv run python -m unittest tests.test_config.TestLoadConfigAgentDefaults -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Run the full config test module — expect pass**

Run: `uv run python -m unittest tests.test_config -v`
Expected: every existing test still PASSes (the `agent` no-op forward-compat test still works because `agent "claude"` without a `default-config` child continues to contribute nothing).

- [ ] **Step 6: Commit**

```bash
git add src/actor/config.py tests/test_config.py
git commit -m "Parse agent \"X\" { default-config } blocks in settings.kdl (#31)"
```

---

### Task 2: Validation errors for malformed agent blocks

**Files:**
- Modify: `tests/test_config.py`

Parser work already done in Task 1; this task adds the explicit error-case coverage.

- [ ] **Step 1: Write failing (really: passing) tests for each validation path**

Append to `tests/test_config.py`:

```python
class TestLoadConfigAgentDefaultsStrict(unittest.TestCase):
    """Parser should reject silently-dropped / ambiguous agent-block input."""

    def _expect_error(self, kdl_text: str, needle: str) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(kdl_text)
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn(needle, str(ctx.exception))

    def test_unknown_agent_name_raises(self):
        self._expect_error(
            'agent "gpt4" {\n    default-config {\n        model "x"\n    }\n}\n',
            "gpt4",
        )

    def test_agent_block_without_name_raises(self):
        self._expect_error(
            'agent {\n    default-config {\n        model "x"\n    }\n}\n',
            "name",
        )

    def test_empty_agent_name_raises(self):
        self._expect_error(
            'agent "" {\n    default-config {\n    }\n}\n',
            "non-empty",
        )

    def test_non_string_agent_name_raises(self):
        self._expect_error(
            'agent 42 {\n    default-config {\n    }\n}\n',
            "name",
        )

    def test_extra_positional_on_agent_raises(self):
        self._expect_error(
            'agent "claude" "extra" {\n    default-config {\n    }\n}\n',
            "extra",
        )

    def test_props_on_agent_node_raises(self):
        self._expect_error(
            'agent "claude" flag="x" {\n    default-config {\n    }\n}\n',
            "claude",
        )

    def test_duplicate_agent_block_in_file_raises(self):
        self._expect_error(
            'agent "claude" {\n    default-config {\n        model "opus"\n    }\n}\n'
            'agent "claude" {\n    default-config {\n        model "sonnet"\n    }\n}\n',
            "claude",
        )

    def test_multiple_default_config_blocks_in_one_agent_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n        model "opus"\n    }\n'
            '    default-config {\n        model "sonnet"\n    }\n'
            '}\n',
            "default-config",
        )

    def test_duplicate_key_in_default_config_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus"\n'
            '        model "sonnet"\n'
            '    }\n'
            '}\n',
            "model",
        )

    def test_child_without_value_in_default_config_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model\n'
            '    }\n'
            '}\n',
            "model",
        )

    def test_extra_args_on_default_config_child_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus" "sonnet"\n'
            '    }\n'
            '}\n',
            "model",
        )

    def test_props_on_default_config_child_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        model name="opus"\n'
            '    }\n'
            '}\n',
            "model",
        )

    def test_positional_args_on_default_config_block_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config "oops" {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n',
            "default-config",
        )
```

- [ ] **Step 2: Run the strict tests — expect all to pass**

Run: `uv run python -m unittest tests.test_config.TestLoadConfigAgentDefaultsStrict -v`
Expected: all PASS (Task 1's parser already raises in each of these cases).

If any test fails, adjust the parser in `src/actor/config.py` — the needle/case the test expects dictates which `raise ConfigError(...)` call is missing or mis-worded. Don't loosen tests; fix the parser.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "Add strict tests for agent block parser errors (#31)"
```

---

### Task 3: Merge `agent_defaults` across user + project configs

**Files:**
- Modify: `src/actor/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for user+project merge semantics**

Append to `tests/test_config.py`:

```python
class TestLoadConfigAgentDefaultsMerge(unittest.TestCase):

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_project_wins_on_same_key(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "sonnet"\n'
                '    }\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})

    def test_distinct_keys_merge(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        effort "max"\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.agent_defaults["claude"], {
                "model": "opus", "effort": "max",
            })

    def test_distinct_agents_in_user_and_project_both_survive(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "codex" {\n'
                '    default-config {\n'
                '        sandbox "danger-full-access"\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})
            self.assertEqual(cfg.agent_defaults["codex"], {"sandbox": "danger-full-access"})

    def test_templates_and_agent_defaults_coexist_in_same_file(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'template "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["qa"].agent, "claude")
            self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})
```

- [ ] **Step 2: Run the merge tests — expect failure on `test_distinct_keys_merge`**

Run: `uv run python -m unittest tests.test_config.TestLoadConfigAgentDefaultsMerge -v`
Expected: `test_project_wins_on_same_key` PASSes today (`dict.update` already overwrites), but `test_distinct_keys_merge` FAILs because the current `_merge` does `merged.update(over)` which replaces the claude dict wholesale (user's `model` is lost when project has only `effort`).

- [ ] **Step 3: Update `_merge` to do per-agent deep-merge on `agent_defaults`**

Replace `_merge` in `src/actor/config.py`:

```python
def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = dict(base.templates)
    merged_templates.update(over.templates)

    merged_agent_defaults: Dict[str, Dict[str, str]] = {
        agent: dict(keys) for agent, keys in base.agent_defaults.items()
    }
    for agent, keys in over.agent_defaults.items():
        existing = merged_agent_defaults.setdefault(agent, {})
        existing.update(keys)

    return AppConfig(
        templates=merged_templates,
        agent_defaults=merged_agent_defaults,
    )
```

Templates stay shallow-merged (project replaces user for the same template name — matches existing behavior). Agent defaults merge per-key so a user-wide `model` plus a project-scoped `effort` both survive.

- [ ] **Step 4: Run the merge tests — expect pass**

Run: `uv run python -m unittest tests.test_config.TestLoadConfigAgentDefaultsMerge -v`
Expected: all 4 PASS.

- [ ] **Step 5: Run the full config test module**

Run: `uv run python -m unittest tests.test_config -v`
Expected: every test PASSes (templates still merge as before).

- [ ] **Step 6: Commit**

```bash
git add src/actor/config.py tests/test_config.py
git commit -m "Per-key merge of agent_defaults across user + project configs (#31)"
```

---

### Task 4: Apply `agent_defaults` in `cmd_new` precedence chain

**Files:**
- Modify: `src/actor/commands.py`
- Modify: `tests/test_actor.py`

- [ ] **Step 1: Write failing tests for cmd_new integration**

Append to `tests/test_actor.py` after `TestCmdNewTemplate` (right before `TestCmdRun`):

```python
# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_new with per-agent default-config (ticket #31)
# ──────────────────────────────────────────────────────────────────────

class TestCmdNewAgentDefaults(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def _cfg(self, **kwargs):
        from actor import AppConfig
        return AppConfig(**kwargs)

    def test_agent_defaults_applied_for_claude(self):
        db = self._db()
        git = FakeGit()
        cfg = self._cfg(agent_defaults={"claude": {"model": "opus"}})
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", config_pairs=[],
            app_config=cfg,
        )
        self.assertEqual(actor.config["model"], "opus")

    def test_only_chosen_agent_defaults_apply(self):
        db = self._db()
        git = FakeGit()
        cfg = self._cfg(agent_defaults={
            "claude": {"model": "opus"},
            "codex": {"sandbox": "danger-full-access"},
        })
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="codex", config_pairs=[],
            app_config=cfg,
        )
        self.assertEqual(actor.config, {"sandbox": "danger-full-access"})
        self.assertNotIn("model", actor.config)

    def test_cli_config_pair_overrides_agent_default(self):
        db = self._db()
        git = FakeGit()
        cfg = self._cfg(agent_defaults={"claude": {"model": "opus"}})
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", config_pairs=["model=haiku"],
            app_config=cfg,
        )
        self.assertEqual(actor.config["model"], "haiku")

    def test_template_config_overrides_agent_default(self):
        # Agent defaults sit BELOW templates in the ladder.
        from actor import AppConfig, Template
        db = self._db()
        git = FakeGit()
        cfg = AppConfig(
            templates={"qa": Template(
                name="qa", agent="claude", config={"model": "sonnet"},
            )},
            agent_defaults={"claude": {"model": "opus", "effort": "max"}},
        )
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name=None, config_pairs=[],
            template_name="qa", app_config=cfg,
        )
        # Template wins on `model`; agent default supplies `effort`.
        self.assertEqual(actor.config["model"], "sonnet")
        self.assertEqual(actor.config["effort"], "max")

    def test_cli_overrides_both_template_and_agent_default(self):
        from actor import AppConfig, Template
        db = self._db()
        git = FakeGit()
        cfg = AppConfig(
            templates={"qa": Template(
                name="qa", agent="claude", config={"model": "sonnet"},
            )},
            agent_defaults={"claude": {"model": "opus"}},
        )
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name=None, config_pairs=["model=haiku"],
            template_name="qa", app_config=cfg,
        )
        self.assertEqual(actor.config["model"], "haiku")

    def test_defaults_follow_cli_agent_not_template_agent(self):
        # Template says claude, but CLI --agent=codex wins → codex defaults
        # apply, claude defaults do NOT.
        from actor import AppConfig, Template
        db = self._db()
        git = FakeGit()
        cfg = AppConfig(
            templates={"qa": Template(name="qa", agent="claude")},
            agent_defaults={
                "claude": {"model": "opus"},
                "codex": {"sandbox": "danger-full-access"},
            },
        )
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="codex", config_pairs=[],
            template_name="qa", app_config=cfg,
        )
        self.assertEqual(actor.agent, AgentKind.CODEX)
        self.assertEqual(actor.config, {"sandbox": "danger-full-access"})

    def test_no_defaults_for_agent_is_noop(self):
        db = self._db()
        git = FakeGit()
        cfg = self._cfg(agent_defaults={"codex": {"sandbox": "x"}})
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", config_pairs=[],
            app_config=cfg,
        )
        self.assertEqual(actor.config, {})

    def test_none_app_config_is_noop(self):
        # Backward-compat: callers that don't pass app_config see the old
        # behavior (no defaults applied).
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", config_pairs=["model=sonnet"],
        )
        self.assertEqual(actor.config, {"model": "sonnet"})

    def test_defaults_applied_when_agent_name_defaults_to_claude(self):
        # agent_name=None + no template → chosen agent is claude. Claude
        # defaults should still apply.
        db = self._db()
        git = FakeGit()
        cfg = self._cfg(agent_defaults={"claude": {"model": "opus"}})
        actor = cmd_new(
            db, git,
            name="a", dir="/tmp", no_worktree=True, base=None,
            agent_name=None, config_pairs=[],
            app_config=cfg,
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
        self.assertEqual(actor.config["model"], "opus")
```

- [ ] **Step 2: Run the new tests — expect failure**

Run: `uv run python -m unittest tests.test_actor.TestCmdNewAgentDefaults -v`
Expected: most tests FAIL — `actor.config` is missing the agent-default keys.

- [ ] **Step 3: Wire agent_defaults into cmd_new**

In `src/actor/commands.py`, replace the config-building section of `cmd_new` (currently around lines 138-142):

Before:
```python
    # Config precedence: template's config is the base; CLI pairs overlay on top.
    config: Dict[str, str] = dict(template.config) if template else {}
    for k, v in parse_config(config_pairs).items():
        config[k] = v
    config = _sorted_config(config)
```

After:
```python
    # Config precedence (lowest → highest): per-agent defaults from
    # settings.kdl → template.config → CLI --config pairs. Per-agent
    # defaults key off the ALREADY-chosen agent_kind, so CLI --agent
    # overriding a template's agent correctly picks up the new agent's
    # defaults rather than the template's.
    config: Dict[str, str] = {}
    if app_config is not None:
        agent_defaults = app_config.agent_defaults.get(agent_kind.as_str())
        if agent_defaults:
            config.update(agent_defaults)
    if template is not None:
        config.update(template.config)
    for k, v in parse_config(config_pairs).items():
        config[k] = v
    config = _sorted_config(config)
```

- [ ] **Step 4: Run the new tests — expect pass**

Run: `uv run python -m unittest tests.test_actor.TestCmdNewAgentDefaults -v`
Expected: all 9 PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run python -m unittest discover tests`
Expected: every test PASSes. Existing `TestCmdNewTemplate` tests still work because without an `app_config.agent_defaults` entry, the new branch is a no-op.

- [ ] **Step 6: Commit**

```bash
git add src/actor/commands.py tests/test_actor.py
git commit -m "Apply per-agent default-config as lowest layer in cmd_new (#31)"
```

---

### Task 5: Docs — SKILL.md + CLAUDE.md

**Files:**
- Modify: `src/actor/_skill/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update SKILL.md — add a "Per-agent defaults" subsection under Templates**

In `src/actor/_skill/SKILL.md`, update the forward-compat paragraph in
the Templates section so it no longer advertises `agent` as a no-op
(`agent` is now implemented). Leave the remaining forward-compat nodes
(`hooks`, `alias`) listed.

Then, immediately before the "**Applying a template** (CLI only — see
note below):" line, insert a new "**Per-agent defaults:**" subsection
that:

1. Shows the `agent "claude" { default-config { … } }` / `agent "codex"
   { default-config { … } }` block syntax.
2. Points to `claude-config.md` / `codex-config.md` for valid keys.
3. Spells out the precedence ladder and merge rule. Treat user and
   project `settings.kdl` as a single merged layer — NOT as separate
   ordered steps — to match how `_merge` folds them per key. The ladder
   covers only the inputs `cmd_new` actually consumes at creation time;
   don't invent phantom layers (e.g. "built-in defaults") that don't
   exist in the code. Recommended phrasing:

   ```
   Precedence at actor creation (lowest → highest):

   1. Per-agent defaults from `settings.kdl` (user + project merged per
      key; project wins on conflicts)
   2. Template config (`--template X`)
   3. CLI `--config key=value`

   `settings.kdl` is read at actor CREATION time. Editing the file later
   does not retroactively change existing actors — use
   `config_actor(name="…", pairs=[…])` / `actor config <name> key=value`
   to update an actor's stored config.

   At run time (`actor run`), the stored config is the base and per-run
   `--config` arguments layer on top for that run only.
   ```

- [ ] **Step 2: Update CLAUDE.md — extend the "Config files & templates" section**

In `CLAUDE.md`, replace the forward-compat paragraph that mentions
`hooks`/`agent`/`alias` with two paragraphs:

1. A per-agent-defaults paragraph that describes the block shape,
   mentions user+project per-key merge, and inlines the precedence
   ladder (lowest → highest: per-agent defaults → template → CLI
   `--config`). Include a sentence about snapshot-at-creation — editing
   `settings.kdl` later does not retroactively change existing actors.
2. A short forward-compat paragraph that still mentions `hooks` / `alias`
   (no longer `agent`) as silently ignored for tickets #30 / #33.

Do NOT reference this plan document (`spec/PLAN-AGENT-DEFAULTS.md`) from
`CLAUDE.md` — the plan is transient and will be archived after the PR
merges.

- [ ] **Step 3: Run the full test suite one more time (docs-only changes, but sanity)**

Run: `uv run python -m unittest discover tests`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add src/actor/_skill/SKILL.md CLAUDE.md
git commit -m "Document per-agent default-config in SKILL.md and CLAUDE.md (#31)"
```

---

## Self-review checklist

- [x] **Spec coverage:** Ticket #31 asks for `agent "X" { default-config { … } }` blocks, precedence layer 4, API-key subsumption. Task 1 parses the block; Task 3 merges across files; Task 4 wires precedence; `strip-api-keys` is covered as an ordinary key (Task 1's multi-key test + Task 4's `--config` override test). Docs in Task 5. No `--api-key` CLI flag work needed — ticket keeps existing `--strip-api-keys` / `--no-strip-api-keys` CLI tri-state.
- [x] **Placeholders:** No "TBD" / "similar to". All code blocks concrete.
- [x] **Type consistency:** `agent_defaults: Dict[str, Dict[str, str]]` used identically in parser, merge, and cmd_new.
- [x] **Out-of-scope fences:** No hooks (#30), no aliases (#33), no MCP `template=` parameter — those are explicitly deferred in the SKILL.md text.
