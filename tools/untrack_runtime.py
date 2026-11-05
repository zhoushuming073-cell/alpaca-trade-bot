"""Remove generated runtime files from Git's index only.

This never deletes disk files, logs, databases or Git history.  It is intended
to be run in a checkout of the remote branch before publishing, so local
databases, logs, caches and generated reports cannot leak into the public
repository.  Run from the repository root; no commit/push is performed.
"""
from pathlib import Path, PurePosixPath
import subprocess

RUNTIME_DIRS = {"data", "logs", "test-results", ".pytest_cache", "htmlcov"}


def untrack_runtime(root):
    root = Path(root).resolve()
    top = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True, encoding="utf-8"
    ).strip()).resolve()
    if top != root:
        raise ValueError("Run from the repository root")
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
    selected = []
    for name in filter(None, paths):
        path = PurePosixPath(name)
        if (
            path.parts[0] in RUNTIME_DIRS
            or "__pycache__" in path.parts
            or any(part.endswith(".egg-info") for part in path.parts)
            or path.suffix in {".pyc", ".sqlite", ".db"}
            or name.endswith((".sqlite-wal", ".sqlite-shm", ".log", ".xml"))
            or name == ".coverage"
        ):
            selected.append(name)
    for name in selected:
        subprocess.run(
            ["git", "rm", "--cached", "--ignore-unmatch", "--", name],
            cwd=root, check=True, stdout=subprocess.DEVNULL,
        )
    return selected


if __name__ == "__main__":
    removed = untrack_runtime(Path.cwd())
    print(f"Untracked {len(removed)} generated files; local data and logs preserved on disk.")
