"""Owned task checkouts and explicit, conflict-checked change integration.

Worktrees protect the caller's working files from concurrent agents. They are
not an operating-system sandbox. Git tasks start at the recorded HEAD; dirty
caller changes remain in the caller's checkout and are never silently copied.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import user_manta_dir
from ..state_lock import state_lock
from .context import _project_identity


class IsolationError(RuntimeError):
    """A task checkout or proposed integration cannot be safely established."""


_IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _git(root: Path, *args: str, env: dict | None = None, binary: bool = False) -> str | bytes:
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    if env:
        clean_env.update(env)
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
            capture_output=True, env=clean_env, check=False, timeout=60,
        )  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise IsolationError("Git operation exceeded 60 seconds; inspect the checkout before retrying") from exc
    if result.returncode:
        raise IsolationError(result.stderr.decode(errors="replace").strip() or "Git command failed")
    return result.stdout if binary else result.stdout.decode(errors="replace").strip()


def _copy(source: Path, destination: Path, excluded: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _IGNORED or (Path(directory) / name).resolve() == excluded
        }

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def prepare(task_id: str, manifest: dict, mode: str = "auto") -> dict:
    """Create one owned detached worktree or non-Git directory snapshot."""
    if mode not in {"auto", "worktree", "directory"}:
        raise IsolationError("isolation must be auto, worktree, or directory")
    source = Path(manifest["project_root"])
    original = Path(manifest["original_cwd"])
    owner = user_manta_dir().expanduser().resolve() / ".state" / "task-workspaces" / task_id
    if owner.exists():
        raise IsolationError("task workspace already exists")
    if source == Path.home() and not (source / ".git").exists():
        raise IsolationError("Select a project directory rather than your home directory")
    work = owner / "work"
    git = (source / ".git").exists()
    if mode == "worktree" and not git:
        raise IsolationError("worktree isolation requires a Git repository")
    if git and mode == "directory":
        raise IsolationError(
            "Git tasks use worktree isolation; directory isolation is for non-Git projects"
        )
    owner.mkdir(parents=True)
    result = dict(manifest)
    result.update(
        {
            "source_project_root": str(source),
            "source_original_cwd": str(original),
            "source_project_identity": manifest["project_identity"],
            "isolation_owner": str(owner),
            "isolation": "worktree" if git else "directory",
        }
    )
    try:
        if git:
            base = str(_git(source, "rev-parse", "HEAD"))
            dirty = bool(_git(source, "status", "--porcelain"))
            _git(source, "worktree", "add", "--detach", str(work), base)
            result.update({"base_revision": base, "source_dirty": dirty})
        else:
            _copy(source, owner / "baseline", owner.parent.parent.resolve())
            _copy(owner / "baseline", work, owner.parent.parent.resolve())
        execution_cwd = work / original.relative_to(source)
        if not execution_cwd.is_dir():
            raise IsolationError(
                "Working subdirectory is absent from the committed snapshot; commit it or submit from the repository root"
            )
        result.update(
            {
                "schema_version": 2,
                "project_root": str(work),
                "original_cwd": str(execution_cwd),
                "execution_root": str(work),
                "project_identity": _project_identity(work),
            }
        )
        return result
    except Exception:
        if git and work.exists():
            try:
                _git(source, "worktree", "remove", "--force", str(work))
            except IsolationError:
                pass
        shutil.rmtree(owner, ignore_errors=True)
        raise


def _owned(manifest: dict) -> tuple[Path, Path, Path]:
    owner = Path(manifest.get("isolation_owner", "")).resolve(strict=True)
    work = Path(manifest.get("execution_root", "")).resolve(strict=True)
    source = Path(manifest.get("source_project_root", "")).resolve(strict=True)
    expected = (user_manta_dir() / ".state" / "task-workspaces").resolve()
    if owner.parent != expected or work != owner / "work" or not work.is_dir():
        raise IsolationError("Task workspace is outside the owned task directory")
    return owner, work, source


def _paths(root: Path) -> dict[str, Path]:
    result = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in _IGNORED for part in rel.parts):
            continue
        if path.is_symlink() or path.is_file():
            result[rel.as_posix()] = path
    return result


def _digest(path: Path | None) -> str | None:
    if path is None or not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() + f":{path.stat().st_mode & 0o777}"


def changes(manifest: dict) -> dict:
    """Snapshot all tracked and non-ignored new files without mutating any index."""
    owner, work, _ = _owned(manifest)
    if manifest["isolation"] == "worktree":
        descriptor, index = tempfile.mkstemp(prefix="manta-task-index-", dir=owner)
        os.close(descriptor)
        os.unlink(index)
        try:
            env = {"GIT_INDEX_FILE": index}
            _git(work, "read-tree", manifest["base_revision"], env=env)
            _git(work, "add", "-A", "--", ".", env=env)
            patch = _git(
                work,
                "diff",
                "--cached",
                "--binary",
                manifest["base_revision"],
                "--",
                ".",
                env=env,
                binary=True,
            )
            raw_names = _git(
                work,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                manifest["base_revision"],
                "--",
                ".",
                env=env,
                binary=True,
            )
        finally:
            Path(index).unlink(missing_ok=True)
            Path(index + ".lock").unlink(missing_ok=True)
        files = [name.decode(errors="replace") for name in raw_names.split(b"\0") if name]
        patch_path = owner / "changes.patch"
        patch_path.write_bytes(patch)
        return {
            "changed_files": files,
            "diff": patch.decode(errors="replace"),
            "diff_path": str(patch_path),
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
        }
    baseline = owner / "baseline"
    old, new = _paths(baseline), _paths(work)
    files = [
        name
        for name in sorted(old.keys() | new.keys())
        if _digest(old.get(name)) != _digest(new.get(name))
    ]
    diffs = []
    for name in files:
        try:
            left = (
                old[name].read_text().splitlines(keepends=True)
                if name in old and not old[name].is_symlink()
                else []
            )
            right = (
                new[name].read_text().splitlines(keepends=True)
                if name in new and not new[name].is_symlink()
                else []
            )
            diffs.extend(
                difflib.unified_diff(left, right, fromfile="a/" + name, tofile="b/" + name)
            )
        except (UnicodeError, OSError):
            diffs.append(f"Binary or symbolic-link change: {name}\n")
    fingerprint = "\0".join(name + ":" + str(_digest(new.get(name))) for name in files)
    return {
        "changed_files": files,
        "diff": "".join(diffs),
        "patch_sha256": hashlib.sha256(fingerprint.encode()).hexdigest(),
    }


def integrate(manifest: dict, expected_patch: str | None = None) -> dict:
    """Apply a reviewed result only if every touched source path still matches."""
    owner, work, source = _owned(manifest)
    with state_lock(user_manta_dir() / ".state" / "task-integration.lock"):
        if _project_identity(source) != manifest.get("source_project_identity"):
            raise IsolationError(
                "The source project or branch changed; restore the original checkout before integrating"
            )
        result = changes(manifest)
        if expected_patch and result.get("patch_sha256") != expected_patch:
            raise IsolationError(
                "Task changes changed after review; review the latest diff before integrating"
            )
        if not result["changed_files"]:
            return result
        if manifest["isolation"] == "worktree":
            if "Subproject commit " in result["diff"] or "mode 160000" in result["diff"]:
                raise IsolationError("Submodule or embedded-repository changes require manual integration")
            patch = Path(result["diff_path"])
            _git(source, "apply", "--check", "--binary", str(patch))
            _git(source, "apply", "--binary", str(patch))
            return result
        baseline = owner / "baseline"
        for name in result["changed_files"]:
            target = source / name
            if any(
                parent.is_symlink()
                for parent in [target, *target.parents]
                if parent.is_relative_to(source)
            ):
                raise IsolationError(f"Cannot integrate through a symbolic link: {name}")
            if (work / name).is_symlink() or (baseline / name).is_symlink():
                raise IsolationError(f"Review symbolic-link changes manually: {name}")
            if any(path.is_dir() for path in (target, baseline / name, work / name)):
                # File/directory replacements need a structural transaction.
                # Reject the complete proposal before writing anything: an
                # ordinary file rollback cannot restore bytes over a newly
                # created directory after a later copy failure.
                raise IsolationError(f"Review file/directory replacements manually: {name}")
            if _digest(target) != _digest(baseline / name):
                raise IsolationError(f"Source file changed since submission: {name}")
        backups = {}
        temporaries: list[Path] = []
        try:
            for name in result["changed_files"]:
                target, proposed = source / name, work / name
                backups[name] = (
                    (target.read_bytes(), target.stat().st_mode) if target.is_file() else None
                )
                if proposed.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary_name = tempfile.mkstemp(prefix=".manta-integrating-", dir=target.parent)
                    os.close(descriptor)
                    temporary = Path(temporary_name)
                    temporaries.append(temporary)
                    shutil.copy2(proposed, temporary)
                    os.replace(temporary, target)
                else:
                    target.unlink(missing_ok=True)
        except Exception:
            for name, backup in backups.items():
                target = source / name
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(backup[0])
                    target.chmod(backup[1])
            raise
        finally:
            for temporary in temporaries:
                temporary.unlink(missing_ok=True)
        return result


def discard(manifest: dict) -> None:
    """Remove only the owned checkout; never touch the source working files."""
    owner, work, source = _owned(manifest)
    if manifest["isolation"] == "worktree":
        _git(source, "worktree", "remove", "--force", "--force", str(work))
    shutil.rmtree(owner)
