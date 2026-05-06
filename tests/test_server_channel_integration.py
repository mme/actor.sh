"""Integration test for the channel-notification delivery path.

Drives the real MCP server over in-memory streams, invokes the
async run_actor tool, and asserts a notifications/claude/channel
event hits the wire after the background run completes.

This is the only test that exercises the full dispatch:
  tool invocation -> _spawn_background_run -> task on the loop ->
  service publishes Notification -> dispatch handler relays to wire

Guards against regressions such as:
  - Future refactors of `_spawn_background_run` dropping the
    notification subscription.
  - The wire format of channel events changing accidentally.

Phase 2 swap: `actor.server._service()` returns a `RemoteActorService`
in production, but the in-process pub/sub semantics are byte-identical
between Local and Remote. This test points `_service()` at a single
shared `LocalActorService` so the fake `run_actor` and the spawn's
`subscribe_notifications` see the same event bus — covering the
server-side relay logic without dragging a real `actord` subprocess
in. End-to-end wire coverage for subscription fan-out lives in
`tests/test_daemon_protocol.py`.
"""
from __future__ import annotations

import asyncio
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
from actor.service import LocalActorService, Notification
from actor.types import AgentKind, Status


class ChannelNotificationReproTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_actor_emits_channel_notification_on_completion(self):
        fake_actor = MagicMock()
        fake_actor.agent = AgentKind.CLAUDE

        run_done = asyncio.Event()

        async def fake_run_actor(self, name, prompt, config):
            """Stand in for the real `run_actor`.

            Publishes a `run_completed` notification through the
            service's pub/sub so the server's subscribed handler
            relays it to the channel — same wire path the production
            code drives, just without spawning a real agent."""
            try:
                await self.publish_notification(Notification(
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

        async def fake_get_actor(self, name):
            return fake_actor

        # One in-process service shared across `_spawn_background_run`'s
        # subscribe + run calls so the pub/sub bus they touch is the same.
        shared = LocalActorService(
            db=MagicMock(), git=MagicMock(), proc_mgr=MagicMock(),
        )

        with patch("actor.server._service", return_value=shared), \
             patch("actor.service.LocalActorService.run_actor", fake_run_actor), \
             patch("actor.service.LocalActorService.get_actor", fake_get_actor):
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
                await asyncio.wait_for(run_done.wait(), timeout=5.0)
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

        # Wire format invariant — content prefix and meta keys must
        # stay byte-identical to the pre-async dispatcher so existing
        # parent claude sessions parse it the same way.
        params = channel_notifs[0].message.root.params
        self.assertEqual(params["content"], "[x] finished")
        self.assertEqual(params["meta"], {"actor": "x", "status": "done"})


if __name__ == "__main__":
    unittest.main()
