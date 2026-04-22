# Per-agent defaults (redesign) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `strip-api-keys`/`default-config`/compound-bypass flags with a uniform per-agent defaults system: flat actor-keys + a `defaults { }` sub-block, hardcoded in agent classes, configurable per-user/project/template/CLI with `null` cancelling lower layers. Claude's default `permission-mode` moves to `"auto"`; Codex's compound bypass flag is replaced with explicit sandbox+approval.

**Architecture:** Agent subclasses expose two class-level dicts — `ACTOR_DEFAULTS` (flat keys, env-filtering only) and `AGENT_DEFAULTS` (keys under `defaults { }`, emitted as CLI flags). Two methods on every agent: `emit_agent_args(defaults)` → `List[str]` and `apply_actor_keys(flat, env)` → `dict`. KDL layer adds `AgentDefaults {actor_keys, agent_args}` plus `AppConfig.agent_defaults: Dict[agent, AgentDefaults]`. Merge precedence (low→high): class defaults → user kdl → project kdl → template → CLI. `null` value at any merged layer pops the key from the merged dict, so the emitter skips it.

**Tech Stack:** Python 3.10+, `kdl-py` (already a dep), `unittest`, no new runtime deps.

---

## Scope guardrails

- Rename `strip-api-keys` → `use-subscription` (same polarity: true = strip env key).
- Rename `default-config` → `defaults`.
- No back-compat — no aliases, no deprecation shim. Tests with old names get rewritten.
- No settings.kdl shipped at install time. Built-in defaults live in agent-class constants.
- Stay strictly inside this spec. Hooks (#30) and aliases (#33) stay forward-compat no-ops in the parser (no tests for them).

---

## Built-in defaults (baseline everything else merges over)

```kdl
agent "claude" {
    use-subscription true
    defaults {
        permission-mode "auto"
    }
}

agent "codex" {
    use-subscription true
    defaults {
        sandbox "danger-full-access"
        a "never"
    }
}
```

Emitter maps:
- **Claude:** `permission-mode "auto"` → `--permission-mode auto`.
- **Codex:** `sandbox "danger-full-access"` → `--sandbox danger-full-access`; `a "never"` → `-a never`.

---

## File Structure

- `src/actor/interfaces.py` — add `Agent.AGENT_DEFAULTS`, `ACTOR_DEFAULTS`, `emit_agent_args`, `apply_actor_keys`. Type `DefaultsConfig = Dict[str, Optional[str]]`.
- `src/actor/agents/claude.py` — fill the two dicts + implement the two methods; remove `_INTERNAL_KEYS` / `_permission_args` / `_config_args`; delete bypass rewrite.
- `src/actor/agents/codex.py` — same shape; drop compound bypass; `--sandbox` and `-a` come straight from `AGENT_DEFAULTS`.
- `src/actor/config.py` — add `AgentDefaults` dataclass + `AppConfig.agent_defaults`; parse `agent "claude|codex" { … defaults { … } }`; handle KDL `null`; per-key merge across user+project; reject unknown flat keys against the agent's `ACTOR_DEFAULTS` whitelist; reject `defaults { … }` at template level with helpful redirect.
- `src/actor/commands.py` — in `cmd_new`, resolve effective config: class defaults → kdl (user+project merged by `load_config`) → template → CLI `--config`. `null` handling happens during merge, so stored actor `config` is `Dict[str, str]`.
- `src/actor/cli.py` — rename `--strip-api-keys` / `--no-strip-api-keys` → `--use-subscription` / `--no-use-subscription`.
- `src/actor/__init__.py` — re-export any new public helpers; drop exports that go away.
- `src/actor/_skill/SKILL.md`, `_skill/claude-config.md`, `_skill/codex-config.md` — document flat vs defaults, null, Codex flag-name asymmetry, new defaults.
- `CLAUDE.md` — same update in the config-files section.
- `tests/test_actor.py` — rewrite `test_config_args_*` around new emitter name; remove `_INTERNAL_KEYS` assumptions.
- `tests/test_config.py` — replace `strip-api-keys` sample with `use-subscription`; add tests for `agent … defaults { }`, null-cancel, unknown-flat-key rejection.
- `tests/test_cli_and_mcp.py` — rename `--strip-api-keys` tests to `--use-subscription`; assert new emitter's flag shape.

No new files. No splits. Every edit is scoped to a file that already exists.

---

## Precedence & emitter invariants (single source of truth)

1. Each layer (class defaults, user kdl, project kdl, template, CLI) contributes a `Dict[str, Optional[str]]` for the agent-args bucket and a `Dict[str, Optional[str]]` for the actor-keys bucket.
2. `merge(low, high)` = `{k: v for k, v in {**low, **high}.items() if v is not None}` (None from the higher layer pops the key out of the merged result).
3. After final merge the dicts are `Dict[str, str]`.
4. Emitter — Claude: `--{key}` + (value if non-empty else nothing). Codex: `-{k}` for `len(key) == 1`, `--{key}` otherwise, then the value if non-empty.
5. Both agents use the SAME emitter body — only the dash-prefix rule differs. Factor a helper.
6. Actor-keys — Claude interprets `use-subscription` as env filter for `ANTHROPIC_API_KEY`. Codex interprets `use-subscription` as env filter for `OPENAI_API_KEY`. That's the only actor key today; `ACTOR_DEFAULTS` is the whitelist.

---

## Task 1 — Rename `strip-api-keys` → `use-subscription` (CLI, tests, skill text)

**Files:**
- Modify: `src/actor/cli.py` (flag + help + epilog)
- Modify: `src/actor/agents/claude.py` (ACTOR_DEFAULTS placeholder still to come in Task 3 — for now just rename the string)
- Modify: `src/actor/agents/codex.py` (same)
- Modify: `src/actor/_skill/SKILL.md`, `_skill/cli.md`, `_skill/claude-config.md`, `_skill/codex-config.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli_and_mcp.py`
- Test: `tests/test_config.py::TestLoadConfigParseShapes::test_bool_value_coerced_to_string`

- [ ] **Step 1.1: Update failing test for template bool coercion**

Edit `tests/test_config.py` lines that use `strip-api-keys` — replace with `use-subscription`:

```python
def test_bool_value_coerced_to_string(self):
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
        p = Path(cwd) / ".actor" / "settings.kdl"
        p.parent.mkdir()
        p.write_text('template "x" {\n    use-subscription true\n}\n')
        cfg = load_config(cwd=Path(cwd), home=Path(home))
        self.assertEqual(cfg.templates["x"].config["use-subscription"], "true")
```

- [ ] **Step 1.2: Update CLI tests for new flag names**

Edit `tests/test_cli_and_mcp.py`: `--no-strip-api-keys` → `--no-use-subscription`; `--strip-api-keys` → `--use-subscription`; `strip-api-keys=…` config pair → `use-subscription=…`.

- [ ] **Step 1.3: Run tests — expect FAIL (old CLI flag still in cli.py)**

```bash
uv run python -m unittest discover tests 2>&1 | tail -20
```
Expected: failures in test_cli_and_mcp (flag not recognized) and test_actor (`strip` in config default).

- [ ] **Step 1.4: Rename CLI flag + help**

Edit `src/actor/cli.py` around the `--strip-api-keys` argparse block — swap name to `--use-subscription` / `--no-use-subscription`, `dest="use_subscription"`, and update the example epilog line. Update the post-parse block that builds `config_pairs`.

- [ ] **Step 1.5: Rename inside agent code**

Edit `src/actor/agents/claude.py` line 23 and 47 — `strip-api-keys` → `use-subscription`. Same for codex.py line 32 and 71.

- [ ] **Step 1.6: Update docs**

Edit `_skill/SKILL.md` (lines referencing `strip-api-keys`), `_skill/cli.md` (line 38), `_skill/claude-config.md`, `_skill/codex-config.md`, `CLAUDE.md`. Global replace in these files (verify by grep before + after).

- [ ] **Step 1.7: Run tests — expect PASS**

```bash
uv run python -m unittest discover tests 2>&1 | tail -10
```

- [ ] **Step 1.8: Commit**

```bash
git add -A
git commit -m "Rename strip-api-keys to use-subscription (#31)"
```

---

## Task 2 — Uniform `Agent` interface: `AGENT_DEFAULTS` / `ACTOR_DEFAULTS` / `emit_agent_args` / `apply_actor_keys`

**Files:**
- Modify: `src/actor/interfaces.py`
- Modify: `src/actor/agents/claude.py`
- Modify: `src/actor/agents/codex.py`
- Modify: `src/actor/commands.py` (public wrappers `claude_config_args`, `codex_config_args`)
- Modify: `src/actor/__init__.py` (exports)
- Test: `tests/test_actor.py` (TestClaudeAgent / TestCodexAgent sections)

Implementation invariants from the spec reiterated here so the task is self-contained:
- `AGENT_DEFAULTS` / `ACTOR_DEFAULTS`: `Dict[str, Optional[str]]`, class-level, built-in baseline.
- `emit_agent_args(defaults: Dict[str, str]) -> List[str]`: straight flag mapping. Skips `None` (defensive even though resolver should have dropped them), emits bare flag for empty string, `--key value` or `-k value` otherwise. Keys sorted for deterministic output.
- `apply_actor_keys(flat: Dict[str, str], env: Dict[str, str]) -> Dict[str, str]`: returns a NEW env dict.

- [ ] **Step 2.1: Write failing test for Claude emitter**

Add to `tests/test_actor.py::TestClaudeAgent`:

```python
def test_emit_agent_args_basic(self):
    from actor.agents.claude import ClaudeAgent
    a = ClaudeAgent()
    args = a.emit_agent_args({"model": "sonnet", "permission-mode": "auto"})
    self.assertEqual(args, ["--model", "sonnet", "--permission-mode", "auto"])

def test_emit_agent_args_empty_string_is_bare_flag(self):
    from actor.agents.claude import ClaudeAgent
    a = ClaudeAgent()
    self.assertEqual(a.emit_agent_args({"verbose": ""}), ["--verbose"])

def test_apply_actor_keys_strips_anthropic_key(self):
    from actor.agents.claude import ClaudeAgent
    a = ClaudeAgent()
    env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "secret"}
    out = a.apply_actor_keys({"use-subscription": "true"}, env)
    self.assertNotIn("ANTHROPIC_API_KEY", out)
    self.assertEqual(out["PATH"], "/bin")

def test_apply_actor_keys_keeps_anthropic_key_when_false(self):
    from actor.agents.claude import ClaudeAgent
    a = ClaudeAgent()
    env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "secret"}
    out = a.apply_actor_keys({"use-subscription": "false"}, env)
    self.assertEqual(out["ANTHROPIC_API_KEY"], "secret")
```

Also replace the old `test_config_args_builds_flags` with a failing assertion that exercises `emit_agent_args` — or rather delete the old test since the public wrapper is replaced by the method.

- [ ] **Step 2.2: Write failing test for Codex emitter**

Add to `tests/test_actor.py::TestCodexAgent`:

```python
def test_emit_agent_args_short_and_long(self):
    from actor.agents.codex import CodexAgent
    c = CodexAgent()
    args = c.emit_agent_args({"m": "o3", "sandbox": "danger-full-access", "a": "never"})
    # Sorted: a < m < sandbox
    self.assertEqual(args, ["-a", "never", "-m", "o3", "--sandbox", "danger-full-access"])

def test_apply_actor_keys_strips_openai_key(self):
    from actor.agents.codex import CodexAgent
    c = CodexAgent()
    env = {"OPENAI_API_KEY": "x"}
    out = c.apply_actor_keys({"use-subscription": "true"}, env)
    self.assertNotIn("OPENAI_API_KEY", out)
```

Replace old `test_config_args_*` tests for Codex.

- [ ] **Step 2.3: Run tests — expect FAIL**

```bash
uv run python -m unittest discover tests 2>&1 | tail
```

- [ ] **Step 2.4: Implement the ABCs**

Edit `src/actor/interfaces.py`: inside `class Agent(abc.ABC):` add:

```python
# Per-agent defaults split. Subclasses fill these in.
# Keys in ACTOR_DEFAULTS are interpreted by actor-sh (env filtering etc.)
# and are NEVER passed to the agent binary. Keys in AGENT_DEFAULTS go
# under `defaults { }` in settings.kdl and map straight to CLI flags.
AGENT_DEFAULTS: "Dict[str, Optional[str]]" = {}
ACTOR_DEFAULTS: "Dict[str, Optional[str]]" = {}

@abc.abstractmethod
def emit_agent_args(self, defaults: "Config") -> List[str]:
    """Turn the resolved `defaults { }` dict into CLI flags."""

@abc.abstractmethod
def apply_actor_keys(self, flat: "Config", env: Dict[str, str]) -> Dict[str, str]:
    """Return a NEW env dict with actor-key side effects applied (e.g. stripping API keys)."""
```

Add imports: `from typing import Dict, List, Optional`.

- [ ] **Step 2.5: Implement Claude side**

Edit `src/actor/agents/claude.py`:

```python
class ClaudeAgent(Agent):
    AGENT_DEFAULTS: Dict[str, Optional[str]] = {
        "permission-mode": "auto",
    }
    ACTOR_DEFAULTS: Dict[str, Optional[str]] = {
        "use-subscription": "true",
    }

    def emit_agent_args(self, defaults: Config) -> List[str]:
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            if value is None:
                continue
            args.append(f"--{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(self, flat: Config, env: Dict[str, str]) -> Dict[str, str]:
        out = dict(env)
        if flat.get("use-subscription", "true") != "false":
            out.pop("ANTHROPIC_API_KEY", None)
        return out
```

Delete `_INTERNAL_KEYS`, `_permission_args`, `_config_args`. Update `_spawn_and_track` to call `apply_actor_keys(config, os.environ)` instead of the inline strip. Update `start`, `resume`, `interactive_argv` to use `emit_agent_args(agent_args)` instead of `_permission_args + _config_args` — see Task 4 for the split of `config` into actor vs agent buckets.

- [ ] **Step 2.6: Implement Codex side**

Edit `src/actor/agents/codex.py`:

```python
class CodexAgent(Agent):
    AGENT_DEFAULTS: Dict[str, Optional[str]] = {
        "sandbox": "danger-full-access",
        "a": "never",
    }
    ACTOR_DEFAULTS: Dict[str, Optional[str]] = {
        "use-subscription": "true",
    }

    def emit_agent_args(self, defaults: Config) -> List[str]:
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            if value is None:
                continue
            prefix = "-" if len(key) == 1 else "--"
            args.append(f"{prefix}{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(self, flat: Config, env: Dict[str, str]) -> Dict[str, str]:
        out = dict(env)
        if flat.get("use-subscription", "true") != "false":
            out.pop("OPENAI_API_KEY", None)
        return out
```

Delete `_INTERNAL_KEYS`, `_permission_args`, `_config_args`. Update `_spawn_and_capture` to use `apply_actor_keys`, and `start/resume/interactive_argv` to use `emit_agent_args`.

NOTE: The split "which keys are actor-keys vs agent-args" lives in Task 4. For this task, write the methods assuming the caller has already split — and update `start/resume/interactive_argv` to use a temporary helper `_split_config(config)` that partitions by `ACTOR_DEFAULTS.keys()`.

- [ ] **Step 2.7: Update public wrappers + exports**

Remove `claude_config_args` and `codex_config_args` from `src/actor/commands.py` and `src/actor/__init__.py` `__all__`. They expose the old shape.

- [ ] **Step 2.8: Run tests — expect PASS**

```bash
uv run python -m unittest discover tests 2>&1 | tail -10
```

- [ ] **Step 2.9: Commit**

```bash
git add -A
git commit -m "Agent.AGENT_DEFAULTS/ACTOR_DEFAULTS + emit_agent_args/apply_actor_keys (#31)"
```

---

## Task 3 — New defaults + behavior changes

**Files:**
- Modify: `src/actor/agents/claude.py`
- Modify: `src/actor/agents/codex.py`
- Test: `tests/test_actor.py::TestClaudeAgent::test_permission_mode_default_is_auto`
- Test: `tests/test_actor.py::TestClaudeAgent::test_bypass_permissions_emits_straight`
- Test: `tests/test_actor.py::TestCodexAgent::test_default_emits_explicit_sandbox_and_approval`

- [ ] **Step 3.1: Write failing tests**

```python
# In TestClaudeAgent:
def test_permission_mode_default_is_auto(self):
    from actor.agents.claude import ClaudeAgent
    self.assertEqual(ClaudeAgent.AGENT_DEFAULTS.get("permission-mode"), "auto")

def test_bypass_permissions_emits_straight(self):
    from actor.agents.claude import ClaudeAgent
    a = ClaudeAgent()
    args = a.emit_agent_args({"permission-mode": "bypassPermissions"})
    self.assertEqual(args, ["--permission-mode", "bypassPermissions"])

# In TestCodexAgent:
def test_default_emits_explicit_sandbox_and_approval(self):
    from actor.agents.codex import CodexAgent
    self.assertEqual(
        CodexAgent.AGENT_DEFAULTS,
        {"sandbox": "danger-full-access", "a": "never"},
    )
```

- [ ] **Step 3.2: Run tests — should pass if Task 2 was done right**

Task 2 already sets the constants correctly and removes the rewrite.  If any of the three fail, fix in the spec shape (not add back a rewrite).

- [ ] **Step 3.3: Manual smoke test against real CLIs**

```bash
# Both must exit 0 or show usage — we're checking the flag set is accepted.
claude --permission-mode auto -p --help >/dev/null 2>&1 && echo "claude OK" || echo "claude FAIL"
codex exec --sandbox danger-full-access -a never --help >/dev/null 2>&1 && echo "codex OK" || echo "codex FAIL"
```

If the binaries aren't available locally, note the skipped smoke test in the commit body; CI has them.

- [ ] **Step 3.4: Commit**

```bash
git add -A
git commit -m "Claude default permission-mode=auto; Codex explicit sandbox+approval (#31)"
```

---

## Task 4 — Split `config` into actor-keys vs agent-args at the call site

**Files:**
- Modify: `src/actor/agents/claude.py` — `start`, `resume`, `interactive_argv`, `_spawn_and_track`
- Modify: `src/actor/agents/codex.py` — `start`, `resume`, `interactive_argv`, `_spawn_and_capture`
- Test: `tests/test_actor.py` — a small test that `start`'s argv contains `--permission-mode auto` when `config={"permission-mode": "auto"}` and does NOT contain `--use-subscription` (because it's an actor key).

