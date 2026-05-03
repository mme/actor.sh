"""e2e: --dir path normalization and edge cases."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from e2e.harness.isolated_home import isolated_home


def _git_init(path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "e", "GIT_AUTHOR_EMAIL": "e@x",
           "GIT_COMMITTER_NAME": "e", "GIT_COMMITTER_EMAIL": "e@x"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True,
                   env=env, capture_output=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=path, check=True,
                   env=env, capture_output=True)


class DirPathHandlingTests(unittest.TestCase):

    def test_dir_with_trailing_slash(self):
        with isolated_home() as env:
            other = env.cwd.parent / "other"
            other.mkdir()
            _git_init(other)
            r = env.run_cli(["new", "alice", "--dir", str(other) + "/"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_dir_relative_path(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--dir", "."])
            # cwd is a git repo; this should work like the default.
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_dir_nonexistent_path_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--dir", "/path/does/not/exist"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_dir_file_not_dir_errors(self):
        with isolated_home() as env:
            target = env.cwd.parent / "afile.txt"
            target.write_text("not a dir")
            r = env.run_cli(["new", "alice", "--dir", str(target)])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
