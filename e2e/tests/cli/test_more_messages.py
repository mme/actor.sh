"""e2e: more error message and output content checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreMessageTests(unittest.TestCase):

    def test_run_on_running_error_includes_name(self):
        import subprocess, time
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            p = subprocess.Popen(
                ["actor", "run", "alice", "long"],
                env=env.env(**claude_responds("ok", sleep=5)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                r = env.run_cli(["run", "alice", "second"])
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("alice", r.stderr + r.stdout)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=3)

    def test_role_codex_with_prompt_error_mentions_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "codex"\n'
                '    prompt "x"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            err = r.stderr + r.stdout
            # Error should reference 'prompt' or 'system prompt'.
            self.assertTrue(
                "prompt" in err.lower(),
                f"error should mention prompt: {err}",
            )

    def test_dir_nonexistent_error_includes_path(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--dir", "/very/specific/missing/path"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("/very/specific/missing/path", r.stderr + r.stdout)

    def test_no_repo_error_mentions_git(self):
        # `actor new` in a non-git dir without --no-worktree.
        with isolated_home(init_git=False) as env:
            r = env.run_cli(["new", "alice"])
            err = r.stderr + r.stdout
            if r.returncode != 0:
                self.assertIn("git", err.lower(),
                              f"error should mention git: {err}")


if __name__ == "__main__":
    unittest.main()
