#!/usr/bin/env python3
"""Tests for src/actor/config.py — KDL loader + templates."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actor.config import AppConfig, load_config
from actor.errors import ConfigError


class TestLoadConfigEmpty(unittest.TestCase):

    def test_no_files_returns_empty_config(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIsInstance(cfg, AppConfig)
            self.assertEqual(cfg.templates, {})
            self.assertEqual(cfg.agent_defaults, {})

    def test_missing_user_file_but_project_file_loads_project(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            proj = Path(cwd) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'template "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertEqual(cfg.templates["qa"].agent, "claude")


class TestLoadConfigPrecedence(unittest.TestCase):

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_project_overrides_user_same_template_name(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'template "qa" {\n    agent "claude"\n    model "sonnet"\n}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'template "qa" {\n    agent "codex"\n    model "opus"\n}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["qa"].agent, "codex")
            self.assertEqual(cfg.templates["qa"].config["model"], "opus")

    def test_user_and_project_both_contribute_distinct_templates(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'template "qa" {\n    agent "claude"\n}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'template "reviewer" {\n    agent "claude"\n}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertIn("reviewer", cfg.templates)


class TestLoadConfigWalkUp(unittest.TestCase):

    def test_project_config_found_by_walking_up_from_cwd(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            proj = Path(root) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'template "qa" {\n    agent "claude"\n}\n'
            )
            deep = Path(root) / "src" / "nested" / "deeper"
            deep.mkdir(parents=True)
            cfg = load_config(cwd=deep, home=Path(home))
            self.assertIn("qa", cfg.templates)


class TestLoadConfigErrors(unittest.TestCase):

    def test_malformed_kdl_raises_config_error_with_path(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('template "qa" {\n    unclosed\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn(str(bad), str(ctx.exception))

    def test_template_without_name_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('template {\n    agent "claude"\n}\n')
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))

    def test_template_child_without_value_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('template "qa" {\n    agent\n}\n')
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
                '    prompt "You\'re a QA engineer."\n'
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
            p.write_text('template "x" {\n    strip-api-keys true\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["x"].config["strip-api-keys"], "true")

    def test_int_value_coerced_without_trailing_zero(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('template "x" {\n    max-budget-usd 5\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["x"].config["max-budget-usd"], "5")

    def test_unknown_top_level_nodes_are_ignored(self):
        # Forward-compat: hooks/alias belong to tickets #30 / #33 and
        # should parse as no-ops today rather than erroring.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'hooks {\n    on-start "echo hi"\n}\n'
                'alias "max" template="qa"\n'
                'template "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.templates)
            self.assertEqual(len(cfg.templates), 1)


class TestLoadConfigParseStrict(unittest.TestCase):
    """Parser should reject silently-dropped input (ambiguous user intent)."""

    def _expect_error(self, kdl_text: str, needle: str) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(kdl_text)
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn(needle, str(ctx.exception))

    def test_extra_args_on_template_node_raises(self):
        self._expect_error(
            'template "qa" "extra" {\n    agent "claude"\n}\n',
            "extra",
        )

    def test_extra_args_on_child_node_raises(self):
        self._expect_error(
            'template "qa" {\n    model "opus" "sonnet"\n}\n',
            "model",
        )

    def test_props_on_child_node_raises(self):
        self._expect_error(
            'template "qa" {\n    model name="opus"\n}\n',
            "model",
        )

    def test_duplicate_key_in_template_raises(self):
        self._expect_error(
            'template "qa" {\n    model "opus"\n    model "sonnet"\n}\n',
            "model",
        )

    def test_duplicate_template_name_in_file_raises(self):
        self._expect_error(
            'template "qa" {\n    agent "claude"\n}\n'
            'template "qa" {\n    agent "codex"\n}\n',
            "qa",
        )

    def test_non_string_agent_value_raises(self):
        self._expect_error(
            'template "qa" {\n    agent 42\n}\n',
            "agent",
        )

    def test_non_string_prompt_value_raises(self):
        self._expect_error(
            'template "qa" {\n    prompt 42\n}\n',
            "prompt",
        )

    def test_props_on_template_node_raises(self):
        self._expect_error(
            'template "qa" flag="x" {\n    agent "claude"\n}\n',
            "qa",
        )

    def test_empty_template_name_raises(self):
        self._expect_error(
            'template "" {\n    agent "claude"\n}\n',
            "non-empty",
        )

    def test_unknown_agent_in_template_raises(self):
        # Mirror the agent-block validator: a typo in template `agent`
        # should also surface as ConfigError with the valid allowlist,
        # not wait until `cmd_new` raises ActorError later.
        self._expect_error(
            'template "qa" {\n    agent "cluade"\n}\n',
            "unknown agent 'cluade'",
        )


class TestLoadConfigCoercion(unittest.TestCase):

    def test_non_integer_float_keeps_decimal(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('template "x" {\n    temperature 0.5\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.templates["x"].config["temperature"], "0.5")


class TestLoadConfigHomeUnset(unittest.TestCase):

    def test_home_none_skips_user_config_and_loads_project(self):
        with tempfile.TemporaryDirectory() as cwd:
            proj = Path(cwd) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'template "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=None)
            self.assertIn("qa", cfg.templates)


class TestLoadConfigCwdUnderHome(unittest.TestCase):
    """Walk-up must stop before re-parsing the user config as a 'project'."""

    def test_cwd_inside_home_does_not_double_load_user_config(self):
        # cwd sits inside home, and only the user config exists.
        # Without the fix, walk-up finds the user config as the "project"
        # path too, and duplicate template names would merge over
        # themselves — and worse, a strict duplicate-template check would
        # wrongly fire on the second parse.
        with tempfile.TemporaryDirectory() as home:
            home_p = Path(home)
            (home_p / ".actor").mkdir()
            (home_p / ".actor" / "settings.kdl").write_text(
                'template "qa" {\n    agent "claude"\n}\n'
            )
            sub = home_p / "work" / "repo"
            sub.mkdir(parents=True)
            cfg = load_config(cwd=sub, home=home_p)
            self.assertIn("qa", cfg.templates)
            self.assertEqual(cfg.templates["qa"].agent, "claude")

    def test_project_config_found_even_when_cwd_is_inside_home(self):
        # Project config at <home>/work/repo/.actor/settings.kdl is still
        # picked up when cwd is there (and wins on override).
        with tempfile.TemporaryDirectory() as home:
            home_p = Path(home)
            (home_p / ".actor").mkdir()
            (home_p / ".actor" / "settings.kdl").write_text(
                'template "qa" {\n    agent "claude"\n}\n'
            )
            proj = home_p / "work" / "repo"
            (proj / ".actor").mkdir(parents=True)
            (proj / ".actor" / "settings.kdl").write_text(
                'template "qa" {\n    agent "codex"\n}\n'
            )
            cfg = load_config(cwd=proj, home=home_p)
            self.assertEqual(cfg.templates["qa"].agent, "codex")


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
        # tickets; an agent block with no `default-config` child parses
        # without error and contributes no defaults.
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
            "must be a string",
        )

    def test_extra_positional_on_agent_raises(self):
        self._expect_error(
            'agent "claude" "extra" {\n    default-config {\n    }\n}\n',
            "extra",
        )

    def test_props_on_agent_node_raises(self):
        self._expect_error(
            'agent "claude" flag="x" {\n    default-config {\n    }\n}\n',
            "does not accept properties",
        )

    def test_duplicate_agent_block_in_file_raises(self):
        self._expect_error(
            'agent "claude" {\n    default-config {\n        model "opus"\n    }\n}\n'
            'agent "claude" {\n    default-config {\n        model "sonnet"\n    }\n}\n',
            "duplicate agent block 'claude'",
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
            "does not accept properties",
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

    def test_props_on_default_config_block_raises(self):
        self._expect_error(
            'agent "claude" {\n'
            '    default-config flag="x" {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n',
            "default-config",
        )

    def test_default_config_as_template_child_raises_helpful_error(self):
        self._expect_error(
            'template "qa" {\n'
            '    agent "claude"\n'
            '    default-config {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n',
            "default-config",
        )

    def test_duplicate_agent_block_detected_even_if_first_is_empty(self):
        # Regression: the duplicate check used to key off
        # `cfg.agent_defaults`, which skipped empty blocks. A first empty
        # block would therefore let a second block pass unchecked.
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '    }\n'
            '}\n'
            'agent "claude" {\n'
            '    default-config {\n'
            '        model "opus"\n'
            '    }\n'
            '}\n',
            "duplicate agent block 'claude'",
        )

    def test_duplicate_agent_block_detected_when_both_empty(self):
        self._expect_error(
            'agent "claude" {\n}\n'
            'agent "claude" {\n}\n',
            "duplicate agent block 'claude'",
        )

    def test_top_level_default_config_raises_helpful_error(self):
        # A top-level `default-config {}` is almost always a misread of
        # the schema. Without this check the keys would silently vanish
        # into the forward-compat no-op bucket.
        self._expect_error(
            'default-config {\n'
            '    model "opus"\n'
            '}\n',
            "top-level `default-config`",
        )

    def test_key_with_value_directly_under_agent_raises_helpful_error(self):
        # User writing `model "opus"` directly under `agent "claude"`
        # (forgetting the `default-config {}` wrapper) would otherwise
        # see their config silently dropped.
        self._expect_error(
            'agent "claude" {\n'
            '    model "opus"\n'
            '}\n',
            "nest config keys",
        )

    def test_prompt_key_in_default_config_raises(self):
        # `prompt` is a template-level field; silently allowing it under
        # `default-config` would otherwise emit `--prompt <value>` as a
        # CLI flag on every spawn — almost never the user's intent.
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        prompt "be brief"\n'
            '    }\n'
            '}\n',
            "'prompt' is not a valid default-config key",
        )

    def test_agent_key_in_default_config_raises(self):
        # Same reasoning: `agent` belongs at template level, not inside
        # the block whose scope is already one agent.
        self._expect_error(
            'agent "claude" {\n'
            '    default-config {\n'
            '        agent "codex"\n'
            '    }\n'
            '}\n',
            "'agent' is not a valid default-config key",
        )

    def test_forward_compat_block_children_of_agent_still_silently_parse(self):
        # The key-with-value guard must not break forward-compat: a
        # block-style child (e.g. `hooks { … }` from #30) has no args
        # and should still be ignored as a no-op.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'agent "claude" {\n'
                '    hooks {\n'
                '        on-start "echo hi"\n'
                '    }\n'
                '    default-config {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.agent_defaults["claude"], {"model": "opus"})


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

    def test_asymmetric_mix_user_has_two_agents_project_only_one(self):
        # User defines both claude + codex; project only touches claude.
        # Expect: claude per-key merges (project wins), codex passes
        # through unchanged.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "opus"\n'
                '        effort "max"\n'
                '    }\n'
                '}\n'
                'agent "codex" {\n'
                '    default-config {\n'
                '        sandbox "danger-full-access"\n'
                '    }\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'agent "claude" {\n'
                '    default-config {\n'
                '        model "sonnet"\n'
                '    }\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.agent_defaults["claude"], {
                "model": "sonnet", "effort": "max",
            })
            self.assertEqual(cfg.agent_defaults["codex"], {
                "sandbox": "danger-full-access",
            })

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


class TestMergeInvariant(unittest.TestCase):
    """`_merge` must preserve the parser invariant 'presence in
    agent_defaults implies at least one declared key' even when callers
    construct AppConfig programmatically."""

    def test_merge_skips_empty_dict_from_over(self):
        from actor.config import _merge
        base = AppConfig()
        over = AppConfig(agent_defaults={"claude": {}})
        merged = _merge(base, over)
        self.assertNotIn("claude", merged.agent_defaults)

    def test_merge_skips_empty_dict_without_clobbering_existing(self):
        from actor.config import _merge
        base = AppConfig(agent_defaults={"claude": {"model": "opus"}})
        over = AppConfig(agent_defaults={"claude": {}})
        merged = _merge(base, over)
        self.assertEqual(merged.agent_defaults["claude"], {"model": "opus"})

    def test_merge_skips_empty_dict_from_base(self):
        # Symmetric with `test_merge_skips_empty_dict_from_over`: an
        # empty dict sitting in `base.agent_defaults` (e.g. a
        # programmatically constructed AppConfig) should not leak into
        # the merged result as a bare agent key with no configuration.
        from actor.config import _merge
        base = AppConfig(agent_defaults={"claude": {}})
        over = AppConfig()
        merged = _merge(base, over)
        self.assertNotIn("claude", merged.agent_defaults)


if __name__ == "__main__":
    unittest.main()
