"""Pytest fixtures shared across TinkerClaw test modules.

Wave 14 W14-L12: the old test_foundation.py created a tempdir at
*import time* and mutated os.environ globally. That leaked across
every other test module that imported dragon_voice.db afterward —
and the CI job runs 9 test files in one pytest invocation, so the
leak was real. Session-scoped here so a single tmpdir is created once
per test run and cleaned up when pytest exits.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _session_db_root(tmp_path_factory: pytest.TempPathFactory):
    """One tempdir per test session; tear down at end."""
    root = Path(tempfile.mkdtemp(prefix="tinkerclaw-tests-"))
    prior = os.environ.get("TINKERCLAW_DB_PATH")
    os.environ["TINKERCLAW_DB_PATH"] = str(root / "test.db")
    try:
        yield root
    finally:
        if prior is None:
            os.environ.pop("TINKERCLAW_DB_PATH", None)
        else:
            os.environ["TINKERCLAW_DB_PATH"] = prior
        shutil.rmtree(root, ignore_errors=True)
