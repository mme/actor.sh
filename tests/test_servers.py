"""Phase 4 discovery tests (issue #35 stage 7+8).

Coverage:
- Ed25519 cert generation: first start mints, second start reuses,
  fingerprint stable, key file mode 0600.
- DiscoveryService publishing: a record appears under
  `_actor._tcp.local.` with the expected TXT fields.
- DiscoveryService browsing: two services in one process see each
  other.
- `ListServers` RPC: round-trips through gRPC against a real daemon.
- `actor servers` CLI: end-to-end against a running daemon.

The cert + discovery tests run in-process — fast, no subprocess.
The RPC / CLI tests spin up a real `actor daemon start` against a
tempdir HOME (matching the Phase 3 pattern) so the test exercises the
production startup path including identity load and zeroconf publish.

Daemon hygiene: every subprocess test cleans up at teardown.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from actor.bootstrap import is_pid_alive, read_daemon_pid
from actor.discovery import (
    ADVERTISED_PORT,
    SERVICE_TYPE,
    DiscoveryService,
)
from actor.identity import (
    Identity,
    cert_fingerprint,
    identity_paths,
    load_or_create_identity,
)
from actor.service import RemoteActorService


def _stop_daemon_in(home: Path) -> None:
    """Best-effort SIGTERM → SIGKILL for any daemon still running for
    this test's HOME. Mirrors the Phase 3 helper."""
    pid = read_daemon_pid(home / ".actor" / "daemon.pid")
    if pid is not None and is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not is_pid_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _start_daemon(home: Path, *, log_file: Path | None = None) -> subprocess.Popen:
    """Spawn `actor daemon start --foreground` against `home` and
    block until the socket accepts."""
    sock = home / ".actor" / "daemon.sock"
    env = {**os.environ, "HOME": str(home)}
    env.pop("ACTOR_NAME", None)
    args = [
        "actor", "daemon", "start", "--foreground",
        "--listen", f"unix:{sock}",
    ]
    if log_file is not None:
        args += ["--log-file", str(log_file)]
    proc = subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.2)
                try:
                    s.connect(str(sock))
                    return proc
                finally:
                    s.close()
            except OSError:
                pass
        time.sleep(0.05)
    proc.terminate()
    raise RuntimeError(f"daemon for {home} did not bind {sock}")


# ---------------------------------------------------------------------------
# Identity (cert generation)
# ---------------------------------------------------------------------------


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p4-ident-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_first_start_mints_files_with_0600_key(self) -> None:
        ident = load_or_create_identity(home=self._tmp)
        key_path, cert_path = identity_paths(home=self._tmp)
        self.assertTrue(key_path.exists())
        self.assertTrue(cert_path.exists())
        # Key must be 0600; cert mode is unspecified (it's public).
        mode = key_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, msg=f"key mode is {oct(mode)}")
        # Fingerprint shape.
        self.assertTrue(ident.fingerprint.startswith("sha256:"))
        self.assertEqual(len(ident.fingerprint), len("sha256:") + 64)

    def test_second_start_reuses_files_and_fingerprint_is_stable(self) -> None:
        a = load_or_create_identity(home=self._tmp)
        # Stat the key file's mtime — the second call must NOT rewrite it.
        key_path, cert_path = identity_paths(home=self._tmp)
        key_mtime_before = key_path.stat().st_mtime_ns
        cert_mtime_before = cert_path.stat().st_mtime_ns

        b = load_or_create_identity(home=self._tmp)
        self.assertEqual(a.fingerprint, b.fingerprint)
        # File identity preserved.
        self.assertEqual(key_path.stat().st_mtime_ns, key_mtime_before)
        self.assertEqual(cert_path.stat().st_mtime_ns, cert_mtime_before)

    def test_cert_fingerprint_function_is_deterministic(self) -> None:
        ident = load_or_create_identity(home=self._tmp)
        # Re-derive the fingerprint from disk and compare.
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(ident.cert_path.read_bytes())
        self.assertEqual(cert_fingerprint(cert), ident.fingerprint)

    def test_short_fingerprint_is_eight_hex_chars(self) -> None:
        ident = load_or_create_identity(home=self._tmp)
        prefix, _, rest = ident.short_fingerprint.partition(":")
        self.assertEqual(prefix, "sha256")
        self.assertEqual(len(rest), 8)


