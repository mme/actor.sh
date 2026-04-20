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


if __name__ == "__main__":
    unittest.main()
