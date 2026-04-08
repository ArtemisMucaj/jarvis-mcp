"""Integration tests for the Jarvis REST API.

Exercises every route on ``create_api_app`` end-to-end through Starlette's
``TestClient``.  Network-touching code paths (``probe_all_servers``) are
stubbed so the tests never open sockets to real MCP servers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jarvis import api as api_mod
from jarvis import probe as probe_mod
from jarvis.api import create_api_app


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(data_dir: Path, servers_json: Path) -> TestClient:
    app = create_api_app(mcp_port=7070)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def stub_probe(monkeypatch: pytest.MonkeyPatch):
    """Replace ``probe_all_servers`` with a deterministic in-memory stub."""

    async def fake_probe_all(raw_servers: dict, timeout: float = 30):
        return {
            name: [
                {"name": f"{name}_tool1", "description": "first"},
                {"name": f"{name}_tool2", "description": "second"},
            ]
            for name in raw_servers
        }

    monkeypatch.setattr(api_mod, "probe_all_servers", fake_probe_all)
    # also patch the original module so any lingering reference works
    monkeypatch.setattr(probe_mod, "probe_all_servers", fake_probe_all)
    return fake_probe_all


# ── /api/health ──────────────────────────────────────────────────────────────


class TestHealth:
    def test_returns_status_and_ports(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "mcp_port": 7070,
            "api_port": 7071,
        }


# ── /api/tools ───────────────────────────────────────────────────────────────


class TestGetTools:
    def test_returns_probe_results_for_enabled_servers(
        self, client: TestClient, stub_probe
    ) -> None:
        response = client.get("/api/tools")
        assert response.status_code == 200
        body = response.json()
        # gamma is disabled in the fixture and must not appear
        assert set(body.keys()) == {"alpha", "beta"}
        assert body["alpha"][0]["name"] == "alpha_tool1"

    def test_returns_error_on_probe_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(raw_servers, timeout: float = 30):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(api_mod, "probe_all_servers", boom)
        response = client.get("/api/tools")
        assert response.status_code == 500
        assert "probe exploded" in response.json()["error"]


# ── /api/config ──────────────────────────────────────────────────────────────


class TestConfigEndpoint:
    def test_get_returns_raw_servers_json(
        self, client: TestClient, servers_json: Path
    ) -> None:
        response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json() == json.loads(servers_json.read_text())

    def test_put_overwrites_config(
        self, client: TestClient, servers_json: Path
    ) -> None:
        new = {"mcpServers": {"solo": {"url": "http://solo"}}}
        response = client.put("/api/config", json=new)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert json.loads(servers_json.read_text()) == new

    def test_path_traversal_is_rejected(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        outside = tmp_path / "evil.json"
        outside.write_text("{}")
        response = client.get(f"/api/config?path={outside}")
        assert response.status_code == 400

    def test_explicit_path_inside_data_dir_is_accepted(
        self, client: TestClient, data_dir: Path
    ) -> None:
        alt = data_dir / "alt.json"
        alt.write_text(json.dumps({"mcpServers": {"x": {"url": "http://x"}}}))
        response = client.get(f"/api/config?path={alt}")
        assert response.status_code == 200
        assert response.json() == {"mcpServers": {"x": {"url": "http://x"}}}

    def test_non_json_file_is_rejected(
        self, client: TestClient, data_dir: Path
    ) -> None:
        alt = data_dir / "alt.txt"
        alt.write_text("not json")
        response = client.get(f"/api/config?path={alt}")
        assert response.status_code == 400


# ── /api/servers/{name}/toggle ───────────────────────────────────────────────


class TestToggleServer:
    def test_disable_server_writes_enabled_false(
        self, client: TestClient, servers_json: Path
    ) -> None:
        response = client.post(
            "/api/servers/alpha/toggle", json={"enabled": False}
        )
        assert response.status_code == 200
        data = json.loads(servers_json.read_text())
        assert data["mcpServers"]["alpha"]["enabled"] is False

    def test_enable_server_removes_enabled_key(
        self, client: TestClient, servers_json: Path
    ) -> None:
        # ``gamma`` starts with enabled=false
        response = client.post(
            "/api/servers/gamma/toggle", json={"enabled": True}
        )
        assert response.status_code == 200
        data = json.loads(servers_json.read_text())
        assert "enabled" not in data["mcpServers"]["gamma"]

    def test_unknown_server_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/servers/ghost/toggle", json={"enabled": False}
        )
        assert response.status_code == 404
        assert "not found" in response.json()["error"]


# ── /api/tools/toggle ────────────────────────────────────────────────────────


class TestToggleTool:
    def test_disable_tool_appends_to_disabled_list(
        self, client: TestClient, servers_json: Path
    ) -> None:
        response = client.post(
            "/api/tools/toggle",
            json={"server": "alpha", "tool": "destructive", "enabled": False},
        )
        assert response.status_code == 200
        data = json.loads(servers_json.read_text())
        assert data["mcpServers"]["alpha"]["disabledTools"] == ["destructive"]

    def test_enable_tool_removes_from_disabled_list(
        self, client: TestClient, servers_json: Path
    ) -> None:
        # beta starts with disabledTools=["noisy"]
        response = client.post(
            "/api/tools/toggle",
            json={"server": "beta", "tool": "noisy", "enabled": True},
        )
        assert response.status_code == 200
        data = json.loads(servers_json.read_text())
        # key must be removed entirely when list becomes empty
        assert "disabledTools" not in data["mcpServers"]["beta"]

    def test_enable_tool_not_in_list_is_noop(
        self, client: TestClient, servers_json: Path
    ) -> None:
        before = json.loads(servers_json.read_text())
        response = client.post(
            "/api/tools/toggle",
            json={"server": "alpha", "tool": "never", "enabled": True},
        )
        assert response.status_code == 200
        assert json.loads(servers_json.read_text()) == before

    def test_disable_same_tool_twice_is_idempotent(
        self, client: TestClient, servers_json: Path
    ) -> None:
        payload = {"server": "alpha", "tool": "dupe", "enabled": False}
        client.post("/api/tools/toggle", json=payload)
        client.post("/api/tools/toggle", json=payload)
        data = json.loads(servers_json.read_text())
        assert data["mcpServers"]["alpha"]["disabledTools"] == ["dupe"]

    def test_unknown_server_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/tools/toggle",
            json={"server": "ghost", "tool": "t", "enabled": False},
        )
        assert response.status_code == 404


# ── /api/presets ─────────────────────────────────────────────────────────────


class TestPresetsEndpoints:
    def test_list_initially_empty(self, client: TestClient, data_dir: Path) -> None:
        response = client.get("/api/presets")
        assert response.status_code == 200
        body = response.json()
        assert body["presets"] == []
        assert body["activePresetID"] is None
        assert body["activeConfigPath"] == str(data_dir / "servers.json")

    def test_create_preset_returns_201(
        self, client: TestClient, data_dir: Path
    ) -> None:
        preset_file = data_dir / "work.json"
        preset_file.write_text('{"mcpServers": {}}')
        response = client.post(
            "/api/presets",
            json={"name": "work", "filePath": str(preset_file)},
        )
        assert response.status_code == 201
        preset = response.json()["preset"]
        assert preset["name"] == "work"
        assert preset["id"]  # non-empty uuid
        # and appears in the listing
        listing = client.get("/api/presets").json()
        assert any(p["id"] == preset["id"] for p in listing["presets"])

    def test_create_preset_missing_fields_returns_400(
        self, client: TestClient
    ) -> None:
        response = client.post("/api/presets", json={"name": "only-name"})
        assert response.status_code == 400

    def test_update_preset(self, client: TestClient, data_dir: Path) -> None:
        f1 = data_dir / "a.json"
        f1.write_text('{"mcpServers": {}}')
        created = client.post(
            "/api/presets", json={"name": "a", "filePath": str(f1)}
        ).json()["preset"]
        response = client.patch(
            f"/api/presets/{created['id']}", json={"name": "renamed"}
        )
        assert response.status_code == 200
        assert response.json()["preset"]["name"] == "renamed"

    def test_update_unknown_preset_404(self, client: TestClient) -> None:
        response = client.patch("/api/presets/does-not-exist", json={"name": "x"})
        assert response.status_code == 404

    def test_delete_preset(self, client: TestClient, data_dir: Path) -> None:
        f = data_dir / "d.json"
        f.write_text('{"mcpServers": {}}')
        created = client.post(
            "/api/presets", json={"name": "d", "filePath": str(f)}
        ).json()["preset"]
        response = client.delete(f"/api/presets/{created['id']}")
        assert response.status_code == 200
        listing = client.get("/api/presets").json()
        assert all(p["id"] != created["id"] for p in listing["presets"])

    def test_delete_unknown_preset_404(self, client: TestClient) -> None:
        response = client.delete("/api/presets/does-not-exist")
        assert response.status_code == 404

    def test_delete_active_preset_clears_active(
        self, client: TestClient, data_dir: Path
    ) -> None:
        f = data_dir / "d.json"
        f.write_text('{"mcpServers": {}}')
        created = client.post(
            "/api/presets", json={"name": "d", "filePath": str(f)}
        ).json()["preset"]
        client.post(f"/api/presets/{created['id']}/activate")
        client.delete(f"/api/presets/{created['id']}")
        listing = client.get("/api/presets").json()
        assert listing["activePresetID"] is None

    def test_activate_preset(self, client: TestClient, data_dir: Path) -> None:
        f = data_dir / "x.json"
        f.write_text('{"mcpServers": {}}')
        created = client.post(
            "/api/presets", json={"name": "x", "filePath": str(f)}
        ).json()["preset"]
        response = client.post(f"/api/presets/{created['id']}/activate")
        assert response.status_code == 200
        assert response.json()["activePresetID"] == created["id"]
        assert client.get("/api/presets").json()["activePresetID"] == created["id"]

    def test_activate_unknown_preset_404(self, client: TestClient) -> None:
        response = client.post("/api/presets/missing/activate")
        assert response.status_code == 404

    def test_activate_default_clears_active(
        self, client: TestClient, data_dir: Path
    ) -> None:
        f = data_dir / "x.json"
        f.write_text('{"mcpServers": {}}')
        created = client.post(
            "/api/presets", json={"name": "x", "filePath": str(f)}
        ).json()["preset"]
        client.post(f"/api/presets/{created['id']}/activate")
        response = client.post("/api/presets/default/activate")
        assert response.status_code == 200
        assert response.json()["activePresetID"] is None


# ── Config round-trip across endpoints ───────────────────────────────────────


class TestConfigRoundTrip:
    """Sanity check: server/tool toggles leave a valid readable config."""

    def test_toggle_then_get_returns_updated_config(
        self, client: TestClient
    ) -> None:
        client.post("/api/servers/alpha/toggle", json={"enabled": False})
        client.post(
            "/api/tools/toggle",
            json={"server": "alpha", "tool": "bad", "enabled": False},
        )
        body = client.get("/api/config").json()
        assert body["mcpServers"]["alpha"]["enabled"] is False
        assert body["mcpServers"]["alpha"]["disabledTools"] == ["bad"]
