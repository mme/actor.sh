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
        # Forward-compat: hooks/agent/alias exist in follow-up tickets but
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


if __name__ == "__main__":
    unittest.main()