# ---------------------------------------------------------------------------
# Discovery: publishing + browsing in one process
# ---------------------------------------------------------------------------


class DiscoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    """Two `DiscoveryService` instances in the same process must see
    each other via the local zeroconf stack. Runs against the real
    network (loopback + cni0 / wlan0 / etc.) — same as production.

    These tests can be flaky on machines without working multicast;
    we wait up to 8 seconds for the records to propagate but don't
    assert hard timing.
    """

    async def _wait_for_peer(
        self,
        service: DiscoveryService,
        peer_name: str,
        *,
        timeout: float = 8.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in service.snapshot():
                if r.is_self:
                    continue
                if r.instance_name == peer_name:
                    return
            await asyncio.sleep(0.1)
        raise AssertionError(
            f"{peer_name!r} not seen by {service.instance_name!r} within {timeout}s"
        )

    async def test_self_record_always_present(self) -> None:
        svc = DiscoveryService(
            instance_name="lone-actord",
            fingerprint="sha256:" + "0" * 64,
            version="0.0.0-test",
            pid=os.getpid(),
        )
        try:
            await svc.start()
            snap = svc.snapshot()
            self.assertGreaterEqual(len(snap), 1)
            self.assertTrue(snap[0].is_self)
            self.assertEqual(snap[0].instance_name, svc.instance_name)
            self.assertEqual(snap[0].port, ADVERTISED_PORT)
        finally:
            await svc.stop()

    async def test_two_services_discover_each_other(self) -> None:
        a = DiscoveryService(
            instance_name="phase4-a-" + _short_id(),
            fingerprint="sha256:" + "a" * 64,
            version="0.0.0-test",
            pid=os.getpid(),
        )
        b = DiscoveryService(
            instance_name="phase4-b-" + _short_id(),
            fingerprint="sha256:" + "b" * 64,
            version="0.0.0-test",
            pid=os.getpid(),
        )
        try:
            await a.start()
            await b.start()
            await self._wait_for_peer(a, b.instance_name)
            await self._wait_for_peer(b, a.instance_name)

            # Each side reports the other with the right TXT contents.
            a_view = {r.instance_name: r for r in a.snapshot()}
            self.assertIn(b.instance_name, a_view)
            self.assertEqual(a_view[b.instance_name].fingerprint, "sha256:" + "b" * 64)
            self.assertEqual(a_view[b.instance_name].version, "0.0.0-test")
            self.assertEqual(a_view[b.instance_name].pid, os.getpid())
            self.assertFalse(a_view[b.instance_name].is_self)
        finally:
            await b.stop()
            await a.stop()


def _short_id() -> str:
    """Random suffix for test instance names — keeps two parallel runs
    on the same host from colliding on the mDNS instance name."""
    import secrets
    return secrets.token_hex(3)


# ---------------------------------------------------------------------------
# ListServers RPC + actor servers CLI — end-to-end against a real daemon
# ---------------------------------------------------------------------------


class ListServersRpcTests(unittest.IsolatedAsyncioTestCase):
    """Round-trip the ListServers RPC against a daemon spawned for a
    tempdir HOME. Verifies the daemon's snapshot contains a self
    record with the expected fingerprint + instance name."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p4-rpc-"))
        (self._tmp / ".actor").mkdir()
        # Use a fixed instance name so the assertion is deterministic.
        self._instance_name = "p4-rpc-" + _short_id()
        (self._tmp / ".actor" / "settings.kdl").write_text(
            f'daemon {{\n    name "{self._instance_name}"\n}}\n',
        )
        self._proc: subprocess.Popen | None = None

    def tearDown(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_list_servers_returns_self_with_fingerprint(self) -> None:
        self._proc = _start_daemon(self._tmp)
        sock = self._tmp / ".actor" / "daemon.sock"

        svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
        try:
            servers = await svc.list_servers()
        finally:
            await svc.aclose()

        # At least the self record is always present.
        self.assertGreaterEqual(len(servers), 1)
        selfs = [s for s in servers if s.is_self]
        self.assertEqual(
            len(selfs), 1, msg=f"expected exactly one self record, got {servers}",
        )
        me = selfs[0]
        self.assertEqual(me.instance_name, self._instance_name)
        self.assertEqual(me.port, ADVERTISED_PORT)
        self.assertTrue(me.fingerprint.startswith("sha256:"))
        self.assertEqual(len(me.fingerprint), len("sha256:") + 64)

    async def test_get_server_info_carries_fingerprint_and_instance_name(
        self,
    ) -> None:
        self._proc = _start_daemon(self._tmp)
        sock = self._tmp / ".actor" / "daemon.sock"

        svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
        try:
            info = await svc.get_server_info()
        finally:
            await svc.aclose()
        self.assertTrue(info.fingerprint.startswith("sha256:"))
        self.assertEqual(info.instance_name, self._instance_name)


class ActorServersCliTests(unittest.TestCase):
    """`actor servers` invokes the same RPC and renders the table."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p4-cli-"))
        (self._tmp / ".actor").mkdir()
        self._instance_name = "p4-cli-" + _short_id()
        (self._tmp / ".actor" / "settings.kdl").write_text(
            f'daemon {{\n    name "{self._instance_name}"\n}}\n',
        )
        self._proc: subprocess.Popen | None = None

    def tearDown(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_actor_servers_table_includes_self_with_marker(self) -> None:
        self._proc = _start_daemon(self._tmp)
        env = {**os.environ, "HOME": str(self._tmp)}
        env.pop("ACTOR_NAME", None)
        r = subprocess.run(
            ["actor", "servers"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # Header.
        self.assertIn("NAME", r.stdout)
        self.assertIn("FINGERPRINT", r.stdout)
        # Self row (instance name with `*`).
        self.assertIn(self._instance_name + "*", r.stdout)
        self.assertIn("self", r.stdout)
        self.assertIn("sha256:", r.stdout)

    def test_actor_servers_json_round_trips_self_record(self) -> None:
        import json

        self._proc = _start_daemon(self._tmp)
        env = {**os.environ, "HOME": str(self._tmp)}
        env.pop("ACTOR_NAME", None)
        r = subprocess.run(
            ["actor", "servers", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        selfs = [s for s in data if s["is_self"]]
        self.assertEqual(len(selfs), 1)
        me = selfs[0]
        self.assertEqual(me["instance_name"], self._instance_name)
        self.assertTrue(me["fingerprint"].startswith("sha256:"))
        self.assertEqual(me["port"], ADVERTISED_PORT)


# ---------------------------------------------------------------------------
# Settings.kdl `daemon` block parsing
# ---------------------------------------------------------------------------


class DaemonConfigParseTests(unittest.TestCase):
    def test_daemon_name_round_trips(self) -> None:
        from actor.config import load_config

        tmp = Path(tempfile.mkdtemp(prefix="p4-kdl-"))
        try:
            (tmp / ".actor").mkdir()
            (tmp / ".actor" / "settings.kdl").write_text(
                'daemon {\n    name "alice"\n}\n',
            )
            cfg = load_config(cwd=tmp, home=tmp)
            self.assertEqual(cfg.daemon.name, "alice")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_daemon_key_is_rejected(self) -> None:
        from actor.config import load_config
        from actor.errors import ConfigError

        tmp = Path(tempfile.mkdtemp(prefix="p4-kdl-"))
        try:
            (tmp / ".actor").mkdir()
            (tmp / ".actor" / "settings.kdl").write_text(
                'daemon {\n    bogus "x"\n}\n',
            )
            with self.assertRaises(ConfigError):
                load_config(cwd=tmp, home=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
