"""Unit tests for low-level helpers in ``jarvis.api``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jarvis.api import atomic_write, config_locks, get_lock


class TestAtomicWrite:
    def test_writes_json_with_indent(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        atomic_write(target, {"a": 1, "b": [2, 3]})
        content = target.read_text()
        assert json.loads(content) == {"a": 1, "b": [2, 3]}
        # indent=2 ⇒ multi-line output
        assert "\n" in content

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        target.write_text('{"old": true}')
        atomic_write(target, {"new": True})
        assert json.loads(target.read_text()) == {"new": True}

    def test_cleans_up_temp_file_on_serialization_error(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "out.json"
        # sets are not JSON-serialisable — json.dumps raises before os.replace
        with pytest.raises(TypeError):
            atomic_write(target, {"bad": {1, 2, 3}})
        # the original file should not exist …
        assert not target.exists()
        # … and no orphan ``.tmp`` files should be left behind
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestGetLock:
    def setup_method(self) -> None:
        config_locks.clear()

    def test_returns_same_lock_for_same_path(self, tmp_path: Path) -> None:
        p = tmp_path / "c.json"
        assert get_lock(p) is get_lock(p)

    def test_different_paths_get_different_locks(self, tmp_path: Path) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        assert get_lock(a) is not get_lock(b)

    def test_resolves_path_before_keying(self, tmp_path: Path) -> None:
        # ``foo/./c.json`` resolves to the same path as ``foo/c.json``
        p1 = tmp_path / "c.json"
        p2 = tmp_path / "." / "c.json"
        assert get_lock(p1) is get_lock(p2)

    def test_lock_is_asyncio_lock(self, tmp_path: Path) -> None:
        assert isinstance(get_lock(tmp_path / "x.json"), asyncio.Lock)


class TestStartApiThread:
    def test_starts_uvicorn_in_daemon_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``start_api_thread`` must spawn uvicorn on the correct port as a
        daemon thread and not block the caller.
        """
        import sys
        import threading
        import types

        captured: dict = {}
        started = threading.Event()

        def fake_run(**kwargs) -> None:
            captured.update(kwargs)
            # Record the thread uvicorn.run was actually invoked on — the
            # point of ``start_api_thread`` is that this is *not* the main
            # thread and that the thread is a daemon.
            current = threading.current_thread()
            captured["thread"] = current
            captured["is_main_thread"] = current is threading.main_thread()
            captured["is_daemon"] = current.daemon
            started.set()

        fake_uvicorn = types.SimpleNamespace(run=fake_run)
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

        from jarvis.api import start_api_thread

        start_api_thread(mcp_port=7070, api_port=7071)
        assert started.wait(timeout=2), "uvicorn.run never called"

        # kwargs passed to uvicorn.run
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 7071
        assert captured["log_level"] == "error"
        assert captured["app"] is not None

        # thread identity + daemon flag
        assert captured["is_main_thread"] is False, (
            "uvicorn.run was invoked on the main thread — start_api_thread "
            "should have spawned a background thread"
        )
        assert captured["is_daemon"] is True, (
            "background thread must be a daemon so it doesn't outlive the "
            "main process"
        )
