"""Sanity-check that `LocalActorService` can be constructed and
exercised without going through CLI argv parsing or the MCP server.

Runs the smallest possible end-to-end flow using the in-memory
fakes from test_actor.py, proving the boundary is wired cleanly so a
future RemoteActorService swap-in only needs to satisfy the same
ABC.
"""
from __future__ import annotations

import unittest

from actor import (
    ActorConfig,
    Database,
    LocalActorService,
    Notification,
    RunResult,
    Status,
)
from tests.test_actor import FakeAgent, FakeGit, FakeProcessManager


class ConstructAndInvokeTests(unittest.TestCase):

    def _service(self, agent=None):
        return LocalActorService(
            db=Database.open(":memory:"),
            git=FakeGit(),
            proc_mgr=FakeProcessManager(),
            agent_factory=lambda _k: agent if agent is not None else FakeAgent(),
        )

    def test_service_constructs_and_creates_actor(self):
        svc = self._service()
        actor = svc.new_actor(
            name="alpha",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config=ActorConfig(),
        )
        self.assertEqual(actor.name, "alpha")
        # Round-trip through discovery.
        self.assertEqual(svc.get_actor("alpha").name, "alpha")
        self.assertEqual([a.name for a in svc.list_actors()], ["alpha"])

    def test_run_actor_publishes_run_completed_notification(self):
        agent = FakeAgent()
        svc = self._service(agent=agent)
        svc.new_actor(
            name="beta", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", config=ActorConfig(),
        )

        seen: list[Notification] = []
        cancel = svc.subscribe_notifications(seen.append)
        try:
            result = svc.run_actor("beta", prompt="go", config=ActorConfig())
        finally:
            cancel()

        self.assertIsInstance(result, RunResult)
        self.assertEqual(result.status, Status.DONE)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].event, "run_completed")
        self.assertEqual(seen[0].actor, "beta")
        self.assertEqual(seen[0].status, Status.DONE)

    def test_subscribe_notifications_returns_cancel(self):
        svc = self._service()
        events: list[Notification] = []
        cancel = svc.subscribe_notifications(events.append)
        # Cancel before publishing — handler must not fire.
        cancel()
        svc.publish_notification(Notification(actor="x", event="run_completed"))
        self.assertEqual(events, [])

    def test_misbehaving_handler_does_not_break_others(self):
        svc = self._service()
        ok_calls: list[Notification] = []

        def bad(_n):
            raise RuntimeError("boom")

        svc.subscribe_notifications(bad)
        svc.subscribe_notifications(ok_calls.append)
        svc.publish_notification(Notification(actor="x", event="run_completed"))
        self.assertEqual(len(ok_calls), 1)


if __name__ == "__main__":
    unittest.main()
