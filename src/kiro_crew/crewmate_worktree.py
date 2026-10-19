"""Each crewmate works in a git worktree of its own.

Two crewmates handed the same repository must not share a working tree or a
checked-out branch: one would commit the other's edits, or land a commit on the
branch the other has checked out. So when a crewmate's worker works on a git
repository, the gateway gives that crewmate its own worktree of it:

* one per crewmate per repository, under the crewmate's workspace
  (``<workspace>/worktrees/<repo>-<hash>``), reused by every worker it opens;
* on a branch of its own, ``crew/<crewmate-slug>/<repo>``, cut from the
  repository's default branch the first time and kept after that;
* registered in a small index beside those worktrees
  (``<workspace>/worktrees/index.json``) so the turn's ``[PROJECT]`` line can name
  the repository and branch without running git.

The worker's ``slot.project`` (its cwd and project scope) is pointed at that
worktree at the start of its turn (:func:`kiro_crew.dashboard.crewmate_worktree_scope`),
so the shared checkout is never a worker's cwd. When the crewmate is removed its
worktrees go with it (:func:`remove_crewmate_worktrees`); a worktree with
uncommitted changes is kept and reported rather than deleted, the branch always
stays (its commits are the crewmate's work), and the original checkout is never
touched.

Every git call goes through the follow-up card's sandboxed runner
(:mod:`kiro_crew.dashboard.handlers.worktree`), with its repo-code guards: no
hooks, no fsmonitor, and a repository declaring a checkout filter is refused.
All functions here block; callers run them off the event loop.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The directory under a crewmate's workspace that holds its worktrees.
WORKTREES_DIR = "worktrees"
#: The index beside those worktrees: ``{worktree path: {repo, branch}}``.
INDEX_FILE = "index.json"
#: Every crewmate branch starts with this, then the crewmate's slug.
BRANCH_PREFIX = "crew/"

# An absolute or home-relative path inside free text: a worker's brief or a goal.
_PATH_RE = re.compile(r"(?<![\w.~/-])((?:~|/)[^\s'\"`<>|;,()\[\]{}]+)")
_PATH_TRAILING = ".:!?*"
_MAX_PATHS = 20
_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class CrewmateWorktree:
    """A crewmate's worktree of one repository."""

    repo: str  # the repository's own checkout (the main worktree root)
    path: str  # this crewmate's worktree root
    branch: str
    cwd: str  # where the worker starts: the same subdirectory inside the worktree


class WorktreeRefused(RuntimeError):
    """The repository cannot be given a crewmate worktree (the reason says why)."""


def _wt():
    from kiro_crew.dashboard.handlers import worktree as wt  # circular: handlers import config

    return wt


def _name(text: str) -> str:
    return (_NAME_STRIP_RE.sub("-", text).strip("-.") or "repo")[:40]


def worktrees_root(workspace_dir: str) -> str:
    """Where *workspace_dir*'s crewmate keeps its worktrees."""
    return os.path.join(os.path.realpath(workspace_dir), WORKTREES_DIR)


def branch_for(slug: str, repo: str) -> str:
    """The branch a crewmate of *slug* works on in *repo*."""
    return f"{BRANCH_PREFIX}{_name(slug)}/{_name(os.path.basename(repo))}"


def path_for(workspace_dir: str, repo: str) -> str:
    """The worktree a crewmate whose workspace is *workspace_dir* gets for *repo*."""
    digest = hashlib.sha256(os.path.realpath(repo).encode("utf-8", "surrogateescape"))
    return os.path.join(
        worktrees_root(workspace_dir), f"{_name(os.path.basename(repo))}-{digest.hexdigest()[:8]}"
    )


def _has_git_marker(path: str) -> bool:
    """Whether *path* or an ancestor holds ``.git``: the cheap check before any spawn."""
    current = Path(path)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return True
    return False


