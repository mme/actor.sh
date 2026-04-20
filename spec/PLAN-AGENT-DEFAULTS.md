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

    def test_empty_default_config_block_yields_empty_dict(self):
        cfg = self._load(
            'agent "claude" {\n'
            '    default-config {\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(cfg.agent_defaults["claude"], {})

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

Add right after the existing `from .errors import ConfigError` line:

```python
from .types import AgentKind
```

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
    """Parse a `default-config { … }` block inside an agent block.

    Same shape rules as template children: each child is `key "value"`
    with exactly one scalar arg, no props, no duplicates. Unknown keys
    pass through (no fixed schema — they're consumed by the agent at
    spawn time).
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
    for child in node.nodes:
        key = child.name
        if key in out:
            raise ConfigError(
                f"agent '{agent_name}' in {source}: duplicate key '{key}' "
                f"in default-config"
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
        if getattr(child, "props", None):
            raise ConfigError(
                f"agent '{agent_name}' in {source}: '{key}' does not accept "
                f"properties"
            )
        out[key] = _coerce_value(child.args[0])
    return out


def _parse_agent_block(node, source: Path) -> Tuple[str, Dict[str, str]]:
    """Parse an `agent "name" { … }` block. Returns (agent_name, defaults).

    Validates the agent name against AgentKind so typos (`agent "cluade"`)
    fail fast. Children other than `default-config` are silently ignored
    so future tickets (hooks etc.) can share the block without breaking
    today's parser.
    """
    if not node.args or not isinstance(node.args[0], str):
        raise ConfigError(
            f"agent block in {source} must have a name "
            f"(e.g. `agent \"claude\" {{ … }}`)"
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
    except Exception as e:
        raise ConfigError(
            f"agent block in {source}: unknown agent '{name}' "
            f"(valid: {', '.join(k.value for k in AgentKind)})"
        ) from e

    defaults: Dict[str, str] = {}
    seen_default_config = False
    for child in node.nodes:
        if child.name != "default-config":
            # Forward-compat: e.g. hooks (#30). Silently skipped today.
            continue
        if seen_default_config:
            raise ConfigError(
                f"agent '{name}' in {source}: multiple `default-config` "
                f"blocks (only one is allowed)"
            )
        seen_default_config = True
        defaults = _parse_default_config(child, name, source)
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
            # An agent block with no `default-config` child contributes no
            # defaults — don't create an empty entry, keep the invariant
            # "presence in agent_defaults implies defaults were declared".
            if defaults:
                cfg.agent_defaults[name] = defaults
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

In `src/actor/_skill/SKILL.md`, locate the paragraph near the top of the Templates section that ends with "Unknown top-level nodes (`hooks`, `agent`, `alias`) parse as no-ops today —" (around line 122) and change it to reflect that `agent` is now implemented. Replace that paragraph with:

```
Unknown top-level nodes (`hooks`, `alias`) parse as no-ops today —
they're reserved for follow-up tickets. Malformed KDL raises an error
with the file path.
```

Then, immediately before the "**Applying a template** (CLI only — see note below):" line, insert a new subsection:

```markdown
**Per-agent defaults:**

Alongside templates you can declare default config scoped to a specific
coding agent. Any Claude actor picks up the `agent "claude"` block; any
Codex actor picks up the `agent "codex"` block.

```kdl
agent "claude" {
    default-config {
        model "opus"
        effort "max"
        strip-api-keys true
    }
}

agent "codex" {
    default-config {
        sandbox "danger-full-access"
    }
}
```

Valid keys are anything the agent itself accepts — same catalogue as
[claude-config.md](claude-config.md) and [codex-config.md](codex-config.md).

Precedence (lowest → highest): built-in defaults → user `settings.kdl`
→ project `settings.kdl` → **per-agent defaults for the chosen agent**
→ template (`--template X`) → CLI `--config key=value` → per-actor DB
config. Same-key conflicts between user and project blocks are resolved
per key, with project winning.

```

- [ ] **Step 2: Update CLAUDE.md — extend the "Config files & templates" section**

In `CLAUDE.md`, locate the paragraph that begins "Unknown top-level nodes (e.g. `hooks`, `agent`, `alias`) are silently ignored for forward-compat with follow-up tickets." and replace it with:

```
Per-agent default config lives in `agent "claude" { default-config { … } }`
(and the same shape for `codex`). Any actor of that kind picks up those
keys as the lowest config layer, under templates and under CLI `--config`.
See ticket #31 for precedence details.

Unknown top-level nodes (e.g. `hooks`, `alias`) are silently ignored for
forward-compat with follow-up tickets #30 / #33.
```

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
