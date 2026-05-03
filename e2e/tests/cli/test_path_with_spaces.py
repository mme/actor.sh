"""e2e: paths with spaces."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from e2e.harness.isolated_home import isolated_home


class PathWithSpacesTests(unittest.TestCase):

    def test_dir_with_space_in_path(self):
        with isolated_home() as env:
            other = env.cwd.parent / "with spaces"
            other.mkdir()
            git_env = {**os.environ,
                       "GIT_AUTHOR_NAME": "e", "GIT_AUTHOR_EMAIL": "e@x",
                       "GIT_COMMITTER_NAME": "e", "GIT_COMMITTER_EMAIL": "e@x"}
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other,
                           check=True, env=git_env, capture_output=True)
            (other / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=other, check=True,
                           env=git_env, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=other,
                           check=True, env=git_env, capture_output=True)
            r = env.run_cli(["new", "alice", "--dir", str(other)])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
