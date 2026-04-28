"""Helper functions for the watch dashboard."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..types import Actor, Status

# -- Status icons -----------------------------------------------------------

STATUS_ICON = {
    Status.RUNNING: "♤",  # animated in tree.py via RUNNING_FRAMES
    Status.DONE: "",
    Status.ERROR: "󰗖",
    Status.IDLE: "",
    Status.STOPPED: "",
    Status.INTERACTIVE: "⌨",  # U+2328 KEYBOARD — plain, non-emoji variant
}


# -- Group actors by parent -------------------------------------------------

def group_by_parent(actors: list[Actor], statuses: dict[str, Status]) -> dict[str | None, list[Actor]]:
    """Group actors by parent, handling cycles and missing parents."""
    actor_names = {a.name for a in actors}

    def _has_cycle(a: Actor) -> bool:
        seen: set[str] = set()
        cur = a.parent
        while cur is not None and cur in actor_names:
            if cur in seen:
                return True
            seen.add(cur)
            parent_actor = next((x for x in actors if x.name == cur), None)
            cur = parent_actor.parent if parent_actor else None
        return False

    by_parent: dict[str | None, list[Actor]] = {}
    for a in actors:
        parent = a.parent if a.parent in actor_names else None
        if parent is not None and _has_cycle(a):
            parent = None
        by_parent.setdefault(parent, []).append(a)

    def sort_key(a: Actor) -> int:
        s = statuses.get(a.name, Status.IDLE)
        order = {Status.RUNNING: 0, Status.ERROR: 1, Status.IDLE: 2, Status.DONE: 3, Status.STOPPED: 4}
        return order.get(s, 9)

    for children in by_parent.values():
        children.sort(key=lambda a: a.updated_at or a.created_at or "", reverse=True)
        children.sort(key=sort_key)

    return by_parent


# -- Read log entries --------------------------------------------------------

def read_log_entries(actor: Actor) -> list:
    """Read raw LogEntry list for an actor (full read)."""
    entries, _ = read_log_entries_since(actor, None)
    return entries


def read_log_entries_since(actor: Actor, cursor=None):
    """Read entries that have arrived since `cursor` for an actor.

    Returns ``(new_entries, next_cursor)`` — same contract as
    ``Agent.read_logs_since``. Pass ``None`` for a full read on first
    call; pass the returned cursor back next time to pick up only new
    tail entries without re-parsing the whole session."""
    from ..agents.claude import ClaudeAgent
    from ..agents.codex import CodexAgent
    from ..interfaces import Agent
    from ..types import AgentKind

    agent: Agent
    if actor.agent == AgentKind.CLAUDE:
        agent = ClaudeAgent()
    else:
        agent = CodexAgent()

    if actor.agent_session is None:
        return [], cursor
    try:
        return agent.read_logs_since(Path(actor.dir), actor.agent_session, cursor)
    except Exception:
        return [], cursor


# -- Compute git diff --------------------------------------------------------

class FileDiff:
    """Diff for a single file.

    `added` / `removed` are the per-file ± line counts. When the
    constructor is called without them they fall back to a synchronous
    `difflib.unified_diff` over the contents — that fallback only fires
    in tests/manual callers; the production pipeline (`compute_diff`)
    parses the counts straight out of `git diff` output and passes
    them in explicitly so no extra diff work runs at render time."""

    def __init__(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
        added: int | None = None,
        removed: int | None = None,
    ) -> None:
        self.file_path = file_path
        self.old_content = old_content
        self.new_content = new_content
        if added is None or removed is None:
            a, r = _count_diff_lines(old_content, new_content)
            self.added = a if added is None else added
            self.removed = r if removed is None else removed
        else:
            self.added = added
            self.removed = removed


def _count_diff_lines(old: str, new: str) -> tuple[int, int]:
    """Fallback ± line counter for FileDiff callers that didn't
    pre-compute. compute_diff bypasses this by passing counts straight
    from `git diff`'s parsed output."""
    import difflib
    added = 0
    removed = 0
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="",
    ):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


class DiffResult:
    """Result of compute_diff."""
    def __init__(self, files: list[FileDiff] | None = None, reason: str = "") -> None:
        self.files = files
        self.reason = reason


