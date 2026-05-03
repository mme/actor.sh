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
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Resolved once; the fakes' bin directory is checked into the repo so
# the path is deterministic.
_FAKES_BIN = (Path(__file__).resolve().parent.parent / "fakes" / "bin").resolve()


@dataclass
class IsolatedHome:
    home: Path
    cwd: Path
    fakes_log: Path
    _entered: bool = field(default=False)
    _orig_dir: Optional[Path] = field(default=None)

    # ------- env helpers -------

    def env(self, **overrides) -> dict[str, str]:
        """Build an env dict with HOME / PATH / fake-log vars set up.

        Pass overrides to layer additional vars (e.g.
        FAKE_CLAUDE_RESPONSE='...') for a single subprocess call.
        """
        base = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{_FAKES_BIN}:{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_LOG": str(self.fakes_log / "claude.jsonl"),
            "FAKE_CODEX_LOG": str(self.fakes_log / "codex.jsonl"),
        }
        base.update({k: str(v) for k, v in overrides.items()})
        return base

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

    def run_cli(self, args: list[str], **env_overrides) -> "CompletedProcess":
        """Run `actor <args...>` with this env's HOME and PATH."""
        from .cli import run_actor_cli
        return run_actor_cli(args, env=self.env(**env_overrides), cwd=self.cwd)

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
