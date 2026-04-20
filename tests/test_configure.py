#!/usr/bin/env python3
"""Tests for src/actor/configure.py — built-in question defaults, resolver,
AskUserQuestion payload shape."""
from __future__ import annotations

import unittest

from actor.config import (
    AgentSettings,
    AppConfig,
    ConfigureBlock,
    Question,
    QuestionOption,
)
from actor.configure import (
    BUILTIN_QUESTIONS,
    ConfigureDisabledError,
    questions_to_payload,
    resolve_questions,
)
from actor.errors import ActorError


class TestBuiltInDefaults(unittest.TestCase):

    def test_claude_builtins_cover_expected_keys(self):
        keys = {q.key for q in BUILTIN_QUESTIONS["claude"]}
        self.assertIn("model", keys)
        self.assertIn("effort", keys)
        self.assertIn("permission-mode", keys)
        self.assertIn("prompt", keys)

    def test_codex_builtins_cover_expected_keys(self):
        keys = {q.key for q in BUILTIN_QUESTIONS["codex"]}
        self.assertIn("sandbox", keys)
        self.assertIn("prompt", keys)

    def test_all_builtin_options_have_two_or_more_labels(self):
        # AskUserQuestion requires >= 2 options per question.
        for agent, qs in BUILTIN_QUESTIONS.items():
            for q in qs:
                if q.kind == "options":
                    self.assertGreaterEqual(
                        len(q.options), 2,
                        f"{agent} {q.key}: only {len(q.options)} options",
                    )


class TestResolveQuestions(unittest.TestCase):

    def test_unknown_agent_raises(self):
        cfg = AppConfig()
        with self.assertRaises(ActorError):
            resolve_questions(cfg, agent="bogus", model=None)

    def test_no_overrides_returns_builtins(self):
        cfg = AppConfig()
        qs = resolve_questions(cfg, agent="claude", model=None)
        self.assertEqual(
            [q.key for q in qs], [q.key for q in BUILTIN_QUESTIONS["claude"]]
        )

    def test_agent_level_configure_replaces_builtins(self):
        agent = AgentSettings(
            name="claude",
            configure_blocks={
                None: ConfigureBlock(
                    model=None,
                    questions=[
                        Question(
                            key="focus",
                            prompt="Focus?",
                            header="Focus",
                            options=[QuestionOption("ui"), QuestionOption("api")],
                        )
                    ],
                )
            },
        )
        cfg = AppConfig(agents={"claude": agent})
        qs = resolve_questions(cfg, agent="claude", model=None)
        self.assertEqual([q.key for q in qs], ["focus"])

    def test_model_scoped_wins_over_agent_level(self):
        agent = AgentSettings(
            name="claude",
            configure_blocks={
                None: ConfigureBlock(
                    model=None,
                    questions=[
                        Question(
                            key="a",
                            prompt="?",
                            header="A",
                            options=[QuestionOption("x"), QuestionOption("y")],
                        )
                    ],
                ),
                "opus": ConfigureBlock(
                    model="opus",
                    questions=[
                        Question(
                            key="b",
                            prompt="?",
                            header="B",
                            options=[QuestionOption("x"), QuestionOption("y")],
                        )
                    ],
                ),
            },
        )
        cfg = AppConfig(agents={"claude": agent})
        qs_agent = resolve_questions(cfg, agent="claude", model=None)
        self.assertEqual([q.key for q in qs_agent], ["a"])
        qs_model = resolve_questions(cfg, agent="claude", model="opus")
        self.assertEqual([q.key for q in qs_model], ["b"])

    def test_model_scoped_falls_back_to_agent_level_when_no_model_block(self):
        agent = AgentSettings(
            name="claude",
            configure_blocks={
                None: ConfigureBlock(
                    model=None,
                    questions=[
                        Question(
                            key="a",
                            prompt="?",
                            header="A",
                            options=[QuestionOption("x"), QuestionOption("y")],
                        )
                    ],
                ),
            },
        )
        cfg = AppConfig(agents={"claude": agent})
        qs = resolve_questions(cfg, agent="claude", model="sonnet")
        self.assertEqual([q.key for q in qs], ["a"])

    def test_model_scoped_falls_back_to_builtins_when_no_agent_block(self):
        cfg = AppConfig(
            agents={
                "claude": AgentSettings(name="claude", configure_blocks={})
            }
        )
        qs = resolve_questions(cfg, agent="claude", model="opus")
        self.assertEqual(
            [q.key for q in qs], [q.key for q in BUILTIN_QUESTIONS["claude"]]
        )

    def test_disabled_raises(self):
        cfg = AppConfig(configure_default="off")
        with self.assertRaises(ConfigureDisabledError):
            resolve_questions(cfg, agent="claude", model=None)


class TestPayloadShape(unittest.TestCase):

    def test_options_question_renders_straight_through(self):
        q = Question(
            key="model",
            prompt="Which model?",
            header="Model",
            options=[QuestionOption("opus"), QuestionOption("sonnet")],
        )
        payload = questions_to_payload([q])
        self.assertEqual(len(payload["questions"]), 1)
        entry = payload["questions"][0]
        self.assertEqual(entry["key"], "model")
        self.assertEqual(entry["kind"], "options")
        self.assertEqual(entry["question"], "Which model?")
        self.assertEqual(entry["header"], "Model")
        self.assertEqual(entry["multiSelect"], False)
        self.assertEqual(
            [o["label"] for o in entry["options"]], ["opus", "sonnet"]
        )

    def test_text_question_synthesizes_skip_and_enter_options(self):
        q = Question(
            key="prompt",
            prompt="What's the goal?",
            header="Goal",
            options=[],
            kind="text",
            optional=True,
        )
        payload = questions_to_payload([q])
        entry = payload["questions"][0]
        self.assertEqual(entry["kind"], "text")
        labels = [o["label"] for o in entry["options"]]
        self.assertIn("Skip", labels)
        self.assertIn("Enter below", labels)

    def test_payload_header_is_trimmed_to_12_chars(self):
        q = Question(
            key="x",
            prompt="?",
            header="This header is too long",
            options=[QuestionOption("a"), QuestionOption("b")],
        )
        payload = questions_to_payload([q])
        self.assertLessEqual(len(payload["questions"][0]["header"]), 12)

    def test_optional_flag_surfaces_in_payload(self):
        q = Question(
            key="prompt",
            prompt="?",
            header="Prompt",
            options=[],
            kind="text",
            optional=True,
        )
        payload = questions_to_payload([q])
        self.assertTrue(payload["questions"][0]["optional"])


if __name__ == "__main__":
    unittest.main()
