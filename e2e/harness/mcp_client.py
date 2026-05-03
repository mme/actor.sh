"""Minimal stdio MCP client for the e2e suite.

Spawns `actor mcp` as a subprocess in an isolated env, performs the
JSON-RPC handshake, exposes list_tools / call_tool / notification
polling. Designed to be small and self-contained — uses just stdlib
JSON-RPC over stdio rather than pulling in a full MCP client.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional


class McpClient:
    """One client = one MCP server subprocess. Use as a context
    manager so the subprocess is cleaned up:

        with McpClient(env=env, cwd=cwd) as client:
            client.initialize()
            tools = client.list_tools()
            result = client.call_tool("list_actors", {})
            note = client.recv_notification(timeout=10)
    """

    def __init__(
        self,
        *,
        env: dict[str, str],
        cwd: Path,
        actor_bin: str = "actor",
    ) -> None:
        self._env = env
        self._cwd = cwd
        self._actor_bin = actor_bin
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._reader_thread: Optional[threading.Thread] = None
        self._inbox: "Queue[dict]" = Queue()
        self._stopped = False

    # ------- lifecycle -------

    def __enter__(self) -> "McpClient":
        self._proc = subprocess.Popen(
            [self._actor_bin, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._cwd),
            env=self._env,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._reader_thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stopped = True
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass

    # ------- low-level send/recv -------

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._inbox.put(msg)
            if self._stopped:
                break

    def _send(self, payload: dict) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def _request(self, method: str, params: Optional[dict] = None,
                 timeout: float = 10.0) -> dict:
        rid = self._next_id
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        })
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._inbox.get(timeout=0.1)
            except Empty:
                continue
            if msg.get("id") == rid:
                return msg
            # Notification or unrelated response — push back? for
            # simplicity, route via notification queue.
            if "method" in msg and "id" not in msg:
                self._notifications.put(msg)
        raise TimeoutError(f"MCP request {method} timed out after {timeout}s")

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    # ------- public API -------

    _notifications: "Queue[dict]" = Queue()

    def initialize(self) -> dict:
        resp = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "actor-e2e", "version": "0"},
        })
        self._notify("notifications/initialized", {})
        return resp.get("result", {})

    def list_tools(self) -> list[dict]:
        resp = self._request("tools/list", {})
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: Optional[dict] = None,
                  timeout: float = 30.0) -> dict:
        return self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, timeout=timeout).get("result", {})

    def recv_notification(self, timeout: float = 10.0) -> dict:
        """Pop the next notifications/* message. Raises TimeoutError
        if none arrives within timeout. Returns the full JSON-RPC
        notification dict (with `method` and `params`)."""
        try:
            return self._notifications.get(timeout=timeout)
        except Empty:
            raise TimeoutError(
                f"no MCP notification arrived within {timeout}s"
            )

    def stderr(self) -> str:
        """Return whatever the MCP subprocess wrote to stderr so far.
        Non-blocking; for failure-mode debugging only."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            return proc.stderr.read() or ""
        except Exception:
            return ""