def repo_root(path: str) -> str | None:
    """The repository checkout *path* belongs to, else ``None``.

    For a path inside a linked worktree (another crewmate's, say) this is the
    repository's own checkout, read from git's common directory, so every
    crewmate maps one repository to one root. A bare repository answers ``None``.
    """
    wt = _wt()
    real = os.path.realpath(os.path.expanduser(path))
    start = real if os.path.isdir(real) else os.path.dirname(real)
    if not os.path.isdir(start) or not _has_git_marker(start):
        return None
    proc = wt._run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start)
    common = proc.stdout.strip() if proc.returncode == 0 else ""
    if not common or os.path.basename(os.path.normpath(common)) != ".git":
        return None
    return os.path.realpath(os.path.dirname(os.path.normpath(common)))


def _read_index(root: str) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(Path(root, INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str)
        and isinstance(v, dict)
        and isinstance(v.get("repo"), str)
        and isinstance(v.get("branch"), str)
    }


def _write_index(root: str, index: dict[str, dict[str, str]]) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(Path(root, INDEX_FILE), json.dumps(index, indent=2, sort_keys=True))


def describe(project: str) -> tuple[str, str, str] | None:
    """``(worktree, repo, branch)`` when *project* is inside a crewmate worktree.

    Reads only the index file (no git), so the per-turn ``[PROJECT]`` line can
    name it. ``None`` for any other directory.
    """
    if not project:
        return None
    real = os.path.realpath(project)
    parts = Path(real).parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i - 1] == WORKTREES_DIR:
            root = str(Path(*parts[:i]))
            entry = _read_index(root).get(str(Path(*parts[: i + 1])))
            if entry:
                return str(Path(*parts[: i + 1])), entry["repo"], entry["branch"]
    return None


def _registered_branch(wt, repo: str, dest: str) -> str | None:
    trees = wt._worktree_branches(repo)
    if trees is None:
        raise WorktreeRefused("git could not list this repository's worktrees")
    return trees.get(wt._norm_path(dest))


def ensure(slug: str, workspace_dir: str, path: str) -> CrewmateWorktree | None:
    """The crewmate's own worktree for the repository *path* is in, made if missing.

    ``None`` when *path* is not inside a git repository. Raises
    :class:`WorktreeRefused` when the repository is one the gateway will not
    check out for it (a checkout filter, an unlistable worktree table, a
    destination something else owns). The worktree is reused across workers: an
    existing one on the crewmate's branch is returned as it is, whatever is in
    it, because it holds this crewmate's own work.
    """
    wt = _wt()
    repo = repo_root(path)
    if repo is None:
        return None
    ws_real = os.path.realpath(workspace_dir)
    if os.path.commonpath([ws_real, repo]) == repo:
        # The crewmate's own workspace sits inside this repository (a home
        # directory under git, say): that is not a repository it was handed.
        return None
    dest = path_for(workspace_dir, repo)
    branch = branch_for(slug, repo)
    real = os.path.realpath(os.path.expanduser(path))
    start = real if os.path.isdir(real) else os.path.dirname(real)
    # The same subdirectory, measured from the work tree *path* sits in (the
    # repository's checkout, or a worktree of it).
    top = wt._git_toplevel(start) or repo
    rel = os.path.relpath(start, top)
    sub = "" if rel == "." or rel.startswith("..") else rel
    registered = _registered_branch(wt, repo, dest) if os.path.exists(dest) else None
    if registered is None:
        if os.path.exists(dest):
            raise WorktreeRefused(f"{dest} exists and is not a worktree of {repo}")
        _create(wt, repo, dest, branch)
    elif registered != branch:
        raise WorktreeRefused(f"{dest} is registered to {registered or 'a detached HEAD'}")
    root = worktrees_root(workspace_dir)
    index = _read_index(root)
    if index.get(dest) != {"repo": repo, "branch": branch}:
        index[dest] = {"repo": repo, "branch": branch}
        _write_index(root, index)
    cwd = os.path.join(dest, sub) if sub and os.path.isdir(os.path.join(dest, sub)) else dest
    return CrewmateWorktree(repo=repo, path=dest, branch=branch, cwd=cwd)


