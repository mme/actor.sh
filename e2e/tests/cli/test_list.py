"""e2e: `actor list` — table output and filters."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorListTests(unittest.TestCase):

    def test_list_empty_db_header_only(self):
        with isolated_home() as env:
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn("NAME", lines[0])

    def test_list_shows_multiple_actors(self):
        with isolated_home() as env:
            for n in ("alice", "bob", "carol"):
                env.run_cli(["new", n])
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            for n in ("alice", "bob", "carol"):
                self.assertIn(n, r.stdout)

    def test_list_stale_pid_reclassified_as_error(self):
        # Insert a Run row with a fake PID that doesn't exist; the
        # next `list` should detect the dead PID and flip to ERROR.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            from actor.types import Run, Status
            with env.db() as db:
                db.insert_run(Run(
                    id=0, actor_name="alice", prompt="doomed",
                    status=Status.RUNNING, pid=999999,
                    started_at="2026-01-01T00:00:00Z",
                    finished_at=None, exit_code=None,
                ))
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("error", r.stdout.lower())
            self.assertNotIn("running", r.stdout.lower())

    def test_list_mixed_statuses_render_correctly(self):
        with isolated_home() as env:
            env.run_cli(["new", "done-actor", "task"], **claude_responds("ok"))
            env.run_cli(["new", "idle-actor"])
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("done-actor", r.stdout)
            self.assertIn("idle-actor", r.stdout)
            self.assertIn("done", r.stdout)
            self.assertIn("idle", r.stdout)

    def test_list_status_filter(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            env.run_cli(["new", "bob"])  # idle
            r = env.run_cli(["list", "--status", "done"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)
            self.assertNotIn("bob", r.stdout)


if __name__ == "__main__":
    unittest.main()
