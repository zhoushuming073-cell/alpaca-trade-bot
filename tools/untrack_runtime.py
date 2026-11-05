"""Remove generated data/cache from Git's index only; keep disk files and logs.

Run in a checkout of the remote branch before publishing the repair there.
No commit, push, history rewrite, filesystem deletion or credential reads.
"""
from pathlib import Path, PurePosixPath
import subprocess


def untrack_runtime(root):
    root = Path(root).resolve()
    top = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root, text=True,encoding="utf-8").strip()).resolve()
    if top != root:
        raise ValueError("Run from the repository root")
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
    selected = []
    for name in filter(None, paths):
        path = PurePosixPath(name)
        if path.parts[0] == "logs":
            continue
        if (path.parts[0] == "data" or "__pycache__" in path.parts or
                any(p.endswith(".egg-info") for p in path.parts) or
                path.suffix in {".pyc", ".sqlite", ".db"} or name.endswith((".sqlite-wal", ".sqlite-shm"))):
            selected.append(name)
    for name in selected:
        subprocess.run(["git", "rm", "--cached", "--ignore-unmatch", "--", name], cwd=root, check=True, stdout=subprocess.DEVNULL)
    return selected


if __name__ == "__main__":
    removed = untrack_runtime(Path.cwd())
    print(f"Untracked {len(removed)} generated files; local data and historical logs preserved.")