- [ ] **Step 4.1: Add `_split_config` helper on Agent base class**

In `src/actor/interfaces.py`:

```python
def _split_config(self, config: "Config") -> tuple["Config", "Config"]:
    """Partition a flat config dict into (actor_keys, agent_args)."""
    actor_keys = {k: v for k, v in config.items() if k in self.ACTOR_DEFAULTS}
    agent_args = {k: v for k, v in config.items() if k not in self.ACTOR_DEFAULTS}
    return actor_keys, agent_args
```

- [ ] **Step 4.2: Update ClaudeAgent.start/resume/interactive_argv + _spawn_and_track**

```python
def _spawn_and_track(self, args, cwd, config):
    actor_keys, _ = self._split_config(config)
    env = self.apply_actor_keys(actor_keys, os.environ)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=str(cwd), env=env)
    ...

def start(self, dir, prompt, config):
    _, agent_args = self._split_config(config)
    session_id = str(uuid.uuid4())
    args = ["claude", *self._CHANNEL_ARGS, "-p", "--session-id", session_id,
            *self.emit_agent_args(agent_args), "--", prompt]
    pid = self._spawn_and_track(args, dir, config)
    return pid, session_id
```

Do the same shape in `resume` and `interactive_argv`. Remove the old `_permission_args` / `_config_args` call sites.

