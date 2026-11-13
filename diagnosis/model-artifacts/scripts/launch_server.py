#!/usr/bin/env python3
"""Trusted entrypoint used to launch the unmodified service under -I.

Only the project root is added to sys.path; the data directory is passed as a
CLI argument and is never imported from.
"""

import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path("/workspace")
sys.path.insert(0, str(PROJECT_ROOT))
runpy.run_module("avprovenance", run_name="__main__", alter_sys=True)