def read_head_oid(actor: Actor) -> str | None:
    """Cheap `git rev-parse HEAD` on the actor's worktree. Used as
    part of the diff-view cache key — the diff against merge-base
    only needs recomputing when HEAD moves (or width changes). Returns
    None when the worktree isn't a git repo or git fails for any
    reason; callers should treat that as a cache miss."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=actor.dir,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    oid = result.stdout.strip()
    return oid or None


def git_index_mtime(actor: Actor) -> float | None:
    """Locate the index file for an actor's worktree and return its
    mtime as a float, or None if the file isn't there.

    Handles both shapes:
    - **primary checkout**: ``<wt>/.git`` is a directory; the index
      lives at ``<wt>/.git/index``.
    - **`git worktree` checkout** (which is what `actor new` creates
      for non-primary actors): ``<wt>/.git`` is a small text file
      with ``gitdir: <path>`` pointing at the per-worktree git dir;
      the index lives at ``<that-path>/index``.

    Used by the live-refresh poller as a near-zero-cost "did
    something happen since last tick" signal — beats running
    `git status` or `git diff` on every poll. Misses unstaged edits
    (the index only updates on `git add`/`rm`/etc.), so the poller
    pairs this with a `git diff --shortstat` call to catch worktree
    changes the index doesn't reflect."""
    wt = Path(actor.dir)
    git = wt / ".git"
    try:
        if git.is_dir():
            index = git / "index"
        elif git.is_file():
            content = git.read_text(errors="replace")
            gitdir: Path | None = None
            for line in content.splitlines():
                if line.startswith("gitdir: "):
                    raw = line[len("gitdir: "):].strip()
                    gd = Path(raw)
                    if not gd.is_absolute():
                        gd = (wt / gd).resolve()
                    gitdir = gd
                    break
            if gitdir is None:
                return None
            index = gitdir / "index"
        else:
            return None
        return index.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


def parse_shortstat(line: str) -> tuple[int, int]:
    """Parse `git diff --shortstat` summary line into (added, removed).

    Output format examples (note the leading space):
      `` 3 files changed, 7 insertions(+), 2 deletions(-)``
      `` 1 file changed, 1 insertion(+)``         (no deletions case)
      `` 1 file changed, 1 deletion(-)``          (no insertions case)
      ``""``                                      (no changes)

    Returns (0, 0) for empty / unparseable input rather than raising —
    the badge is best-effort UX, not a correctness gate."""
    added = 0
    removed = 0
    for chunk in line.split(","):
        chunk = chunk.strip()
        if "insertion" in chunk:
            try:
                added = int(chunk.split()[0])
            except (ValueError, IndexError):
                pass
        elif "deletion" in chunk:
            try:
                removed = int(chunk.split()[0])
            except (ValueError, IndexError):
                pass
    return added, removed


def compute_diff_shortstat(actor: Actor) -> tuple[int, int] | None:
    """Quick `git diff --shortstat` against the merge base — used by
    the watch DIFF tab to populate the (±N) badge label in <100ms
    while the heavier `compute_diff` + render path is still running.

    Two subprocesses (`merge-base` + `diff --shortstat`); returns the
    combined (added, removed) line counts. Untracked files are NOT
    included — shortstat only sees the index. The full build's later
    `_update_diff_tab_label` call lands the authoritative number when
    it commits, which may revise the badge upward if untracked files
    contributed lines.

    Returns None when git fails (no repo, missing base, etc.)."""
    base_branch = actor.base_branch or "HEAD"
    try:
        mb = subprocess.run(
            ["git", "merge-base", base_branch, "HEAD"],
            capture_output=True, text=True, cwd=actor.dir,
        )
    except Exception:
        return None
    if mb.returncode != 0:
        if "not a git repository" in (mb.stderr or "").lower():
            return None
        diff_ref = base_branch
    else:
        diff_ref = mb.stdout.strip() or base_branch

    try:
        ss = subprocess.run(
            ["git", "diff", "--shortstat", diff_ref],
            capture_output=True, text=True, cwd=actor.dir,
        )
    except Exception:
        return None
    if ss.returncode != 0:
        return None
    return parse_shortstat(ss.stdout)


