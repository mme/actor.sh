#!/usr/bin/env python3
"""Tests for src/actor/config.py — KDL loader + roles."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actor.config import AgentDefaults, AppConfig, Role, load_config
from actor.errors import ConfigError


class TestLoadConfigEmpty(unittest.TestCase):

    def test_no_files_returns_only_built_in_roles(self):
        # The built-in `main` role is always present even with no kdl files.
        # Other slots (agent_defaults, hooks) stay empty.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIsInstance(cfg, AppConfig)
            self.assertEqual(set(cfg.roles), {"main"})
            self.assertEqual(cfg.agent_defaults, {})

    def test_built_in_main_role_has_agent_and_prompt(self):
        # `main` should be usable out of the box: an agent to spawn and a
        # system prompt to seed the actor with.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            main = cfg.roles["main"]
            self.assertEqual(main.name, "main")
            self.assertEqual(main.agent, "claude")
            self.assertTrue(main.prompt)
            self.assertIsNotNone(main.description)

    def test_user_kdl_can_override_built_in_main(self):
        # A `role "main" { ... }` in settings.kdl replaces the built-in
        # wholesale (whole-role replacement, not field-by-field merge).
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(home) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'role "main" {\n'
                '    agent "codex"\n'
                '    prompt "custom system prompt"\n'
                '}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            main = cfg.roles["main"]
            self.assertEqual(main.agent, "codex")
            self.assertEqual(main.prompt, "custom system prompt")
            # Description from the built-in is gone — user replaced the whole role.
            self.assertIsNone(main.description)

    def test_project_kdl_can_override_user_main(self):
        # Standard precedence: project beats user beats built-in.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            user_p = Path(home) / ".actor" / "settings.kdl"
            user_p.parent.mkdir()
            user_p.write_text('role "main" {\n    prompt "user main"\n}\n')
            proj_p = Path(cwd) / ".actor" / "settings.kdl"
            proj_p.parent.mkdir()
            proj_p.write_text('role "main" {\n    prompt "project main"\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.roles["main"].prompt, "project main")

    def test_missing_user_file_but_project_file_loads_project(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            proj = Path(cwd) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'role "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.roles)
            self.assertEqual(cfg.roles["qa"].agent, "claude")


class TestLoadConfigPrecedence(unittest.TestCase):

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_project_overrides_user_same_role_name(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'role "qa" {\n    agent "claude"\n    model "sonnet"\n}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'role "qa" {\n    agent "codex"\n    model "opus"\n}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.roles["qa"].agent, "codex")
            self.assertEqual(cfg.roles["qa"].config["model"], "opus")

    def test_user_and_project_both_contribute_distinct_roles(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'role "qa" {\n    agent "claude"\n}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'role "reviewer" {\n    agent "claude"\n}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.roles)
            self.assertIn("reviewer", cfg.roles)


class TestLoadConfigWalkUp(unittest.TestCase):

    def test_project_config_found_by_walking_up_from_cwd(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            proj = Path(root) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'role "qa" {\n    agent "claude"\n}\n'
            )
            deep = Path(root) / "src" / "nested" / "deeper"
            deep.mkdir(parents=True)
            cfg = load_config(cwd=deep, home=Path(home))
            self.assertIn("qa", cfg.roles)


class TestLoadConfigErrors(unittest.TestCase):

    def test_malformed_kdl_raises_config_error_with_path(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('role "qa" {\n    unclosed\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn(str(bad), str(ctx.exception))

    def test_role_without_name_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('role {\n    agent "claude"\n}\n')
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))

    def test_role_child_without_value_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bad = Path(cwd) / ".actor" / "settings.kdl"
            bad.parent.mkdir()
            bad.write_text('role "qa" {\n    agent\n}\n')
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))


class TestLoadConfigParseShapes(unittest.TestCase):

    def test_role_with_all_fields(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    model "opus"\n'
                '    effort "max"\n'
                '    prompt "You\'re a QA engineer."\n'
                '    description "Run tests; report failures concisely."\n'
                '}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            role = cfg.roles["qa"]
            self.assertEqual(role.agent, "claude")
            self.assertEqual(role.prompt, "You're a QA engineer.")
            self.assertEqual(role.description, "Run tests; report failures concisely.")
            self.assertEqual(role.config, {"model": "opus", "effort": "max"})

    def test_role_description_optional(self):
        # Roles without a description parse fine; the field is None.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('role "qa" {\n    agent "claude"\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIsNone(cfg.roles["qa"].description)

    def test_bool_value_coerced_to_string(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('role "x" {\n    use-subscription true\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.roles["x"].config["use-subscription"], "true")

    def test_int_value_coerced_without_trailing_zero(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('role "x" {\n    max-budget-usd 5\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.roles["x"].config["max-budget-usd"], "5")

    def test_unknown_top_level_nodes_are_ignored(self):
        # Forward-compat: hooks / alias are reserved for follow-up tickets
        # #30 / #33 and should parse as no-ops today rather than erroring.
        # (`agent` is now a first-class node — see TestLoadConfigAgentBlocks.)
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'hooks {\n    on-start "echo hi"\n}\n'
                'alias "max" role="qa"\n'
                'role "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("qa", cfg.roles)
            # Built-in `main` is always present alongside user-defined roles.
            self.assertEqual(set(cfg.roles), {"main", "qa"})


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

    def test_extra_args_on_role_node_raises(self):
        self._expect_error(
            'role "qa" "extra" {\n    agent "claude"\n}\n',
            "extra",
        )

    def test_extra_args_on_child_node_raises(self):
        self._expect_error(
            'role "qa" {\n    model "opus" "sonnet"\n}\n',
            "model",
        )

    def test_props_on_child_node_raises(self):
        self._expect_error(
            'role "qa" {\n    model name="opus"\n}\n',
            "model",
        )

    def test_duplicate_key_in_role_raises(self):
        self._expect_error(
            'role "qa" {\n    model "opus"\n    model "sonnet"\n}\n',
            "model",
        )

    def test_duplicate_role_name_in_file_raises(self):
        self._expect_error(
            'role "qa" {\n    agent "claude"\n}\n'
            'role "qa" {\n    agent "codex"\n}\n',
            "qa",
        )

    def test_non_string_agent_value_raises(self):
        self._expect_error(
            'role "qa" {\n    agent 42\n}\n',
            "agent",
        )

    def test_non_string_prompt_value_raises(self):
        self._expect_error(
            'role "qa" {\n    prompt 42\n}\n',
            "prompt",
        )

    def test_props_on_role_node_raises(self):
        self._expect_error(
            'role "qa" flag="x" {\n    agent "claude"\n}\n',
            "qa",
        )

    def test_empty_role_name_raises(self):
        self._expect_error(
            'role "" {\n    agent "claude"\n}\n',
            "non-empty",
        )

    def test_null_in_role_raises_friendly_error(self):
        # Null is only meaningful inside `agent { }` blocks (as a cancel
        # marker). Inside roles it would silently stringify to "None"
        # or crash with an ugly type error. Reject loudly.
        self._expect_error(
            'role "qa" {\n    model null\n}\n',
            "null",
        )

    def test_nested_block_inside_defaults_raises(self):
        # Defaults is a flat namespace. Any sub-block under it is a
        # parse error — keys must be leaf values.
        self._expect_error(
            'defaults "claude" {\n'
            '    permission-mode {\n'
            '        nested "value"\n'
            '    }\n'
            '}\n',
            "permission-mode",
        )


class TestLoadConfigCoercion(unittest.TestCase):

    def test_non_integer_float_keeps_decimal(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text('role "x" {\n    temperature 0.5\n}\n')
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            self.assertEqual(cfg.roles["x"].config["temperature"], "0.5")


class TestLoadConfigHomeUnset(unittest.TestCase):

    def test_home_none_skips_user_config_and_loads_project(self):
        with tempfile.TemporaryDirectory() as cwd:
            proj = Path(cwd) / ".actor"
            proj.mkdir()
            (proj / "settings.kdl").write_text(
                'role "qa" {\n    agent "claude"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=None)
            self.assertIn("qa", cfg.roles)


class TestLoadConfigCwdUnderHome(unittest.TestCase):
    """Walk-up must stop before re-parsing the user config as a 'project'."""

    def test_cwd_inside_home_does_not_double_load_user_config(self):
        # cwd sits inside home, and only the user config exists.
        # Without the fix, walk-up finds the user config as the "project"
        # path too, and duplicate role names would merge over
        # themselves — and worse, a strict duplicate-role check would
        # wrongly fire on the second parse.
        with tempfile.TemporaryDirectory() as home:
            home_p = Path(home)
            (home_p / ".actor").mkdir()
            (home_p / ".actor" / "settings.kdl").write_text(
                'role "qa" {\n    agent "claude"\n}\n'
            )
            sub = home_p / "work" / "repo"
            sub.mkdir(parents=True)
            cfg = load_config(cwd=sub, home=home_p)
            self.assertIn("qa", cfg.roles)
            self.assertEqual(cfg.roles["qa"].agent, "claude")

    def test_project_config_found_even_when_cwd_is_inside_home(self):
        # Project config at <home>/work/repo/.actor/settings.kdl is still
        # picked up when cwd is there (and wins on override).
        with tempfile.TemporaryDirectory() as home:
            home_p = Path(home)
            (home_p / ".actor").mkdir()
            (home_p / ".actor" / "settings.kdl").write_text(
                'role "qa" {\n    agent "claude"\n}\n'
            )
            proj = home_p / "work" / "repo"
            (proj / ".actor").mkdir(parents=True)
            (proj / ".actor" / "settings.kdl").write_text(
                'role "qa" {\n    agent "codex"\n}\n'
            )
            cfg = load_config(cwd=proj, home=home_p)
            self.assertEqual(cfg.roles["qa"].agent, "codex")


class TestLoadConfigAgentBlocks(unittest.TestCase):

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_agent_defaults_parsed(self):
        # Flat namespace — actor-keys (use-subscription) and agent-args
        # (permission-mode, model) live side by side; the parser routes
        # by checking each key against the agent's ACTOR_DEFAULTS
        # whitelist.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    use-subscription false\n'
                '    permission-mode "bypassPermissions"\n'
                '    model "opus"\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.actor_keys, {"use-subscription": "false"})
            self.assertEqual(
                d.agent_args,
                {"permission-mode": "bypassPermissions", "model": "opus"},
            )

    def test_null_in_defaults_survives_merge_as_cancel_marker(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    permission-mode null\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            # Null must survive all the way to cmd_new so it can cancel a
            # lower-precedence class default.
            self.assertIn("claude", cfg.agent_defaults)
            self.assertIsNone(
                cfg.agent_defaults["claude"].agent_args["permission-mode"]
            )

    def test_unknown_agent_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'defaults "bogus" {\n'
                '    x "1"\n'
                '}\n'
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("bogus", str(ctx.exception))

    def test_arbitrary_key_routes_to_agent_args(self):
        # Keys not in ACTOR_DEFAULTS land in agent_args verbatim — the
        # parser doesn't validate against any agent-flag schema, so a
        # typo like `permision-mode` would be forwarded to the agent
        # binary which then errors at spawn time. Documented limitation
        # of the stringly-typed pipeline.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    not-a-real-flat-key "x"\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.agent_args, {"not-a-real-flat-key": "x"})
            self.assertEqual(d.actor_keys, {})

    def test_project_agent_defaults_override_user_per_key(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    model "sonnet"\n'
                '    permission-mode "auto"\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    permission-mode null\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.agent_args.get("model"), "sonnet")
            # project layer null wins over user layer "auto" and is preserved
            # as a cancel marker for cmd_new's class-default merge.
            self.assertIn("permission-mode", d.agent_args)
            self.assertIsNone(d.agent_args["permission-mode"])

    def test_project_agent_defaults_actor_key_override(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    use-subscription true\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    use-subscription false\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["claude"]
            self.assertEqual(d.actor_keys.get("use-subscription"), "false")

    def test_defaults_block_at_role_level_rejected(self):
        # `defaults` is the top-level per-agent block name; nesting it
        # under `role` is rejected with a hint pointing at the
        # correct shape (flat keys directly under the role).
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'role "qa" {\n'
                '    defaults {\n'
                '        model "opus"\n'
                '    }\n'
                '}\n'
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("reserved", str(ctx.exception))

    def test_codex_agent_defaults_parsed(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "codex" {\n'
                '    m "o3"\n'
                '    sandbox "read-only"\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            d = cfg.agent_defaults["codex"]
            self.assertEqual(d.agent_args, {"m": "o3", "sandbox": "read-only"})

    def test_duplicate_defaults_block_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'defaults "claude" {\n'
                '    use-subscription true\n'
                '}\n'
                'defaults "claude" {\n'
                '    use-subscription false\n'
                '}\n'
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(cwd=Path(cwd), home=Path(home))
            self.assertIn("claude", str(ctx.exception))

    def test_defaults_block_without_name_rejected(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(
                'defaults {\n'
                '    model "opus"\n'
                '}\n'
            )
            with self.assertRaises(ConfigError):
                load_config(cwd=Path(cwd), home=Path(home))

    def test_null_flat_key_survives_merge(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            self._write(
                Path(home) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    use-subscription true\n'
                '}\n',
            )
            self._write(
                Path(cwd) / ".actor" / "settings.kdl",
                'defaults "claude" {\n'
                '    use-subscription null\n'
                '}\n',
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
            # Project layer null wins over user layer "true" and is preserved
            # so cmd_new can cancel the class ACTOR_DEFAULTS entry.
            self.assertIn("claude", cfg.agent_defaults)
            self.assertIsNone(
                cfg.agent_defaults["claude"].actor_keys["use-subscription"]
            )


if __name__ == "__main__":
    unittest.main()
