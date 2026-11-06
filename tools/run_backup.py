"""Silent pythonw entry point for the scheduled database backup."""

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))
runpy.run_module("trading_bot.backup", run_name="__main__")
