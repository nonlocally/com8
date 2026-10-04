#!/usr/bin/env python3
"""com8_workspace — the workspace axis: what material an agent works on.

A workspace is a directory, plus (when it is a git repo) the commit and branch
it was at. Recording it costs one subprocess and makes three things possible
that are impossible without it: restarting an agent where it was, moving one to
another machine honestly, and telling two agents apart on one repo.

Pure functions over paths — no daemon state, no imports from com8.py.
"""
import os
import subprocess


class WorkspaceError(Exception):
    pass


def _git(repo, *args, timeout=10):
    """Run a git command in `repo`; return stdout stripped, or None."""
    try:
        r = subprocess.run(("git", "-C", repo) + args, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def describe(path):
    """{path, ref, branch, worktree} for a directory, or None if it is absent.
    ref/branch are None when the path is not inside a git repo — a workspace
    does not have to be a repo (a data corpus is a workspace too)."""
    if not path:
        return None
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(p):
        raise WorkspaceError("no such directory: %s" % p)
    top = _git(p, "rev-parse", "--show-toplevel")
    if not top:
        return {"path": p, "ref": None, "branch": None, "worktree": False}
    ref = _git(p, "rev-parse", "HEAD")
    branch = _git(p, "rev-parse", "--abbrev-ref", "HEAD")
    inside = _git(p, "rev-parse", "--is-inside-work-tree")
    return {"path": p, "ref": ref,
            "branch": None if branch == "HEAD" else branch,
            "worktree": bool(inside) and os.path.isfile(os.path.join(p, ".git"))}


def make_worktree(repo, name, base=None):
    """Create `<repo>-worktrees/<name>` on branch `com8/<name>`. Idempotent:
    an existing worktree for that branch is adopted, not duplicated."""
    src = os.path.abspath(os.path.expanduser(repo))
    top = _git(src, "rev-parse", "--show-toplevel")
    if not top:
        raise WorkspaceError("--worktree needs a git repo: %s" % src)
    branch = "com8/%s" % name
    dest = os.path.join(os.path.dirname(top),
                        "%s-worktrees" % os.path.basename(top), name)
    if os.path.isdir(dest):
        return describe(dest)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as e:
        raise WorkspaceError("cannot create worktree parent: %s" % e)
    if _git(top, "rev-parse", "--verify", branch) is not None:
        # The branch already exists (the normal recovery path: `git worktree
        # remove` deliberately leaves the branch behind) — adopt it as the
        # worktree's commit-ish. A bare `git worktree add <dest>` with no
        # ref here would silently create a NEW branch named after the
        # destination directory instead, orphaning the original `com8/<name>`.
        args = ["worktree", "add", dest, branch]
    else:
        args = ["worktree", "add", "-b", branch, dest]
        if base:
            args.append(base)
    if _git(top, *args, timeout=60) is None:
        raise WorkspaceError("git worktree add failed for %s" % dest)
    return describe(dest)