def _create(wt, repo: str, dest: str, branch: str) -> None:
    """Add *dest* on *branch*: cut the branch first unless it already exists."""
    offending = wt._checkout_filter(repo)
    if offending:
        raise WorktreeRefused(f"the repository configures a checkout filter ({offending})")
    trees = wt._worktree_branches(repo)
    if trees is None:
        raise WorktreeRefused("git could not list this repository's worktrees")
    holder = next((p for p, held in trees.items() if held == branch), None)
    if holder is not None:
        raise WorktreeRefused(f"{branch} is checked out at {holder}")
    claimed = False
    base_sha = ""
    if not wt._resolve_commit(repo, f"refs/heads/{branch}"):
        base_sha = wt._resolve_commit(repo, wt._resolve_base_ref(repo))
        if not base_sha:
            raise WorktreeRefused("the repository has no commit to branch from")
        if not wt._claim_branch(repo, branch, base_sha):
            raise WorktreeRefused(f"could not create {branch}")
        claimed = True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.mkdir(dest)
    except FileExistsError:
        if claimed:
            wt._cleanup_partial(repo, dest, branch, claimed=True, created=False, base_sha=base_sha)
        raise WorktreeRefused(f"{dest} already exists") from None
    proc = wt._run_git(["worktree", "add", dest, branch], repo)
    if proc.returncode != 0 or not os.path.exists(os.path.join(dest, ".git")):
        wt._cleanup_partial(repo, dest, branch, claimed=claimed, created=True, base_sha=base_sha)
        raise WorktreeRefused(wt._git_error(proc) if proc.returncode else "worktree add failed")
    logger.info("crewmate worktree %s on %s from %s", dest, branch, repo)


#: The base-branch note for the next worker turn in a worktree, by worktree path.
#: Process-local: the turn that follows the sync runs in this gateway.
_BASE_NOTES: dict[str, str] = {}


def _default_branch(wt, repo: str) -> str:
    """The repository's default branch: ``origin/HEAD``, else a local ``main`` or ``master``.

    Not the shared checkout's ``HEAD``: whoever has that checkout open (a person, or a
    worker that stepped out of its worktree) decides what it points at, and a crewmate
    branch rebased onto another crewmate's branch would take that crewmate's work.
    ``HEAD`` only when the repository has neither.
    """
    if wt._resolve_commit(repo, "refs/remotes/origin/HEAD"):
        return "origin/HEAD"
    for name in ("main", "master"):
        if wt._resolve_commit(repo, f"refs/heads/{name}"):
            return name
    return "HEAD"


def _base_label(wt, repo: str, ref: str) -> str:
    """The base ref as a person names it: ``main`` rather than ``HEAD`` or ``origin/HEAD``."""
    proc = wt._run_git(["rev-parse", "--abbrev-ref", ref], repo)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name if name and name != "HEAD" else ref


