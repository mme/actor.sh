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


class PtySessionShutdownKillTests(unittest.IsolatedAsyncioTestCase):
    """shutdown_kill is the non-blocking variant for Textual's on_unmount."""

    async def test_shutdown_kill_returns_fast_for_running_child(self):
        import time, signal
        sleep_bin = _find_binary("sleep")
        session = PtySession(
            argv=[sleep_bin, "100"],
            cwd=pathlib.Path("/tmp"),
        )
        session.spawn()
        start = time.monotonic()
        session.shutdown_kill()
        elapsed = time.monotonic() - start
        # Budget is generous to survive loaded CI runners; the real
        # threshold that matters is "not seconds," which close() would
        # hit via its 500ms deadline + blocking waitpid.
        self.assertLess(elapsed, 0.4,
                        f"shutdown_kill must not block; took {elapsed:.3f}s")
        # exit_code may be None (zombie) or -SIGKILL (reaped in time).
        # Either way, _exit_fired is set so the session is done.
        self.assertTrue(session._exit_fired)

    async def test_shutdown_kill_idempotent(self):
        sleep_bin = _find_binary("sleep")
        session = PtySession(
            argv=[sleep_bin, "100"],
            cwd=pathlib.Path("/tmp"),
        )
        session.spawn()
        session.shutdown_kill()
        # Second call is a no-op.
        session.shutdown_kill()
        self.assertTrue(session._exit_fired)


class PtySessionFailurePathsTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_failure_yields_exit_127(self):
        """A bogus binary should surface via on_exit with code 127 rather
        than hanging or raising in the parent."""
        exit_codes: list[int] = []
        session = PtySession(
            argv=["/nonexistent/binary-xyz-nope"],
            cwd=pathlib.Path("/tmp"),
            on_exit=lambda code: exit_codes.append(code),
        )
        session.spawn()
        for _ in range(200):
            if exit_codes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(exit_codes, [127])

    async def test_write_handles_eagain(self):
        """os.write raising EAGAIN must queue the bytes rather than crash;
        the drainer flushes them once the fd is writable."""
        from unittest.mock import patch
        import errno as _errno

        cat = _find_binary("cat")
        session = PtySession(
            argv=[cat],
            cwd=pathlib.Path("/tmp"),
        )
        session.spawn()
        try:
            # Make the first write raise EAGAIN. Later writes (via drainer)
            # fall through to the real os.write so the session stays alive.
            real_write = os.write
            call = {"n": 0}

            def fake_write(fd, data):
                call["n"] += 1
                if call["n"] == 1:
                    raise OSError(_errno.EAGAIN, "fake")
                return real_write(fd, data)

            with patch("actor.watch.interactive.pty_session.os.write",
                       side_effect=fake_write):
                session.write(b"hello\n")
                # The initial write got EAGAIN → queued.
                self.assertEqual(
                    bytes(session._write_queue), b"hello\n",
                    "EAGAIN should have queued the bytes",
                )
                # Let the add_writer drainer fire.
                for _ in range(50):
                    if not session._write_queue:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(session._write_queue, bytearray())
        finally:
            session.close()


class TestQueryReplies(unittest.TestCase):
    """`_build_query_replies` auto-answers terminal-capability queries
    that codex (and other modern TUIs) emit on startup. Without these
    replies, the child stalls waiting for a real terminal and never
    paints its full UI."""

    @staticmethod
    def _build(data: bytes) -> bytes:
        from actor.watch.interactive.pty_session import _build_query_replies
        return _build_query_replies(data)

    def test_dsr_cursor_position_reply(self):
        """CSI 6 n → CSI 1;1 R."""
        self.assertEqual(self._build(b"\x1b[6n"), b"\x1b[1;1R")

    def test_dsr_device_status_reply(self):
        """CSI 5 n → CSI 0 n (terminal OK)."""
        self.assertEqual(self._build(b"\x1b[5n"), b"\x1b[0n")

    def test_da1_primary_attributes_reply(self):
        """CSI c → DA1 advertising xterm-256color modules."""
        self.assertEqual(
            self._build(b"\x1b[c"),
            b"\x1b[?64;1;2;6;9;15;22c",
        )
        # CSI 0 c is the same query with an explicit zero parameter.
        self.assertEqual(
            self._build(b"\x1b[0c"),
            b"\x1b[?64;1;2;6;9;15;22c",
        )

    def test_kitty_keyboard_query_reply(self):
        """CSI ? u → CSI ? 0 u (no flags)."""
        self.assertEqual(self._build(b"\x1b[?u"), b"\x1b[?0u")

    def test_osc_color_queries(self):
        """OSC 10/11 ?ST get rgb: replies. Both ST forms (ESC \\ and
        BEL) terminate the query; we accept either."""
        out = self._build(b"\x1b]10;?\x1b\\")
        self.assertIn(b"\x1b]10;rgb:", out)
        out = self._build(b"\x1b]11;?\x07")
        self.assertIn(b"\x1b]11;rgb:", out)

    def test_codex_startup_burst_produces_combined_reply(self):
        """The ~47-byte capture from codex's startup contains DSR +
        DA1 + OSC 10 + OSC 11 + kitty-keyboard, in one chunk. The
        reply buffer must contain answers for all of them."""
        burst = (
            b"\x1b[6n"             # DSR cursor pos
            b"\x1b[?u"             # kitty keyboard
            b"\x1b[c"              # DA1
            b"\x1b]10;?\x1b\\"     # OSC 10 fg
            b"\x1b]11;?\x1b\\"     # OSC 11 bg
        )
        out = self._build(burst)
        self.assertIn(b"\x1b[1;1R", out)
        self.assertIn(b"\x1b[?0u", out)
        self.assertIn(b"\x1b[?64;1;2;6;9;15;22c", out)
        self.assertIn(b"\x1b]10;rgb:", out)
        self.assertIn(b"\x1b]11;rgb:", out)

    def test_no_queries_no_replies(self):
        """A chunk of plain text or unrelated escape codes must
        produce zero reply bytes — fast path."""
        self.assertEqual(self._build(b""), b"")
        self.assertEqual(self._build(b"hello world"), b"")
        # A 256-palette SGR shouldn't be confused with a query.
        self.assertEqual(self._build(b"\x1b[38;5;42m"), b"")


if __name__ == "__main__":
    unittest.main()
