# Config File System + Templates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `~/.actor/settings.kdl` + `<project>/.actor/settings.kdl` config files, a `Template` system, and a `--template` flag for `actor new`.

**Architecture:** New pure-Python module `src/actor/config.py` loads KDL files via the `kdl-py` parser, exposes an `AppConfig` dataclass with `templates: dict[str, Template]`. The CLI layer (`cli.py`) calls `load_config()` once per invocation and passes the result down to `cmd_new`. Project file is found by walking up from cwd (git-style). Project templates of the same name override user templates.

**Tech Stack:** Python 3.10+, `kdl-py>=1.2.0` (pure-python KDL v1 parser), `unittest`, `dataclasses`, `pathlib`. No other new dependencies.

**Scope strictly per ticket #29:** config files + templates + `--template` CLI flag.

**Out of scope (follow-ups):**
- Hooks (#30) — explicitly skip any `hooks { ... }` nodes at parse time with a comment noting the follow-up.
- Per-agent `agent "claude" { default-config { ... } }` blocks (#31) — skip at parse time.
- Aliases (#33) — skip at parse time.
- Resources — dropped from the roadmap.
- MCP `new_actor` tool keeps current signature — templates are CLI-only for now.

---

## File Structure

**Files created:**
- `src/actor/config.py` — parses KDL, exposes `AppConfig` / `Template` dataclasses and `load_config()`.
- `tests/test_config.py` — unittest suite for `config.py`.
- `tests/__init__.py` — empty marker file; prevents `kdl-py`'s bundled `tests/` package from shadowing the project's tests during `unittest discover`. (See "KDL parser packaging workaround" below.)

**Files modified:**
- `pyproject.toml` — add `kdl-py>=1.2.0` to `[project].dependencies`.
- `src/actor/__init__.py` — export `AppConfig`, `Template`, `load_config` for test use.
- `src/actor/commands.py` — `cmd_new` accepts optional `template_name` + `app_config` kwargs; applies template before CLI overrides.
- `src/actor/cli.py` — add `--template` to `new` subparser; call `load_config()`; pass it to `cmd_new`; template prompt used as fallback when no CLI arg and no stdin.
- `tests/test_actor.py` — new `TestCmdNewTemplate` class covering template application + CLI override precedence.
- `tests/test_cli_and_mcp.py` — tests for `--template` argparse wiring.
- `CLAUDE.md` — new "Config files" section describing layout + `--template`.
- `src/actor/_skill/SKILL.md` — new section on templates so agents know how to surface them to users.

---

## KDL parser packaging workaround

The `kdl-py` 1.2.0 wheel ships a top-level `tests/` package inside `site-packages` (`kdl_py-1.2.0-py3-none-any.whl` → `tests/__init__.py`, `tests/run.py`). Once installed, `import tests` resolves to that site-packages package, shadowing the project's `tests/` directory. `tests/test_interactive_manager.py:67` imports `from tests.test_actor import FakeGit`, which then fails.

**Fix:** add an empty `tests/__init__.py` to the project. Making our `tests/` a real regular package (not a namespace package) gives it precedence on `sys.path[0]` (the cwd injected by `python -m unittest discover tests`). Verified locally: with `tests/__init__.py` present, all 295 tests pass with `kdl-py` installed.

---

## Data shapes

```python
# src/actor/config.py

@dataclass
class Template:
    name: str
    agent: Optional[str] = None      # "claude" | "codex" | None
    prompt: Optional[str] = None     # default prompt for the first run
    config: Dict[str, str] = field(default_factory=dict)  # model, effort, etc.


@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)


def load_config(
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
) -> AppConfig:
    """Load user + project settings.kdl, with project winning.

    cwd defaults to Path.cwd(); home defaults to Path(os.environ['HOME']).
    Both kwargs exist for testability — tests pass tmp dirs.
    Missing files are silently skipped. Malformed files raise ConfigError.
    """
```

The loader is idempotent and pure (given the same disk state, same result). Every CLI invocation calls `load_config()` once and passes the result down.

---

## Precedence applied in `cmd_new`

The ticket lists six layers. Out-of-scope layers (hooks, per-agent, aliases) are not implemented, so the layers that actually compose here are:

1. Hard-coded defaults (existing — `AgentKind.from_str("claude")` default in `cli.py`, empty config dict).
2. User `~/.actor/settings.kdl` templates.
3. Project `.actor/settings.kdl` templates (overrides user templates of the same name).
4. `--template X` applied in `cmd_new`: pulls the template's `agent`, `prompt`, `config` dict.
5. `--config key=value` and `--agent` / `--model` CLI flags (these win over template values).
6. Per-actor DB config (existing — unchanged).

**Implementation:** `cmd_new` builds its `Config` dict by starting from the template's config (if any), then overlaying CLI pairs. For `agent_name`, `cli.py` changes the argparse default from `"claude"` to `None`; `cmd_new` resolves as: explicit `--agent` → template's `agent` → fallback `"claude"`. For the prompt, CLI arg / stdin still take precedence, and template.prompt is used only when neither is present.

---

## Task list

### Task 1: Add `kdl-py` dependency and `tests/__init__.py`

**Files:**
- Modify: `pyproject.toml:16`
- Create: `tests/__init__.py`

- [ ] **Step 1: Add kdl-py to dependencies**

Edit `pyproject.toml`, insert into `[project].dependencies` (after existing entries, before the closing `]`):

```toml
dependencies = [
    "mcp>=1.0",
    "pyte>=0.8.1",
    "textual>=1.0",
    "textual-serve>=1.0",
    "kdl-py>=1.2.0",
]
```

- [ ] **Step 2: Create empty `tests/__init__.py`**

```bash
touch tests/__init__.py
```

- [ ] **Step 3: Sync environment and verify no regressions**

```bash
uv sync
uv run python -m unittest discover tests
```

Expected: `Ran 295 tests ... OK`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock tests/__init__.py
git commit -m "chore: add kdl-py dep + tests/__init__.py for package discovery"
```

---

### Task 2: Write failing tests for `load_config()` (empty state)

**Files:**
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test file**

```python
#!/usr/bin/env python3
"""Tests for src/actor/config.py — KDL loader + templates."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actor.config import AppConfig, Template, load_config
from actor.errors import ConfigError


class TestLoadConfigEmpty(unittest.TestCase):

    def test_no_files_returns_empty_config(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIsInstance(cfg, AppConfig)
            self.assertEqual(cfg.templates, {})

    def test_missing_user_file_but_project_file_loads_project(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            proj = Path(cwd) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text('template "qa" { agent "claude" }\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertEqual(cfg.templates["qa"].agent, "claude")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run python -m unittest tests.test_config -v
```

Expected: `ModuleNotFoundError: No module named 'actor.config'`.

---

### Task 3: Create minimal `config.py` to pass the empty-state tests

**Files:**
- Create: `src/actor/config.py`

- [ ] **Step 1: Write the minimal module**

```python
"""Config file system for actor.sh.

Loads ~/.actor/settings.kdl (user) and <project>/.actor/settings.kdl
(project), merges them, and exposes the merged AppConfig with its
templates map. Project config overrides user config on same-key conflict.

Out of scope (see tickets #30 / #31 / #33): hooks, per-agent default-config
blocks, aliases. Their nodes are skipped at parse time without error.
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


# Config keys that live at the top level of a template block and are
# promoted to Template attributes rather than stored in Template.config.
_TEMPLATE_RESERVED = {"agent", "prompt"}


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


def _parse_template(node, source: Path) -> Template:
    if not node.args or not isinstance(node.args[0], str):
        raise ConfigError(
            f"template block in {source} must have a name (e.g. `template \"qa\" {{ ... }}`)"
        )
    name = node.args[0]
    tpl = Template(name=name)
    for child in node.nodes:
        key = child.name
        if not child.args:
            raise ConfigError(
                f"template '{name}' in {source}: '{key}' needs a value"
            )
        value = child.args[0]
        # Coerce KDL types (bool/float/str) into plain strings for the
        # existing config pipeline, which is stringly-typed.
        if isinstance(value, bool):
            value_str = "true" if value else "false"
        elif isinstance(value, float):
            # KDL parses all numbers as float; drop trailing .0 for
            # round-number literals so `effort 2` stays "2" not "2.0".
            value_str = str(int(value)) if value.is_integer() else str(value)
        else:
            value_str = str(value)
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
    - home defaults to Path(os.environ['HOME']) (or AppConfig() if unset)

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
```

- [ ] **Step 2: Run the tests and confirm they pass**

```bash
uv run python -m unittest tests.test_config -v
```

Expected: 2 tests, both pass.

- [ ] **Step 3: Commit**

```bash
git add src/actor/config.py tests/test_config.py
git commit -m "feat: add config.py with AppConfig + Template + load_config()"
```

---

### Task 4: Add tests + implementation for precedence, walk-up, parse errors

**Files:**
- Modify: `tests/test_config.py`
- Modify: `src/actor/config.py` (only if tests surface a gap)

- [ ] **Step 1: Write additional tests for load_config**

Append to `tests/test_config.py`:

```python
class TestLoadConfigPrecedence(unittest.TestCase):

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_project_overrides_user_same_template_name(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'template "qa" { agent "claude"; model "sonnet" }\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'template "qa" { agent "codex"; model "opus" }\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["qa"].agent, "codex")
            self.assertEqual(cfg.templates["qa"].config["model"], "opus")

    def test_user_and_project_both_contribute_distinct_templates(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'template "qa" { agent "claude" }\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'template "reviewer" { agent "claude" }\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertIn("reviewer", cfg.templates)


class TestLoadConfigWalkUp(unittest.TestCase):

    def test_project_config_found_by_walking_up_from_cwd(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            proj = Path(root) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text('template "qa" { agent "claude" }\n')
            deep = Path(root) / "src" / "nested" / "deeper"
            deep.mkdir(parents=True)
            cfg = load_config(cwd=deep, home=Path(home))
            self.assertIn("qa", cfg.templates)


class TestLoadConfigErrors(unittest.TestCase):

    def test_malformed_kdl_raises_config_error_with_path(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('template "qa" { unclosed\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn(str(bad), str(ctx.exception))

    def test_template_without_name_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text("template { agent \"claude\" }\n")
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))

    def test_template_child_without_value_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('template "qa" { agent }\n')
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))


class TestLoadConfigParseShapes(unittest.TestCase):

    def test_template_with_all_fields(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'template "qa" {\n'
                '    agent "claude"\n'
                '    model "opus"\n'
                '    effort "max"\n'
                '    prompt "You\\'re a QA engineer."\n'
                '}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            tpl = cfg.templates["qa"]
            self.assertEqual(tpl.agent, "claude")
            self.assertEqual(tpl.prompt, "You're a QA engineer.")
            self.assertEqual(tpl.config, {"model": "opus", "effort": "max"})

    def test_bool_value_coerced_to_string(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('template "x" { strip-api-keys true }\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["x"].config["strip-api-keys"], "true")

    def test_int_value_coerced_without_trailing_zero(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('template "x" { max-budget-usd 5 }\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["x"].config["max-budget-usd"], "5")

    def test_unknown_top_level_nodes_are_ignored(self):
        # Forward-compat: hooks/agent/alias exist in follow-up tickets but
        # should parse as no-ops today rather than erroring.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'hooks { on-start "echo hi" }\n'
                'alias "max" template="qa"\n'
                'template "qa" { agent "claude" }\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertEqual(len(cfg.templates), 1)
```

- [ ] **Step 2: Run the tests**

```bash
uv run python -m unittest tests.test_config -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "test: precedence, walk-up, parse errors for load_config"
```

---

### Task 5: Re-export new types from the actor package

**Files:**
- Modify: `src/actor/__init__.py`

- [ ] **Step 1: Add imports and __all__ entries**

Insert under the existing `# Types` block in `src/actor/__init__.py`:

```python
from .config import AppConfig, Template, load_config
```

Add to `__all__` (in the "Types" or a new "Config" cluster):

```python
    # Config
    "AppConfig",
    "Template",
    "load_config",
```

- [ ] **Step 2: Verify the package re-exports work**

```bash
uv run python -c "from actor import AppConfig, Template, load_config; print(AppConfig())"
```

Expected: `AppConfig(templates={})`.

- [ ] **Step 3: Commit**

```bash
git add src/actor/__init__.py
git commit -m "feat: re-export AppConfig, Template, load_config from actor package"
```

---

### Task 6: Write failing tests for `cmd_new` template application

**Files:**
- Modify: `tests/test_actor.py`

- [ ] **Step 1: Add the test class**

Append to `tests/test_actor.py` after the existing `TestCmdNew` class:

```python
class TestCmdNewTemplate(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def _cfg_with_qa(self):
        from actor import AppConfig, Template
        return AppConfig(templates={
            "qa": Template(
                name="qa",
                agent="codex",
                prompt="you're qa",
                config={"model": "opus", "effort": "max"},
            ),
        })

    def test_template_applies_agent_and_config(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=[],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.agent, AgentKind.CODEX)
        self.assertEqual(actor.config["model"], "opus")
        self.assertEqual(actor.config["effort"], "max")

    def test_cli_agent_overrides_template_agent(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config_pairs=[],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)

    def test_cli_config_pair_overrides_template_config(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=["model=haiku"],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.config["model"], "haiku")   # CLI wins
        self.assertEqual(actor.config["effort"], "max")    # template retained

    def test_unknown_template_raises_config_error(self):
        from actor.errors import ConfigError
        db = self._db()
        git = FakeGit()
        with self.assertRaises(ConfigError):
            cmd_new(
                db, git,
                name="fix-auth",
                dir="/tmp",
                no_worktree=True,
                base=None,
                agent_name=None,
                config_pairs=[],
                template_name="does-not-exist",
                app_config=self._cfg_with_qa(),
            )

    def test_no_template_backward_compatible(self):
        """Calling cmd_new without template args behaves exactly as before."""
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="plain",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config_pairs=["model=sonnet"],
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
        self.assertEqual(actor.config["model"], "sonnet")

    def test_agent_name_none_without_template_defaults_to_claude(self):
        """New None default for --agent resolves to claude when no template."""
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="x",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=[],
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
uv run python -m unittest tests.test_actor.TestCmdNewTemplate -v
```

Expected: all 6 fail with `TypeError: cmd_new() got an unexpected keyword argument 'template_name'` (or similar).

---

### Task 7: Update `cmd_new` to accept template args

**Files:**
- Modify: `src/actor/commands.py:106-182` (the `cmd_new` function)

- [ ] **Step 1: Update the `cmd_new` signature and body**

Change `cmd_new` to:

```python
def cmd_new(
    db: Database,
    git: GitOps,
    name: str,
    dir: Optional[str],
    no_worktree: bool,
    base: Optional[str],
    agent_name: Optional[str],
    config_pairs: List[str],
    template_name: Optional[str] = None,
    app_config: Optional["AppConfig"] = None,
) -> Actor:
    validate_name(name)

    # Resolve template (if any)
    template = None
    if template_name is not None:
        if app_config is None or template_name not in app_config.templates:
            raise ConfigError(f"unknown template: '{template_name}'")
        template = app_config.templates[template_name]

    # Agent: explicit CLI flag wins; otherwise template's agent; otherwise claude
    if agent_name is None:
        agent_name = template.agent if (template and template.agent) else "claude"
    agent_kind = AgentKind.from_str(agent_name)

    if not binary_exists(agent_kind.binary_name):
        print(f"warning: '{agent_kind.binary_name}' not found on PATH", file=sys.stderr)

    # Config: template's config as base; CLI pairs override
    config: Dict[str, str] = dict(template.config) if template else {}
    cli_pairs = parse_config(config_pairs)
    for k, v in cli_pairs.items():
        config[k] = v
    config = _sorted_config(config)

    # ...rest of cmd_new is unchanged (base_dir resolution, worktree, db insert, ...)
```

Then use `config` in the `Actor(..., config=config, ...)` construction instead of the previous `config = parse_config(config_pairs)`.

The required import additions at the top of `commands.py`:

```python
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .config import AppConfig
```

And import `ConfigError` from `.errors` (already imported — verify).

- [ ] **Step 2: Run the template tests and verify they pass**

```bash
uv run python -m unittest tests.test_actor.TestCmdNewTemplate -v
```

Expected: all 6 pass.

- [ ] **Step 3: Run the full suite to verify no regressions**

```bash
uv run python -m unittest discover tests
```

Expected: `Ran 30X tests ... OK` (the new template tests push the count above 295).

- [ ] **Step 4: Commit**

```bash
git add src/actor/commands.py tests/test_actor.py
git commit -m "feat: cmd_new applies --template then CLI overrides"
```

---

### Task 8: Wire `--template` in the CLI

**Files:**
- Modify: `src/actor/cli.py:59-84` (the `new` subparser) and `cli.py:334-371` (the `if args.command == "new":` branch).

- [ ] **Step 1: Add the argparse flag and flip --agent default**

In `_build_parser`, inside the `new` subparser block:

```python
p_new.add_argument("--agent", default=None, help="Coding agent (defaults to template's agent or 'claude')")
p_new.add_argument("--template", default=None, help="Apply a template from settings.kdl")
```

Update the epilog to mention `--template`:

```python
  actor new my-feature --template qa                  Apply the 'qa' template
```

- [ ] **Step 2: Load app_config and pass template_name into cmd_new**

In `main()`, at the top of the `if args.command == "new":` branch:

```python
elif args.command == "new":
    from .config import load_config
    app_config = load_config()
    config_pairs = list(args.config)
    if args.model is not None:
        config_pairs.append(f"model={args.model}")
    if not args.strip_api_keys:
        config_pairs.append("strip-api-keys=false")
    actor = cmd_new(
        db, git,
        name=args.name,
        dir=args.dir,
        no_worktree=args.no_worktree,
        base=args.base,
        agent_name=args.agent,
        config_pairs=config_pairs,
        template_name=args.template,
        app_config=app_config,
    )
    print(f"{actor.name} created ({actor.dir})")

    prompt = args.prompt
    stdin_consumed = False
    if prompt is None and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        stdin_consumed = True
    if prompt is None and args.template is not None:
        tpl = app_config.templates.get(args.template)
        if tpl is not None and tpl.prompt:
            prompt = tpl.prompt
    if stdin_consumed and not prompt:
        print("error: stdin was empty — expected a prompt", file=sys.stderr)
        sys.exit(1)
    if prompt:
        # ...unchanged cmd_run block
```

(The `stdin_consumed and not prompt` check must come **after** the template fallback so a template's prompt doesn't mask the stdin-empty error. But note that if stdin was consumed and produced empty, we intentionally do NOT fall back to the template — explicit stdin empty is a user error, not a "no prompt provided" case. So the stdin check still precedes the template check semantically. Restructure as:

```python
    prompt = args.prompt
    if prompt is None and not sys.stdin.isatty():
        stdin_val = sys.stdin.read().strip()
        if not stdin_val:
            print("error: stdin was empty — expected a prompt", file=sys.stderr)
            sys.exit(1)
        prompt = stdin_val
    if prompt is None and args.template is not None:
        tpl = app_config.templates.get(args.template)
        if tpl is not None and tpl.prompt:
            prompt = tpl.prompt
    if prompt:
        # ...unchanged cmd_run block
```

- [ ] **Step 3: Run the full test suite**

```bash
uv run python -m unittest discover tests
```

Expected: all tests pass. (test_cli_and_mcp.py uses `MagicMock` — not affected.)

- [ ] **Step 4: Smoke test the CLI end-to-end (optional, not automated)**

```bash
uv run python -c "
import tempfile, os
from pathlib import Path
with tempfile.TemporaryDirectory() as home:
    os.environ['HOME'] = home
    settings = Path(home) / '.actor' / 'settings.kdl'
    settings.parent.mkdir()
    settings.write_text('template \"qa\" { agent \"claude\"; model \"opus\" }\n')
    from actor.config import load_config
    print(load_config())
"
```

Expected: prints an `AppConfig` with the `qa` template.

- [ ] **Step 5: Commit**

```bash
git add src/actor/cli.py
git commit -m "feat: wire --template flag in actor new"
```

---

### Task 9: Add CLI argparse tests for `--template`

**Files:**
- Modify: `tests/test_cli_and_mcp.py`

- [ ] **Step 1: Add tests that exercise the argparse surface**

Insert into `tests/test_cli_and_mcp.py` inside the `NewCommandTests` class (continue the existing pattern):

```python
    def test_new_passes_template_arg_to_cmd_new(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Template
        fake_app_cfg = AppConfig(templates={
            "qa": Template(name="qa", agent="claude", prompt="run tests"),
        })

        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=fake_app_cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()), \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = fake_db
            try:
                main(["new", "foo", "--template", "qa"])
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        cmd_new.assert_called_once()
        kwargs = cmd_new.call_args.kwargs
        self.assertEqual(kwargs["template_name"], "qa")
        self.assertIsNotNone(kwargs["app_config"])
        # Template's prompt becomes the first-run prompt when none given.
        cmd_run.assert_called_once()
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "run tests")
        self.assertEqual(code, 0)

    def test_new_cli_prompt_beats_template_prompt(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Template
        fake_app_cfg = AppConfig(templates={
            "qa": Template(name="qa", agent="claude", prompt="template says run tests"),
        })

        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=fake_app_cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()), \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = fake_db
            main(["new", "foo", "custom prompt", "--template", "qa"])
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "custom prompt")
```

- [ ] **Step 2: Run the CLI tests**

```bash
uv run python -m unittest tests.test_cli_and_mcp.NewCommandTests -v
```

Expected: all pass.

- [ ] **Step 3: Run the full suite**

```bash
uv run python -m unittest discover tests
```

Expected: clean pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cli_and_mcp.py
git commit -m "test: CLI argparse wiring for --template"
```

---

### Task 10: Update docs

**Files:**
- Modify: `CLAUDE.md`
- Modify: `src/actor/_skill/SKILL.md`

- [ ] **Step 1: Add a "Config files" section to CLAUDE.md**

Insert after the "Key runtime paths" section:

```markdown
## Config files

User + project config live in `settings.kdl` files using the [KDL](https://kdl.dev) format:

- `~/.actor/settings.kdl` — user-level defaults (applies everywhere).
- `<project>/.actor/settings.kdl` — project-level; found by walking up from cwd (git-style).

Loader: `src/actor/config.py` → `load_config() -> AppConfig`. Parses via
`kdl-py`. The loader is called once per CLI invocation and passed into
`cmd_new`.

**Templates** are named bundles of agent defaults:

\`\`\`kdl
template "qa" {
    agent "claude"
    model "opus"
    effort "max"
    prompt "You're a QA engineer. Run tests, report findings."
}
\`\`\`

Apply with: `actor new fix-auth --template qa`. Precedence: project overrides user
for same template name; CLI `--config` and `--agent` flags override the template.

**Not yet implemented (follow-up tickets):** lifecycle hooks (#30), per-agent
`default-config` blocks (#31), aliases (#33). Unknown top-level nodes parse
as no-ops today for forward compatibility.
```

- [ ] **Step 2: Add a brief note to `src/actor/_skill/SKILL.md`**

Insert after the "Commands Reference" → "Create and run an actor" section:

```markdown
### Templates

If the user has templates defined in `~/.actor/settings.kdl` or
`<project>/.actor/settings.kdl`, apply one with the CLI:

\`\`\`
actor new fix-auth --template qa
actor new fix-auth --template qa --config model=haiku    # template + CLI override
\`\`\`

The MCP `new_actor` tool does not yet accept a template parameter — use
the CLI (Bash tool) when the user asks for a templated actor.
```

- [ ] **Step 3: Verify docs render sensibly**

Skim both files locally. Ensure examples match actual CLI and KDL syntax (double-quoted strings, comment syntax `//` not `#`).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md src/actor/_skill/SKILL.md
git commit -m "docs: config files + templates"
```

---

### Task 11: Final verification — full suite + smoke test

**Files:** none

- [ ] **Step 1: Clean cache and run full suite**

```bash
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
uv run python -m unittest discover tests
```

Expected: `Ran 30X tests ... OK` (X = 295 + new tests; no regressions, no errors).

- [ ] **Step 2: Smoke test --help**

```bash
uv run actor new --help
```

Expected: output includes `--template`, the new epilog line, and `--agent` default updated.

- [ ] **Step 3: Smoke test load_config isolation**

```bash
uv run python -c "from actor.config import load_config; print(load_config())"
```

Expected: prints an `AppConfig` with whatever templates exist (empty or pre-existing — not an error).

---

## Self-review checklist

- **Spec coverage:** New module ✓, KDL parser dependency ✓, precedence chain layers 1-5 implemented ✓ (6 unchanged), `--template` flag ✓, cmd_new consults Config.templates[name] ✓, `AppConfig` dataclass ✓, doc updates ✓, test plan ✓. Explicit scope-limiting notes for hooks/per-agent/aliases ✓.
- **Placeholder scan:** No "TBD", no "handle edge cases" hand-waves — every step has the actual code it needs.
- **Type consistency:** `AppConfig`, `Template`, `load_config` names match across all tasks. `template_name` / `app_config` kwargs consistent. `Config` (the existing `Dict[str, str]` alias) is kept for backward-compat; `AppConfig` is the new dataclass so they don't collide.