def sync_with_base(entry: CrewmateWorktree) -> str:
    """Bring *entry*'s branch up to the repository's default branch; the note for the turn.

    Before a worker's turn: when the default branch (``_resolve_base_ref`` in the
    repository's own checkout, ``main`` for a local repository) has commits the
    crewmate's branch lacks, a clean worktree is rebased onto it, and a worktree
    with uncommitted changes is left as it is and the worker told how far behind
    it is. A rebase that stops is aborted, so the branch is never left mid-rebase.
    ``""`` when the branch already holds the base. The note is also kept for
    :func:`take_base_note`, which the turn's ``[PROJECT]`` line reads. Blocking.
    """
    from kiro_crew import crewmate_prompts

    wt = _wt()
    ref = _default_branch(wt, entry.repo)
    base = wt._resolve_commit(entry.repo, ref)
    if not base:
        return ""
    proc = wt._run_git(["rev-list", "--count", f"HEAD..{base}"], entry.path)
    try:
        count = int(proc.stdout.strip()) if proc.returncode == 0 else 0
    except ValueError:
        count = 0
    if count <= 0:
        return ""
    values = {"branch": entry.branch, "base": _base_label(wt, entry.repo, ref), "count": count}
    status = wt._run_git(["status", "--porcelain", "--untracked-files=no"], entry.path)
    if status.returncode != 0 or status.stdout.strip():
        note = crewmate_prompts.fill("worktree.base_moved_dirty", **values)
    else:
        rebase = wt._run_git(["rebase", base], entry.path)
        if rebase.returncode == 0:
            note = crewmate_prompts.fill("worktree.base_rebased", **values)
            logger.info("crewmate worktree %s rebased onto %s (%d)", entry.path, base, count)
        else:
            wt._run_git(["rebase", "--abort"], entry.path)
            note = crewmate_prompts.fill(
                "worktree.base_rebase_stopped", reason=wt._git_error(rebase), **values
            )
    _BASE_NOTES[entry.path] = note
    return note


def take_base_note(path: str) -> str:
    """The base-branch note :func:`sync_with_base` left for worktree *path*, once."""
    return _BASE_NOTES.pop(path, "")


def paths_in(text: str) -> list[str]:
    """Absolute or ``~`` paths named in *text*, in order, at most a bounded few."""
    out: list[str] = []
    for match in _PATH_RE.finditer(text or ""):
        candidate = match.group(1).rstrip(_PATH_TRAILING)
        if len(candidate) > 1 and candidate not in out:
            out.append(candidate)
        if len(out) >= _MAX_PATHS:
            break
    return out


def remove_crewmate_worktrees(workspace_dir: str) -> tuple[list[str], list[str]]:
    """Remove the worktrees a crewmate's workspace holds; ``(removed, kept)``.

    ``git worktree remove`` without ``--force``: a worktree with uncommitted
    changes is kept (and listed in ``kept``) rather than deleted. Branches stay.
    The repository's own checkout is never touched.
    """
    wt = _wt()
    root = worktrees_root(workspace_dir)
    index = _read_index(root)
    removed: list[str] = []
    kept: list[str] = []
    for dest, entry in sorted(index.items()):
        repo = entry["repo"]
        if not os.path.isdir(dest):
            removed.append(dest)
            with contextlib.suppress(Exception):
                wt._run_git(["worktree", "prune"], repo)
            continue
        try:
            proc = wt._run_git(["worktree", "remove", dest], repo)
        except Exception:  # noqa: BLE001 - no sandbox, no git: keep it
            logger.warning("crewmate worktree %s could not be removed", dest, exc_info=True)
            kept.append(dest)
            continue
        if proc.returncode == 0:
            removed.append(dest)
        else:
            logger.warning("crewmate worktree %s kept: %s", dest, wt._git_error(proc))
            kept.append(dest)
    remaining = {k: v for k, v in index.items() if k in kept}
    if index:
        if remaining:
            _write_index(root, remaining)
        else:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(root, INDEX_FILE))
            with contextlib.suppress(OSError):
                os.rmdir(root)
    return removed, kept


def remove_for_member(name: str, config) -> None:
    """Remove the worktrees of crewmate *name* (its record read from *config*). Best-effort.

    Called when the crewmate is deleted; never raises, so it cannot block that.
    """
    from kiro_crew.config import loader

    record = getattr(config, "agents", {}).get(name)
    if record is None:
        return
    try:
        workspace = str(loader.workspace_dir_for(getattr(record, "workspace", "") or None))
        _removed, kept = remove_crewmate_worktrees(workspace)
    except Exception:  # noqa: BLE001 - never blocks the crewmate's removal
        logger.warning("worktrees of %s were not removed", name, exc_info=True)
        return
    if kept:
        logger.warning(
            "kept %d worktree(s) of %s with uncommitted changes: %s", len(kept), name, kept
        )
