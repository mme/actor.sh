"""Integration tests for PtySession against real processes.

Uses /bin/cat and /bin/sh as the target commands so we exercise the full
fork + exec + read + write + resize + reap pipeline end-to-end.

These tests are slower (real subprocess + real fd) but still under a
second each. Skipped on non-POSIX systems.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pathlib
import shutil
import struct
import sys
import termios
import unittest

if sys.platform == "win32":
    raise unittest.SkipTest("PTY not available on Windows")

from actor.watch.interactive.pty_session import PtySession


def _find_binary(name: str) -> str:
    for base in ("/bin", "/usr/bin"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    path = shutil.which(name)
    if path is None:
        raise unittest.SkipTest(f"{name} not found on PATH")
    return path


class PtySessionTests(unittest.IsolatedAsyncioTestCase):
    async def _drain_until(self, session: PtySession, received: list[bytes], target: bytes, timeout: float = 3.0) -> None:
        async def waiter():
            while True:
                joined = b"".join(received)
                if target in joined:
                    return
                await asyncio.sleep(0.01)
        await asyncio.wait_for(waiter(), timeout=timeout)

    async def test_cat_echoes_input(self):
        cat = _find_binary("cat")
        received: list[bytes] = []
        exit_codes: list[int] = []
        session = PtySession(
            argv=[cat],
            cwd=pathlib.Path("/tmp"),
            rows=24, cols=80,
            on_output=lambda data: received.append(data),
            on_exit=lambda code: exit_codes.append(code),
        )
        session.spawn()
        try:
            session.write(b"hello world\n")
            await self._drain_until(session, received, b"hello world")
        finally:
            session.close()

        # Close is synchronous — the exit callback fired.
        self.assertEqual(len(exit_codes), 1)
        self.assertIsNotNone(session.exit_code)
        self.assertTrue(session.exited)

    async def test_child_exit_triggers_on_exit_callback(self):
        sh = _find_binary("sh")
        exit_codes: list[int] = []
        session = PtySession(
            argv=[sh, "-c", "exit 7"],
            cwd=pathlib.Path("/tmp"),
            on_exit=lambda code: exit_codes.append(code),
        )
        session.spawn()
        # Wait up to 3s for the child to exit and the reader to notice.
        for _ in range(300):
            if exit_codes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(exit_codes, [7])
        self.assertEqual(session.exit_code, 7)

    async def test_resize_changes_winsize(self):
        sh = _find_binary("sh")
        # Use `read` so the shell blocks between the two stty size calls —
        # no race between our resize and the first print.
        received: list[bytes] = []
        session = PtySession(
            argv=[sh, "-c", "stty size && read _ && stty size"],
            cwd=pathlib.Path("/tmp"),
            rows=24, cols=80,
            on_output=lambda data: received.append(data),
        )
        session.spawn()
        try:
            # Wait until the first stty prints (one newline).
            for _ in range(200):
                if b"\n" in b"".join(received):
                    break
                await asyncio.sleep(0.01)
            session.resize(rows=40, cols=120)
            # Unblock the shell so the second stty runs.
            session.write(b"x\n")
            for _ in range(200):
                if b"".join(received).count(b"\n") >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            session.close()
        joined = b"".join(received).decode("ascii", errors="replace")
        lines = [ln.strip() for ln in joined.splitlines() if ln.strip()]
        self.assertEqual(
            lines[0], "24 80",
            f"first stty should see initial winsize; got {lines!r}",
        )
        self.assertIn(
            "40 120", lines,
            f"post-resize stty should see new winsize; got {lines!r}",
        )

    async def test_close_kills_running_child(self):
        import signal
        sh = _find_binary("sh")
        # Shell must NOT trap SIGTERM — we want to verify the signal landed.
        # Some systems' /bin/sh is dash which already passes signals to its
        # own process group; spawning `sleep` directly keeps the chain simple.
        sleep_bin = _find_binary("sleep")
        exit_codes: list[int] = []
        session = PtySession(
            argv=[sleep_bin, "100"],
            cwd=pathlib.Path("/tmp"),
            on_exit=lambda code: exit_codes.append(code),
        )
        session.spawn()
        start = asyncio.get_event_loop().time()
        session.close()  # SIGTERM
        elapsed = asyncio.get_event_loop().time() - start
        self.assertLess(elapsed, 2.0, "close() should not block for seconds")
        self.assertEqual(
            session.exit_code, -signal.SIGTERM,
            f"expected -{signal.SIGTERM} (SIGTERM), got {session.exit_code}",
        )
        self.assertEqual(exit_codes, [-signal.SIGTERM])

    async def test_write_after_exit_is_noop(self):
        sh = _find_binary("sh")
        session = PtySession(
            argv=[sh, "-c", "exit 0"],
            cwd=pathlib.Path("/tmp"),
        )
        session.spawn()
        for _ in range(100):
            if session.exited:
                break
            await asyncio.sleep(0.01)
        # Should not raise.
        session.write(b"hello\n")

    async def test_cwd_is_applied(self):
        sh = _find_binary("sh")
        received: list[bytes] = []
        session = PtySession(
            argv=[sh, "-c", "pwd"],
            cwd=pathlib.Path("/tmp"),
            on_output=lambda data: received.append(data),
        )
        session.spawn()
        for _ in range(200):
            if b"\n" in b"".join(received):
                break
            await asyncio.sleep(0.01)
        session.close()
        joined = b"".join(received).decode("ascii", errors="replace")
        # macOS resolves /tmp → /private/tmp inside the child.
        self.assertTrue(
            "/tmp" in joined or "/private/tmp" in joined,
            f"pwd output was: {joined!r}",
        )

    async def test_env_is_passed(self):
        sh = _find_binary("sh")
        received: list[bytes] = []
        session = PtySession(
            argv=[sh, "-c", "echo $MY_MARKER"],
            cwd=pathlib.Path("/tmp"),
            env={"MY_MARKER": "hello-pty-env", "PATH": os.environ.get("PATH", "")},
            on_output=lambda data: received.append(data),
        )
        session.spawn()
        for _ in range(200):
            if b"hello-pty-env" in b"".join(received):
                break
            await asyncio.sleep(0.01)
        session.close()
        self.assertIn(b"hello-pty-env", b"".join(received))


if __name__ == "__main__":
    unittest.main()
