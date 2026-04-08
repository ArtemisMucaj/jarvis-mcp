"""Shared test fixtures.

Isolates every test from the real ``~/.jarvis`` directory in two layers:

1. *Before* any ``jarvis`` module is imported, ``JARVIS_DATA_DIR`` is set to
   a session-scoped temp directory.  This guarantees that the module-level
   ``config.token_storage = DiskStore(directory=str(DATA_DIR))`` binding —
   which happens exactly once on first import — never touches the user's
   real ``~/.jarvis/cache.db``.
2. A per-test ``data_dir`` fixture rebinds ``DATA_DIR``/``PRESETS_PATH`` and
   recreates ``token_storage`` against an isolated ``tmp_path`` subdir, so
   tests that need per-test isolation still get it.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Step 1: isolate the module-level ``token_storage`` from the real home dir.
# This runs at conftest *import* time, which happens before any jarvis import
# below, so ``DiskStore`` ends up pointing at a scratch directory that we
# then throw away at the end of the session.
# ---------------------------------------------------------------------------

_SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
os.environ["JARVIS_DATA_DIR"] = str(_SESSION_DATA_DIR)

# Safe to import jarvis now — DATA_DIR will resolve to _SESSION_DATA_DIR.
from jarvis import api as api_mod  # noqa: E402
from jarvis import config as config_mod  # noqa: E402
from jarvis import probe as probe_mod  # noqa: E402
from key_value.aio.stores.disk import DiskStore  # noqa: E402


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Remove the session-scoped scratch directory after the run."""
    shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all jarvis data-dir reads/writes to an isolated temp dir.

    Rebinds ``DATA_DIR`` / ``PRESETS_PATH`` / ``token_storage`` on every
    module that captured a reference at import time, so each test runs
    against a fresh, empty data directory.
    """
    fake_dir = tmp_path / "jarvis_data"
    fake_dir.mkdir()

    monkeypatch.setattr(config_mod, "DATA_DIR", fake_dir)
    monkeypatch.setattr(config_mod, "PRESETS_PATH", fake_dir / "presets.json")
    # ``api`` and ``probe`` both imported ``DATA_DIR`` by name at module load.
    monkeypatch.setattr(api_mod, "DATA_DIR", fake_dir)
    monkeypatch.setattr(probe_mod, "DATA_DIR", fake_dir)

    # Recreate the token store against the fresh dir and rebind it on every
    # module that holds a reference.  This keeps OAuth tests from leaking
    # across each other *and* makes it impossible to hit the session-scoped
    # scratch dir either.
    fresh_store = DiskStore(directory=str(fake_dir))
    monkeypatch.setattr(config_mod, "token_storage", fresh_store)
    monkeypatch.setattr(probe_mod, "token_storage", fresh_store)

    return fake_dir


@pytest.fixture
def servers_json(data_dir: Path) -> Path:
    """Write a default ``servers.json`` into the isolated data dir."""
    path = data_dir / "servers.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {
                        "url": "https://alpha.example.com/mcp",
                        "transport": "http",
                    },
                    "beta": {
                        "command": "echo",
                        "args": ["hello"],
                        "disabledTools": ["noisy"],
                    },
                    "gamma": {
                        "url": "https://gamma.example.com/mcp",
                        "transport": "http",
                        "enabled": False,
                    },
                }
            },
            indent=2,
        )
    )
    return path
