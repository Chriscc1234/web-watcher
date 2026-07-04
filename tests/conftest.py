"""Global test setup.

Point the user-data root at a throwaway temp dir for the whole test session BEFORE any
web_watcher module is imported, so tests never read/migrate/write the real user data under
%LOCALAPPDATA%\\WebWatcher. Individual tests may still override WW_DATA_DIR via monkeypatch.
"""

from __future__ import annotations

import os
import tempfile

# Set at import time (conftest loads before test modules import web_watcher), unless the
# caller already pinned it.
if not os.environ.get("WW_DATA_DIR"):
    os.environ["WW_DATA_DIR"] = tempfile.mkdtemp(prefix="ww_test_dataroot_")
