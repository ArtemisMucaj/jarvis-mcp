"""Error-path integration tests for the REST API.

These specifically exercise the ``except Exception`` branches inside each
handler so the 500 responses don't rot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from jarvis.api import atomic_write, create_api_app


@pytest.fixture
def client(data_dir: Path, servers_json: Path) -> TestClient:
    app = create_api_app(mcp_port=7070)
    with TestClient(app) as client:
        yield client


class TestConfigGetErrors:
    def test_500_when_file_unreadable(
        self, client: TestClient, servers_json: Path
    ) -> None:
        # Write a corrupt JSON file — the GET handler catches the decode error
        servers_json.write_text("{ not json")
        response = client.get("/api/config")
        assert response.status_code == 500
        assert "error" in response.json()


class TestConfigPutErrors:
    def test_500_when_atomic_write_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jarvis.api as api_mod

        def boom(path: Path, data: dict) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(api_mod, "atomic_write", boom)
        response = client.put("/api/config", json={"mcpServers": {}})
        assert response.status_code == 500
        assert "disk full" in response.json()["error"]


class TestToggleServerErrors:
    def test_500_when_config_unreadable(
        self, client: TestClient, servers_json: Path
    ) -> None:
        # corrupt JSON → json.loads raises inside the handler
        servers_json.write_text("{ not json")
        response = client.post(
            "/api/servers/alpha/toggle", json={"enabled": False}
        )
        assert response.status_code == 500

    def test_500_when_body_missing(self, client: TestClient) -> None:
        # No JSON body at all → ``request.json()`` raises → caught, 500
        response = client.post("/api/servers/alpha/toggle")
        assert response.status_code == 500


class TestToggleToolErrors:
    def test_500_when_body_missing_keys(self, client: TestClient) -> None:
        # Missing "server"/"tool" → KeyError → caught, 500
        response = client.post("/api/tools/toggle", json={})
        assert response.status_code == 500


class TestPresetErrors:
    def test_update_preset_with_bad_body_400(
        self, client: TestClient, data_dir: Path
    ) -> None:
        f = data_dir / "p.json"
        f.write_text("{}")
        created = client.post(
            "/api/presets", json={"name": "p", "filePath": str(f)}
        ).json()["preset"]
        # send invalid JSON body → request.json() raises
        response = client.patch(
            f"/api/presets/{created['id']}",
            content=b"{ not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400


class TestResolveConfigPathTraversal:
    def test_unresolvable_path_hits_except_branch(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The try/except around ``Path.resolve()`` must catch nastiness."""
        import jarvis.api as api_mod

        original = Path

        class ExplodingPath(type(Path())):
            def resolve(self, *a, **kw):  # type: ignore[override]
                raise RuntimeError("boom")

        def fake_path(value):
            if isinstance(value, str) and value == "EXPLODE":
                return ExplodingPath(".")
            return original(value)

        monkeypatch.setattr(api_mod, "Path", fake_path)
        response = client.get("/api/config?path=EXPLODE")
        assert response.status_code == 400


# ── atomic_write direct error-path tests ─────────────────────────────────────


class TestAtomicWriteErrors:
    def test_cleans_up_tmp_when_write_fails(self, tmp_path: Path) -> None:
        """Force ``os.fdopen`` to fail; the ``.tmp`` file must be removed."""
        target = tmp_path / "out.json"
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def failing_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            f = real_fdopen(fd, *args, **kwargs)
            f.close()  # close the fd properly before raising
            raise OSError("write failed")

        with patch("jarvis.api.os.fdopen", side_effect=failing_fdopen):
            with pytest.raises(OSError, match="write failed"):
                atomic_write(target, {"a": 1})

        assert calls["n"] == 1
        assert not target.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_survives_unlink_failure_during_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the tmp-file cleanup itself fails with OSError, the original
        exception must still propagate."""
        target = tmp_path / "out.json"

        import jarvis.api as api_mod

        def failing_replace(src, dst):
            raise RuntimeError("replace failed")

        def failing_unlink(path):
            raise OSError("unlink failed")

        monkeypatch.setattr(api_mod.os, "replace", failing_replace)
        monkeypatch.setattr(api_mod.os, "unlink", failing_unlink)

        with pytest.raises(RuntimeError, match="replace failed"):
            atomic_write(target, {"a": 1})