def _parse_diff_files(diff_output: str) -> list[dict]:
    """Parse `git diff --no-color` output into per-file metadata.

    Returns a list of dicts with: ``path``, ``added``, ``removed``,
    ``is_new``, ``is_deleted``, ``is_binary``. The walk is a flat
    state machine — no regex on every line — so it stays cheap on
    large diffs. Multiple hunks per file aggregate into the same
    counts. Lines like ``\\ No newline at end of file`` are skipped.

    The file path comes from the ``+++ b/<path>`` line when present
    (authoritative for adds and modifies), falling back to
    ``--- a/<path>`` for deletes (where the +++ side is /dev/null).
    The ``diff --git a/X b/Y`` header is intentionally not parsed
    for the path because it's ambiguous when paths contain spaces
    — git only quotes it for special chars."""
    files: list[dict] = []
    cur: dict | None = None
    in_hunk = False

    for line in diff_output.split("\n"):
        if line.startswith("diff --git "):
            if cur is not None:
                files.append(cur)
            cur = {
                "path": None,
                "added": 0,
                "removed": 0,
                "is_new": False,
                "is_deleted": False,
                "is_binary": False,
            }
            in_hunk = False
            continue
        if cur is None:
            continue
        if line.startswith("--- "):
            rest = line[4:]
            if rest == "/dev/null":
                cur["is_new"] = True
            else:
                # Strip the leading prefix segment. Default git output
                # uses `a/<path>`, but `diff.mnemonicPrefix=true`
                # produces `c/`/`w/` instead. Splitting on the first
                # `/` covers both. Fallback path for deletes —
                # overridden by the +++ line when present.
                slash = rest.find("/")
                if slash >= 0 and cur["path"] is None:
                    cur["path"] = rest[slash + 1:]
            in_hunk = False
            continue
        if line.startswith("+++ "):
            rest = line[4:]
            if rest == "/dev/null":
                cur["is_deleted"] = True
            else:
                slash = rest.find("/")
                if slash >= 0:
                    cur["path"] = rest[slash + 1:]
            in_hunk = False
            continue
        if line.startswith("Binary files "):
            cur["is_binary"] = True
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            cur["added"] += 1
        elif line.startswith("-"):
            cur["removed"] += 1
        # Context (' ') and `\ No newline at end of file` markers are
        # intentionally ignored.

    if cur is not None:
        files.append(cur)
    return files