- [ ] **Step 4.3: Update CodexAgent equivalents (same pattern)**

- [ ] **Step 4.4: Write test asserting argv flag presence**

Hard to test argv directly without stubbing Popen. Skip end-to-end assertion; rely on `emit_agent_args` tests + manual smoke test in Task 3. Move on.

- [ ] **Step 4.5: Run tests**

```bash
uv run python -m unittest discover tests 2>&1 | tail -10
```

- [ ] **Step 4.6: Commit**

```bash
git add -A
git commit -m "Split config into actor_keys/agent_args at agent call sites (#31)"
```

---

## Task 5 — KDL parser: `agent "X" { flat… defaults { … } }` with null support

**Files:**
- Modify: `src/actor/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 5.1: Write failing tests**

```python
class TestLoadConfigAgentBlocks(unittest.TestCase):
    def _write_kdl(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_agent_defaults_parsed(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write_kdl(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    use-subscription false\n'
                '    defaults {\n'
                '        permission-mode "bypassPermissions"\n'
                '        model "opus"\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.actor_keys, {"use-subscription": "false"})
            self.assertEqual(d.agent_args, {"permission-mode": "bypassPermissions", "model": "opus"})

    def test_null_in_defaults_represented_as_none(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write_kdl(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    defaults {\n'
                '        permission-mode #null\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertIn("permission-mode", d.agent_args)
            self.assertIsNone(d.agent_args["permission-mode"])

    def test_unknown_agent_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('agent "bogus" { defaults { x 1 } }\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("bogus", str(ctx.exception))

    def test_unknown_flat_key_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('agent "claude" { not-a-real-flat-key "x" }\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("not-a-real-flat-key", str(ctx.exception))

    def test_project_agent_defaults_override_user_per_key(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write_kdl(
                Path(home) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    defaults {\n'
                '        model "sonnet"\n'
                '        permission-mode "auto"\n'
                '    }\n'
                '}\n',
            )
            self._write_kdl(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    defaults {\n'
                '        permission-mode #null\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.agent_args.get("model"), "sonnet")
            self.assertNotIn("permission-mode", d.agent_args)

    def test_defaults_block_at_template_level_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('template "qa" { defaults { model "opus" } }\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("belong under", str(ctx.exception))
```

- [ ] **Step 5.2: Run tests — expect FAIL**

```bash
uv run python -m unittest discover tests.test_config 2>&1 | tail -15
```

- [ ] **Step 5.3: Implement `AgentDefaults` + parser**

Edit `src/actor/config.py`:

```python
from typing import Dict, Optional, Tuple

_VALID_AGENTS = ("claude", "codex")

@dataclass
class AgentDefaults:
    actor_keys: Dict[str, Optional[str]] = field(default_factory=dict)
    agent_args: Dict[str, Optional[str]] = field(default_factory=dict)

@dataclass
class AppConfig:
    templates: Dict[str, Template] = field(default_factory=dict)
    agent_defaults: Dict[str, AgentDefaults] = field(default_factory=dict)
```

Add helper `_coerce_value_or_none(raw)` that returns `None` for the kdl `null` literal (kdl-py yields Python `None` already) and otherwise delegates to `_coerce_value`. Handle null before the type check.

Add `_parse_agent_block(node, source) -> (agent_name, AgentDefaults)` that:
- Requires `len(node.args) == 1` and `node.args[0] in _VALID_AGENTS`.
- Iterates children — `defaults` child (no args) handled specially; its nested children become `agent_args` (nulls preserved as None). All other children are flat keys and go into `actor_keys`.
- If a flat child has no args → ConfigError (same message shape as templates).
- Reject nested `defaults` inside `defaults` with a helpful message.
- Reject a `defaults "x"` with positional args (forbid second-arg form per spec).

Update `_parse_kdl_file` top-level dispatch:
- `"template"` → `_parse_template`
- `"agent"` → `_parse_agent_block`; duplicate agent names raise ConfigError.
- Anything else (`hooks`, `alias`, etc.) — still silently ignored per the forward-compat rule.

Update `_parse_template`:
- If a child key is `"defaults"` and it's a block (no args, has nodes), raise with "`defaults { ... }` blocks belong under `agent \"claude\" { … }` / `agent \"codex\" { … }`, not inside a template". Keep the existing checks after.

Update `_merge`:

```python
def _merge(base: AppConfig, over: AppConfig) -> AppConfig:
    merged_templates = {**base.templates, **over.templates}
    merged_defaults: Dict[str, AgentDefaults] = {}
    for agent in set(base.agent_defaults) | set(over.agent_defaults):
        b = base.agent_defaults.get(agent, AgentDefaults())
        o = over.agent_defaults.get(agent, AgentDefaults())
        merged_defaults[agent] = AgentDefaults(
            actor_keys=_merge_dict(b.actor_keys, o.actor_keys),
            agent_args=_merge_dict(b.agent_args, o.agent_args),
        )
    # Drop empty AgentDefaults so tests can assert `"claude" not in cfg.agent_defaults`.
    merged_defaults = {
        k: v for k, v in merged_defaults.items()
        if v.actor_keys or v.agent_args
    }
    return AppConfig(templates=merged_templates, agent_defaults=merged_defaults)

def _merge_dict(
    low: Dict[str, Optional[str]], high: Dict[str, Optional[str]]
) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for k, v in low.items():
        if v is not None:
            out[k] = v
    for k, v in high.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out
```

Note: the parser layer preserves None values inside a single file (so the test `test_null_in_defaults_represented_as_none` can read them back). `_merge_dict` is what actually cancels. At final load time after all merges, surviving Nones are filtered out — or we can document that the parser-level AgentDefaults retains Nones and only the merged result is None-free. Simpler: `_merge_dict` drops Nones as it merges. If only one file is loaded, no merge happens — the parser-retained Nones propagate to the caller. Adjust the `_parse_single` layer to leave Nones, and add a final pass in `load_config` that calls `_merge_dict(AgentDefaults(), parsed)` on the single-file path so None-stripping is uniform.

Actually simpler: `load_config` always calls `_merge` (with an empty `AppConfig` as base), so all surviving Nones get dropped on the way out. The per-file parser can hold Nones, but they never leak to callers of `load_config`. Tests for the "null parsed as None" case need to inspect AT the parser level (call `_parse_kdl_file` directly) — update test accordingly.

Revised test for null:

```python
def test_null_in_defaults_is_stripped_by_merge(self):
    # Null cancels the key in the merged result. Starting from an empty
    # base, null at top level is equivalent to omitting the key.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
        self._write_kdl(
            Path(cwd) / ".actor" / "settings.kdl",
            'agent "claude" {\n'
            '    defaults {\n'
            '        permission-mode #null\n'
            '    }\n'
            '}\n',
        )
        cfg = load_config(cwd=Path(cwd), home=Path(home))
        self.assertNotIn("claude", cfg.agent_defaults)
```

Replace step 5.1's `test_null_in_defaults_represented_as_none` with this.

- [ ] **Step 5.4: Run tests — expect PASS**

- [ ] **Step 5.5: Commit**

```bash
git add -A
git commit -m "Parse agent \"X\" { flat; defaults { ... } } with null-cancel semantics (#31)"
```

---

## Task 6 — Wire per-agent defaults into `cmd_new`

**Files:**
- Modify: `src/actor/commands.py` (`cmd_new`)
- Test: `tests/test_actor.py::TestCmdNewTemplates` — add precedence tests

- [ ] **Step 6.1: Write failing test for precedence ladder**

```python
class TestCmdNewAgentDefaults(unittest.TestCase):
    def test_agent_defaults_applied_as_lowest_layer(self):
        # AGENT_DEFAULTS baseline + kdl + template + CLI, in precedence order.
        from actor import AppConfig, Template
        from actor.config import AgentDefaults

        db, _, git, pm = _fresh_db_and_fakes()
        app = AppConfig(
            templates={"qa": Template(name="qa", config={"permission-mode": "plan"})},
            agent_defaults={
                "claude": AgentDefaults(
                    actor_keys={"use-subscription": "false"},
                    agent_args={"model": "opus"},
                ),
            },
        )
        a = cmd_new(db, git, name="foo", dir=None, no_worktree=True, base=None,
                    agent_name="claude",
                    config_pairs=["model=haiku"],  # CLI wins
                    template_name="qa",            # template supplies permission-mode=plan
                    app_config=app)
        self.assertEqual(a.config.get("model"), "haiku")                  # CLI > kdl
        self.assertEqual(a.config.get("permission-mode"), "plan")          # template
        self.assertEqual(a.config.get("use-subscription"), "false")        # kdl flat key
        # Class-level AGENT_DEFAULTS baseline must also show when nobody overrode it:
        # (auto for Claude's permission-mode was overridden by template here, so we test
        # the absence case with a separate scenario.)

    def test_class_defaults_applied_when_nothing_else_sets_key(self):
        from actor import AppConfig
        db, _, git, pm = _fresh_db_and_fakes()
        app = AppConfig()  # empty
        a = cmd_new(db, git, name="bar", dir=None, no_worktree=True, base=None,
                    agent_name="claude",
                    config_pairs=[], template_name=None, app_config=app)
        self.assertEqual(a.config.get("permission-mode"), "auto")
        self.assertEqual(a.config.get("use-subscription"), "true")
```

Add a helper `_fresh_db_and_fakes()` if one doesn't already exist; mirror the existing pattern used in `test_actor.py` for fake DBs.

- [ ] **Step 6.2: Run tests — expect FAIL**

- [ ] **Step 6.3: Implement precedence in cmd_new**

Edit `src/actor/commands.py::cmd_new`. After the `agent_kind` is resolved and before the current `config` dict is built, insert:

```python
# Precedence ladder (low → high):
#   1. Agent class hardcoded defaults
#   2. User+project kdl agent_defaults (already merged by load_config)
#   3. Template config
#   4. CLI --config
agent_cls = type(_create_agent(agent_kind))
# Start with class baseline — ACTOR_DEFAULTS and AGENT_DEFAULTS are all merged
# into a single flat dict since the actor stores one `config`.
config: Dict[str, Optional[str]] = {}
for k, v in agent_cls.ACTOR_DEFAULTS.items():
    if v is not None:
        config[k] = v
for k, v in agent_cls.AGENT_DEFAULTS.items():
    if v is not None:
        config[k] = v

# KDL layer
if app_config is not None:
    defaults = app_config.agent_defaults.get(agent_kind.value)
    if defaults is not None:
        for k, v in defaults.actor_keys.items():
            if v is None:
                config.pop(k, None)
            else:
                config[k] = v
        for k, v in defaults.agent_args.items():
            if v is None:
                config.pop(k, None)
            else:
                config[k] = v

# Template layer (flat bag — agent_kind already decided above)
if template is not None:
    for k, v in template.config.items():
        config[k] = v

# CLI layer (flat bag)
for k, v in parse_config(config_pairs).items():
    config[k] = v

# Strip Nones (safety net) and sort for determinism.
config_final: Dict[str, str] = {k: v for k, v in config.items() if v is not None}
config_final = _sorted_config(config_final)
```

Replace the existing build with this. Keep the rest of `cmd_new` the same. `actor.config = config_final`.

- [ ] **Step 6.4: Run tests**

- [ ] **Step 6.5: Commit**

```bash
git add -A
git commit -m "Apply per-agent defaults as lowest layer in cmd_new (#31)"
```

---

## Task 7 — Skill + CLAUDE.md docs

**Files:**
- Modify: `src/actor/_skill/SKILL.md` (settings.kdl block: explain flat vs defaults, null, Codex flag-name quirk, new defaults text; update "Important Notes" at the bottom).
- Modify: `src/actor/_skill/claude-config.md` (permission-mode default → auto; document bypassPermissions is still reachable explicitly).
- Modify: `src/actor/_skill/codex-config.md` (sandbox + approval default; drop the compound flag reference).
- Modify: `CLAUDE.md` (extend the "Config files & templates" section to cover per-agent defaults and null semantics).

- [ ] **Step 7.1: SKILL.md — add a subsection under "Templates" titled "Per-agent defaults"**

Describe:
```kdl
agent "claude" {
    use-subscription true
    defaults {
        permission-mode "auto"
        model "opus"
    }
}
```
- Flat keys = actor-sh controls (only `use-subscription` today).
- `defaults { }` keys = agent CLI flags.
- Claude uses semantic names (`model`, `permission-mode`). Codex uses the binary's native short flags (`m`, `a`, `sandbox`). No translation layer on either side.
- `null` cancels a lower-precedence value (e.g. project file sets `permission-mode #null` to erase user file's bypass setting).
- Precedence low→high: class defaults → user kdl → project kdl → template → CLI.

- [ ] **Step 7.2: claude-config.md — flip default**

Update permission-mode options: move `auto` to "default" and explain that `bypassPermissions` is opt-in for autonomous worktree runs.

- [ ] **Step 7.3: codex-config.md — drop compound, add explicit**

Remove the "When neither sandbox nor approval is set" paragraphs. State the default is explicit: `sandbox=danger-full-access` + `a=never`. Document that Codex config uses Codex's own flag names verbatim.

- [ ] **Step 7.4: CLAUDE.md — extend config section**

Add a paragraph under "Config files & templates" covering per-agent defaults, null semantics, Codex asymmetry.

- [ ] **Step 7.5: Commit**

```bash
git add -A
git commit -m "Docs: per-agent defaults, null semantics, new Claude/Codex defaults (#31)"
```

---

## Task 8 — Full test sweep + CR loop prep

- [ ] **Step 8.1: Full test suite**

```bash
uv run python -m unittest discover tests 2>&1 | tail -10
```

All green. If anything else referenced `claude_config_args`, `codex_config_args`, `_INTERNAL_KEYS`, `_permission_args`, `_config_args`, `--strip-api-keys`, or `default-config`, fix now.

- [ ] **Step 8.2: Grep for stragglers**

```bash
grep -rn "strip-api-keys\|strip_api_keys\|default-config\|_INTERNAL_KEYS\|claude_config_args\|codex_config_args\|_permission_args\|_config_args\|dangerously-skip-permissions\|dangerously-bypass-approvals-and-sandbox" src/ tests/ CLAUDE.md 2>/dev/null
```

Expected: no hits. Any hit = residual doc or test needs updating.

- [ ] **Step 8.3: Push branch**

```bash
git push -u origin per-agent-defaults-redesign
```

- [ ] **Step 8.4: Open PR**

```bash
gh pr create --title "Per-agent defaults (redesign) (#31)" --body "$(cat <<'EOF'
## Summary

- Per-agent defaults in `settings.kdl` split into flat actor-keys and a `defaults { }` sub-block (passed to the agent binary as flags).
- `strip-api-keys` → `use-subscription`; `default-config` → `defaults`. No back-compat shim.
- Agent subclasses expose `ACTOR_DEFAULTS` + `AGENT_DEFAULTS` class constants and `emit_agent_args` / `apply_actor_keys` methods. Emitter is straight flag mapping — Claude uses `--key`, Codex uses `-k` for 1-char keys and `--key` otherwise.
- Built-in defaults: Claude `permission-mode "auto"` (replaces `bypassPermissions`); Codex `sandbox "danger-full-access"` + `a "never"` (replaces the compound bypass flag).
- `null` in any layer cancels a lower-precedence value.

## Test plan

- [ ] Unit tests cover: emit_agent_args for both agents, apply_actor_keys env filtering, kdl parse for agent blocks + null + unknown-key rejection, precedence ladder in cmd_new.
- [ ] Manual smoke: `claude --permission-mode auto` and `codex exec --sandbox danger-full-access -a never` both accepted.

Closes #31
EOF
)"
```

- [ ] **Step 8.5: Run the cr-loop**

Invoke the `cr-loop` skill with Path A (7 agents, verbatim Law 3 prompt). Loop until a 7-agent round returns zero findings. Do not exit dirty.

---

## Self-Review

Spec coverage:

| Spec item | Task covering it |
|---|---|
| Rename `strip-api-keys` → `use-subscription` | Task 1 |
| Rename `default-config` → `defaults` | Task 5 |
| `agent "X" { flat; defaults { } }` shape, no second-arg form | Task 5 |
| No `settings.kdl` shipped; hardcoded in class constants | Task 2 (constants) |
| Precedence: class → user → project → template → CLI | Task 6 |
| `null` cancels | Task 5 (`_merge_dict`) |
| Claude: `key "value"` → `--key value`; empty string → bare `--key` | Task 2 (Claude emitter) |
| Codex: `-k` or `--key` based on length | Task 2 (Codex emitter) |
| Claude default `permission-mode "auto"` | Task 3 |
| Drop `bypassPermissions` → `--dangerously-skip-permissions` rewrite | Task 2 (remove `_permission_args`) |
| Drop Codex compound bypass flag | Task 2 (remove `_permission_args`) |
| Codex explicit defaults (sandbox + a) | Task 3 |
| Uniform `Agent` interface (AGENT_DEFAULTS/ACTOR_DEFAULTS/emit/apply) | Task 2 |
| Fix double `--permission-mode` emit bug | Task 2 (unified emitter removes the helper that emitted it twice) |
| Docs updates (CLAUDE.md, SKILL.md, configs) | Task 7 |
| cr-loop Path A, no exit-dirty | Task 8 |
| `gh pr close 50` + fresh PR title | Already done; Task 8 opens the new PR |
| No new deps; pre-existing tests still pass (except rewritten) | Constraints respected throughout |

Placeholder scan: none. Every step has the concrete edit or command.

Type consistency: `Dict[str, Optional[str]]` everywhere until the final merged storage, which is `Dict[str, str]` (matching `Actor.config`). `emit_agent_args` takes `Config` (= `Dict[str, str]`) and defensively skips Nones.
