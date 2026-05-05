"""Integration test for the channel-notification delivery path.

Drives the real MCP server over in-memory streams, invokes the sync
run_actor tool, and asserts a notifications/claude/channel event hits the
wire after the background run completes.

This is the only test that exercises the full dispatch:
  tool invocation -> _spawn_background_run -> loop capture ->
  background thread -> run_coroutine_threadsafe -> wire

Guards against regressions such as:
  - FastMCP moving sync tools onto a worker thread (loop capture would
    break — this was the suspected cause in issue #6, which turned out
    to be a false alarm; see PR that added this test).
  - Future refactors of _spawn_background_run that drop the loop handoff.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from actor import server as srv
from actor.service import Notification
from actor.types import AgentKind, Status


class ChannelNotificationReproTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_actor_emits_channel_notification_on_completion(self):
        fake_actor = MagicMock()
        fake_actor.agent = AgentKind.CLAUDE

        fake_db = MagicMock()
        fake_db.get_actor.return_value = fake_actor
        resolved = MagicMock()
        resolved.value = "done"
        fake_db.resolve_actor_status.return_value = resolved

        run_done = threading.Event()

        def fake_run_actor(self, name, prompt, config):
            """Stand in for the real `LocalActorService.run_actor`.

            Publishes a `run_completed` notification through the
            service's pub/sub so the server's subscribed handler
            relays it to the channel — same wire path the production
            code drives, just without spawning a real agent."""
            try:
                self.publish_notification(Notification(
                    actor=name,
                    event="run_completed",
                    run_id=1,
                    status=Status.DONE,
                    output="finished",
                ))
                from actor import RunResult
                return RunResult(
                    run_id=1, actor=name,
                    status=Status.DONE, exit_code=0, output="finished",
                )
            finally:
                run_done.set()

        fake_db_cls = MagicMock()
        fake_db_cls.open.return_value = fake_db

        with patch("actor.server._db", return_value=fake_db), \
             patch("actor.server.Database", fake_db_cls), \
             patch("actor.server._create_agent", return_value=MagicMock()), \
             patch("actor.service.LocalActorService.run_actor", fake_run_actor), \
             patch("actor.server.RealProcessManager"):
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                client_read, client_write = client_streams
                server_read, server_write = server_streams

                server = srv.mcp._mcp_server
                init_options = server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}},
                )
                server_task = asyncio.create_task(
                    server.run(server_read, server_write, init_options)
                )

                received: list[SessionMessage] = []

                async def drain():
                    async for msg in client_read:
                        received.append(msg)

                drain_task = asyncio.create_task(drain())

                async def send(payload) -> None:
                    await client_write.send(SessionMessage(message=JSONRPCMessage(payload)))

                await send(JSONRPCRequest(
                    jsonrpc="2.0", id=1, method="initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "repro-test", "version": "0"},
                    },
                ))

                async def wait_for_response(req_id: int, timeout: float = 2.0) -> None:
                    deadline = asyncio.get_event_loop().time() + timeout
                    while asyncio.get_event_loop().time() < deadline:
                        for m in received:
                            root = m.message.root
                            if isinstance(root, JSONRPCResponse) and root.id == req_id:
                                return
                        await asyncio.sleep(0.02)
                    raise AssertionError(f"no response to id={req_id} within {timeout}s")

                await wait_for_response(1)

                await send(JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/initialized",
                    params={},
                ))

                await send(JSONRPCRequest(
                    jsonrpc="2.0", id=2, method="tools/call",
                    params={
                        "name": "run_actor",
                        "arguments": {"name": "x", "prompt": "go"},
                    },
                ))

                await wait_for_response(2)
                await asyncio.to_thread(run_done.wait, 5.0)
                # Give the server a moment to flush any trailing notification.
                await asyncio.sleep(0.5)

                server_task.cancel()
                drain_task.cancel()
                for t in (server_task, drain_task):
                    try:
                        await t
                    except BaseException:
                        pass

        channel_notifs = [
            m for m in received
            if isinstance(m.message.root, JSONRPCNotification)
            and m.message.root.method == "notifications/claude/channel"
        ]
        wire_methods = [
            getattr(m.message.root, "method", type(m.message.root).__name__)
            for m in received
        ]
        self.assertEqual(
            len(channel_notifs), 1,
            f"expected exactly one notifications/claude/channel event on the wire "
            f"after run_actor completion; saw {len(channel_notifs)}. "
            f"Messages received: {wire_methods}",
        )


if __name__ == "__main__":
    unittest.main()
