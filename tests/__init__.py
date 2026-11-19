"""Automated unit and integration test suite for Companion AI OS."""

import atexit
import os
import tempfile
from pathlib import Path

os.environ.setdefault("COMPANION_TEST_MODE", "1")
if not os.environ.get("COMPANION_DATA_DIR"):
    _TEST_DATA_TMP = tempfile.TemporaryDirectory(prefix="companion_test_data_", ignore_cleanup_errors=True)
    os.environ["COMPANION_DATA_DIR"] = _TEST_DATA_TMP.name
    atexit.register(_TEST_DATA_TMP.cleanup)
    from companion.config import settings

    settings.data_dir = Path(_TEST_DATA_TMP.name).resolve()
