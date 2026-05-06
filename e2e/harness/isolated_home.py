"""Per-test HOME isolation.

Every e2e test starts in a fresh tempdir laid out like:

    $TMPDIR/<test-id>/
      home/                    ← $HOME (with .actor/ + .claude/ scaffolding)
      cwd/                     ← test working directory; usually a real git repo
      fakes_log/               ← captures fake_claude / fake_codex invocations

Use as a context manager:

    with isolated_home() as env:
        env.write_settings_kdl('role "qa" { ... }')
        env.run_cli(["new", "alice", "do x"])
        actor = env.fetch_actor("alice")

Or as a setUp helper for unittest.TestCase subclasses:

    class MyTest(unittest.TestCase):
        def setUp(self):
            self.env = isolated_home().__enter__()
            self.addCleanup(self.env.__exit__, None, None, None)

Phase 2 (issue #35): every CLI / MCP invocation requires a running
`actord`. The harness lazy-starts a per-test daemon on first `env()`
call (the call site that builds the env dict for any subprocess that
talks to actor) and SIGTERMs it at `__exit__`. Each test's daemon
binds a unix socket inside its own HOME, so concurrent test runs
don't collide.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Resolved once; the fakes' bin directory is checked into the repo so
# the path is deterministic.
_FAKES_BIN = (Path(__file__).resolve().parent.parent / "fakes" / "bin").resolve()


def _grpc_probe(socket_path: str) -> bool:
    """Quick `actor_exists` RPC against the daemon to confirm both
    the listener AND the gRPC handshake are ready. Returns True on
    success, False on any failure (the harness keeps polling)."""
    import asyncio as _asyncio

    async def _attempt() -> bool:
        from grpclib.client import Channel
        from actor._proto.actor.v1 import ActorExistsRequest, ActorServiceStub
        chan = Channel(path=socket_path)
        try:
            stub = ActorServiceStub(chan)
            await stub.actor_exists(
                ActorExistsRequest(name="__probe__"), timeout=0.5,
            )
            return True
        except Exception:
            return False
        finally:
            chan.close()

    try:
        return _asyncio.run(_attempt())
    except Exception:
        return False


@dataclass
class IsolatedHome:
    home: Path
    cwd: Path
    fakes_log: Path
    _entered: bool = field(default=False)
    _orig_dir: Optional[Path] = field(default=None)
    _daemon: Optional[subprocess.Popen] = field(default=None)
    _daemon_log: Optional[Path] = field(default=None)
    _daemon_fake_env: dict[str, str] = field(default_factory=dict)

    # ------- env helpers -------

    def env(self, **overrides) -> dict[str, str]:
        """Build an env dict with HOME / PATH / fake-log vars set up.

        Pass overrides to layer additional vars (e.g.
        FAKE_CLAUDE_RESPONSE='...') for a single subprocess call.

        Lazily ensures a per-test `actord` is running before returning
        so any subprocess this env feeds (CLI, MCP) can reach the
        daemon at $HOME/.actor/daemon.sock.

        Phase 2 detail: the agent (claude/codex fake) is spawned by
        the daemon, not the CLI. Per-call FAKE_* overrides therefore
        need to land in the *daemon's* env, not the CLI's. The
        harness restarts the daemon whenever the FAKE_* slice of
        `overrides` changes, so each test sees the responses it set up.
        """
        # Forward any FAKE_*/AGENT_*/ANTHROPIC_*/OPENAI_*/CLAUDE_*
        # override into the daemon's env — that's where the spawned
        # fake binary will pick it up.
        daemon_extra = {
            k: str(v) for k, v in overrides.items()
            if (k.startswith("FAKE_")
                or k.startswith("AGENT_")
                or k.startswith("ANTHROPIC_")
                or k.startswith("OPENAI_")
                or k.startswith("CLAUDE_"))
        }
        self._ensure_daemon(daemon_extra)
        base = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{_FAKES_BIN}:{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_LOG": str(self.fakes_log / "claude.jsonl"),
            "FAKE_CODEX_LOG": str(self.fakes_log / "codex.jsonl"),
        }
        base.update({k: str(v) for k, v in overrides.items()})
        return base

    # ------- daemon lifecycle -------

    def _ensure_daemon(self, extra_env: Optional[dict[str, str]] = None) -> None:
        """Start `actord` once per IsolatedHome. Idempotent unless the
        caller's `extra_env` differs from what the running daemon was
        started with — in which case we restart so the spawned agent
        sees the new vars (e.g. FAKE_CLAUDE_RESPONSE).

        Tests share the same fakes PATH the daemon needs to spawn
        agents (the daemon resolves `claude` / `codex` from PATH).
        Without `_FAKES_BIN` ahead of the system PATH the daemon would
        spawn the real binary from CI's environment.
        """
        extra_env = extra_env or {}
        if self._daemon is not None and self._daemon.poll() is None:
            # Empty overrides → keep the daemon as-is. The first
            # call that wanted FAKE_* vars set them; subsequent
            # callers that don't override anything inherit whatever
            # the daemon was started with. This matches the
            # pre-Phase-2 semantics where every test's fake-control
            # env was sticky for the rest of the test.
            if not extra_env or self._daemon_fake_env == extra_env:
                return
            # Different fake env — stop and restart with the new vars.
            self.stop_daemon()
        sock = self.home / ".actor" / "daemon.sock"
        sock.parent.mkdir(parents=True, exist_ok=True)
        self._daemon_log = self.home / ".actor" / "daemon.log"
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{_FAKES_BIN}:{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_LOG": str(self.fakes_log / "claude.jsonl"),
            "FAKE_CODEX_LOG": str(self.fakes_log / "codex.jsonl"),
            **extra_env,
        }
        self._daemon_fake_env = dict(extra_env)
        proc = subprocess.Popen(
            [sys.executable, "-m", "actor.daemon",
             "--listen", f"unix:{sock}",
             "--db-path", str(self.home / ".actor" / "actor.db"),
             "--pidfile", str(self.home / ".actor" / "daemon.pid"),
             "--log-file", str(self._daemon_log)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the socket to accept connections. 2s ceiling — way
        # more than the daemon's ~50ms cold start; tests fail loudly
        # if something's wrong rather than wedging the suite.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if proc.poll() is not None:
                # Daemon died during startup — surface the log so
                # CI failures point at the cause.
                tail = ""
                try:
                    tail = self._daemon_log.read_text()[-2000:]
                except OSError:
                    pass
                raise RuntimeError(
                    f"actord exited (rc={proc.returncode}) during e2e startup; "
                    f"log tail:\n{tail}"
                )
            if sock.exists():
                # Probe with an actual gRPC unary call against a known
                # cheap method. A raw AF_UNIX connect is enough for
                # "listener is up" on a TCP socket, but gRPC needs the
                # HTTP/2 handshake to settle before the daemon will
                # accept RPCs. `actor_exists` is the cheapest method —
                # no DB writes, no agent spawn — and probes the entire
                # stack end-to-end.
                if _grpc_probe(str(sock)):
                    self._daemon = proc
                    return
            time.sleep(0.02)
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError(f"actord did not bind {sock} within 2s")

    def stop_daemon(self) -> None:
        """Stop the per-test daemon. Called from __exit__; safe to
        call directly for tests that want to assert behavior with
        the daemon down."""
        proc = self._daemon
        if proc is None:
            return
        self._daemon = None
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    # ------- file helpers -------

    def write_settings_kdl(self, body: str, *, scope: str = "user") -> Path:
        """scope='user' writes ~/.actor/settings.kdl; scope='project'
        writes <cwd>/.actor/settings.kdl."""
        if scope == "user":
            target = self.home / ".actor" / "settings.kdl"
        elif scope == "project":
            target = self.cwd / ".actor" / "settings.kdl"
        else:
            raise ValueError(f"unknown scope: {scope!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    # ------- CLI helpers -------

    def run_cli(self, args: list[str], *,
                input: Optional[str] = None,
                timeout: float = 30.0,
                **env_overrides) -> "CompletedProcess":
        """Run `actor <args...>` with this env's HOME and PATH.

        `input` (kwarg-only) feeds stdin to the subprocess. Without it,
        stdin is a real PTY (no data) — see `harness.cli.run_actor_cli`
        for why that matters. `env_overrides` populate FAKE_*_RESPONSE
        and similar fake-control env vars."""
        from .cli import run_actor_cli
        return run_actor_cli(
            args, env=self.env(**env_overrides), cwd=self.cwd,
            input=input, timeout=timeout,
        )

    # ------- DB helpers -------

    def db(self):
        """Open the test's SQLite DB through actor.db (real schema)."""
        from actor.db import Database
        return Database.open(str(self.home / ".actor" / "actor.db"))

    def fetch_actor(self, name: str):
        with self.db() as db:
            return db.get_actor(name)

    def list_actor_names(self) -> list[str]:
        with self.db() as db:
            return [a.name for a in db.list_actors()]

    # ------- fake-call introspection -------

    def claude_invocations(self) -> list[dict]:
        """Read every recorded fake-claude invocation as a list of
        parsed dicts. Empty list if the fake hasn't been called."""
        log = self.fakes_log / "claude.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    def codex_invocations(self) -> list[dict]:
        log = self.fakes_log / "codex.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    # ------- context manager -------

    def __enter__(self) -> "IsolatedHome":
        self._entered = True
        self._orig_dir = Path.cwd()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Stop the daemon BEFORE wiping the tempdir so the daemon's
        # final socket / pidfile cleanup doesn't race against us.
        self.stop_daemon()
        if self._orig_dir is not None:
            try:
                os.chdir(self._orig_dir)
            except OSError:
                pass
        try:
            shutil.rmtree(self.home.parent, ignore_errors=True)
        except Exception:
            pass


