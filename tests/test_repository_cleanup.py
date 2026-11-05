import importlib.util
import subprocess
from pathlib import Path


def test_cleanup_preserves_database_and_logs_on_disk(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/untrack_runtime.py"
    spec = importlib.util.spec_from_file_location("cleanup", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    names = ["data/paper.sqlite", "logs/history.log", "src/__pycache__/test.pyc", "source.py"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"historical fixture")
    subprocess.run(["git", "add", "--", *names], cwd=tmp_path, check=True)
    assert set(module.untrack_runtime(tmp_path)) == set(names[:3])
    assert all((tmp_path / name).read_bytes() == b"historical fixture" for name in names)
    tracked = subprocess.check_output(["git", "ls-files"], cwd=tmp_path, text=True).splitlines()
    assert tracked == ["source.py"]