def _git_cat_file_batch(refs: list[str], cwd: str) -> dict[str, str]:
    """Batch-read git objects via a single ``git cat-file --batch``.

    `refs` are typically ``<merge-base>:<path>`` strings. Returns a
    dict mapping each input ref to its decoded content. Refs that
    don't resolve (e.g. paths added since the merge-base) map to ``""``.

    Decoding uses utf-8 with ``errors="replace"`` so a stray binary
    blob doesn't crash the whole render — the substituted U+FFFD
    chars surface as garbled text in the diff view, which is a
    deliberately mild failure mode. Tracked binary files are
    detected upstream by `_parse_diff_files` (``is_binary``) and
    skipped before they reach the FileDiff list, so this path
    mostly handles utf-8-clean tracked content."""
    if not refs:
        return {}
    try:
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
        stdin_data = ("\n".join(refs) + "\n").encode("utf-8")
        stdout, _stderr = proc.communicate(stdin_data)
    except Exception:
        return {ref: "" for ref in refs}

    contents: dict[str, str] = {}
    pos = 0
    n = len(stdout)
    for ref in refs:
        if pos >= n:
            contents[ref] = ""
            continue
        nl = stdout.find(b"\n", pos)
        if nl == -1:
            contents[ref] = ""
            break
        header = stdout[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        if header.endswith(" missing"):
            contents[ref] = ""
            continue
        # Format: <sha> <type> <size>. rsplit shields against shas /
        # types containing spaces (they don't, but it's free safety).
        parts = header.rsplit(" ", 2)
        if len(parts) != 3:
            contents[ref] = ""
            continue
        try:
            size = int(parts[2])
        except ValueError:
            contents[ref] = ""
            continue
        body = stdout[pos:pos + size]
        # The protocol emits a trailing LF after each body — skip it
        # so the next iteration reads the next header cleanly.
        pos += size + 1
        contents[ref] = body.decode("utf-8", errors="replace")
    return contents


def compute_diff(actor: Actor) -> DiffResult:
    """Compute diff for an actor's worktree against the merge base.

    Spawns a constant number of subprocesses regardless of how many
    files changed — at most four:

    1. ``git merge-base <base> HEAD`` (also detects "not a git repo"
       via stderr, so the prior ``git rev-parse --git-dir`` probe
       drops out entirely).
    2. ``git diff --no-color <merge-base>`` — single unified diff
       parsed in-process for paths and ± counts.
    3. ``git ls-files --others --exclude-standard`` — untracked.
    4. ``git cat-file --batch`` — every tracked file's pre-image read
       in one round-trip; replaces the per-file ``git show``.

    The 4th call is skipped when there are no tracked changes that
    need an old-content read (pure adds + untracked-only). New
    contents come from disk reads, same as before.

    Down from N+5 in the per-file ``git show`` era — a 50-file diff
    used to spawn 55 subprocesses cold."""
    worktree_dir = actor.dir
    base_branch = actor.base_branch or "HEAD"

    try:
        # 1. Merge-base. A non-git cwd surfaces as "fatal: not a git
        # repository" on stderr; treat that as the legacy
        # "no repository" reason. Any other failure (e.g. unrelated
        # histories, missing base ref) falls through to base_branch
        # so we still produce a diff.
        mb = subprocess.run(
            ["git", "merge-base", base_branch, "HEAD"],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        if mb.returncode != 0:
            stderr = (mb.stderr or "").lower()
            if "not a git repository" in stderr:
                return DiffResult(reason="no repository")
            diff_ref = base_branch
        else:
            diff_ref = mb.stdout.strip() or base_branch

        # 2. Tracked changes — one diff with hunks. Pinning
        # `--src-prefix`/`--dst-prefix` overrides the user's
        # `diff.mnemonicPrefix` so the parser sees the standard
        # `a/`/`b/` prefixes regardless of local git config.
        diff_proc = subprocess.run(
            ["git", "diff", "--no-color",
             "--src-prefix=a/", "--dst-prefix=b/",
             diff_ref],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        tracked = (
            _parse_diff_files(diff_proc.stdout)
            if diff_proc.returncode == 0 else []
        )

        # 3. Untracked files.
        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        untracked_paths = (
            untracked_proc.stdout.splitlines()
            if untracked_proc.returncode == 0 else []
        )

        if not tracked and not untracked_paths:
            return DiffResult(reason="working tree clean")

        # 4. Batch-read old contents for every tracked, non-binary,
        # non-new file in a single cat-file invocation.
        refs_needed: list[str] = []
        for f in tracked:
            if f["path"] is None or f["is_new"] or f["is_binary"]:
                continue
            refs_needed.append(f"{diff_ref}:{f['path']}")
        old_contents = (
            _git_cat_file_batch(refs_needed, worktree_dir)
            if refs_needed else {}
        )

        wt = Path(worktree_dir)
        file_diffs: list[FileDiff] = []
        for f in tracked:
            path = f["path"]
            if path is None or f["is_binary"]:
                # Binary diffs render as nothing useful in our
                # text-oriented view; skip rather than mount blank
                # rows.
                continue
            old = "" if f["is_new"] else old_contents.get(f"{diff_ref}:{path}", "")
            if f["is_deleted"]:
                new = ""
            else:
                try:
                    new = (wt / path).read_text(errors="replace")
                except (FileNotFoundError, OSError):
                    new = ""
            if old != new:
                file_diffs.append(FileDiff(
                    file_path=path,
                    old_content=old,
                    new_content=new,
                    added=f["added"],
                    removed=f["removed"],
                ))

        for path in untracked_paths:
            if not path:
                continue
            try:
                new = (wt / path).read_text(errors="replace")
            except (FileNotFoundError, OSError):
                new = ""
            # Untracked files are wholly new — every line counts as
            # added. This matches what `git diff` would report if
            # they were staged.
            added = len(new.splitlines())
            file_diffs.append(FileDiff(
                file_path=path,
                old_content="",
                new_content=new,
                added=added,
                removed=0,
            ))

        if not file_diffs:
            return DiffResult(reason="working tree clean")
        return DiffResult(files=file_diffs)
    except Exception:
        return DiffResult(reason="error")