def isolated_home(*, init_git: bool = True) -> IsolatedHome:
    """Create an isolated HOME tempdir for one test.

    `init_git=True` (default) initializes the cwd as a real git repo
    with one commit, so worktree-based actors have a base branch to
    fork from. Pass `init_git=False` for tests that exercise the
    no-worktree path or "not a git repo" error path.
    """
    root = Path(tempfile.mkdtemp(prefix="actor-e2e-"))
    home = root / "home"
    cwd = root / "cwd"
    fakes_log = root / "fakes_log"
    for d in (home, cwd, fakes_log,
              home / ".actor", home / ".claude" / "projects",
              home / ".codex"):
        d.mkdir(parents=True, exist_ok=True)
    if init_git:
        _init_git_repo(cwd)
    return IsolatedHome(home=home, cwd=cwd, fakes_log=fakes_log)


def _init_git_repo(path: Path) -> None:
    """Initialize a real git repo at `path` with one commit, so
    `git worktree add` has a base to fork from."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "actor-e2e",
        "GIT_AUTHOR_EMAIL": "e2e@actor.sh",
        "GIT_COMMITTER_NAME": "actor-e2e",
        "GIT_COMMITTER_EMAIL": "e2e@actor.sh",
    }
    def run(*args):
        subprocess.run(["git", *args], cwd=path, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("init", "-q", "-b", "main")
    (path / "README.md").write_text("# test repo\n")
    run("add", "README.md")
    run("commit", "-q", "-m", "initial commit")


# Lazy import marker so editor tooling doesn't choke before the real
# class is needed. CompletedProcess is just `subprocess.CompletedProcess`.
from subprocess import CompletedProcess  # noqa: E402
