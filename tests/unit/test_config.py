"""Unit tests for ``jarvis.config``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import config as config_mod
from jarvis.config import (
    active_config_from_presets,
    configure_servers,
    expand_env_vars,
    get_disabled_tools,
    load_presets,
    load_raw_config,
    save_presets,
)


# ── expand_env_vars ───────────────────────────────────────────────────────────


class TestExpandEnvVars:
    def test_substitutes_known_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_KEY", "secret")
        assert expand_env_vars("token=${MY_KEY}") == "token=secret"

    def test_leaves_unknown_variable_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
        assert expand_env_vars("x=${DEFINITELY_NOT_SET}") == "x=${DEFINITELY_NOT_SET}"

    def test_substitutes_multiple_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert expand_env_vars("${A}-${B}") == "1-2"

    def test_no_placeholders_is_pass_through(self) -> None:
        assert expand_env_vars("plain value") == "plain value"


# ── load_raw_config ───────────────────────────────────────────────────────────


class TestLoadRawConfig:
    def test_filters_disabled_servers(self, servers_json: Path) -> None:
        mcp_dict, raw_servers = load_raw_config(servers_json)
        # ``gamma`` has ``"enabled": false`` in the fixture
        assert set(mcp_dict["mcpServers"].keys()) == {"alpha", "beta"}
        assert set(raw_servers.keys()) == {"alpha", "beta"}

    def test_strips_non_standard_keys(self, servers_json: Path) -> None:
        mcp_dict, _ = load_raw_config(servers_json)
        for srv in mcp_dict["mcpServers"].values():
            assert "enabled" not in srv
            assert "disabledTools" not in srv

    def test_preserves_standard_keys(self, servers_json: Path) -> None:
        mcp_dict, _ = load_raw_config(servers_json)
        assert mcp_dict["mcpServers"]["alpha"]["url"] == "https://alpha.example.com/mcp"
        assert mcp_dict["mcpServers"]["beta"]["command"] == "echo"
        assert mcp_dict["mcpServers"]["beta"]["args"] == ["hello"]

    def test_enabled_true_is_kept(self, data_dir: Path) -> None:
        path = data_dir / "s.json"
        path.write_text(
            json.dumps(
                {"mcpServers": {"x": {"url": "http://x", "enabled": True}}}
            )
        )
        mcp_dict, _raw = load_raw_config(path)
        assert "x" in mcp_dict["mcpServers"]
        assert "enabled" not in mcp_dict["mcpServers"]["x"]


# ── get_disabled_tools ────────────────────────────────────────────────────────


class TestGetDisabledTools:
    def test_returns_prefixed_names(self, servers_json: Path) -> None:
        assert get_disabled_tools(servers_json) == {"beta_noisy"}

    def test_skips_disabled_servers(self, data_dir: Path) -> None:
        path = data_dir / "s.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "off": {
                            "url": "http://off",
                            "enabled": False,
                            "disabledTools": ["a", "b"],
                        }
                    }
                }
            )
        )
        assert get_disabled_tools(path) == set()

    def test_empty_when_no_disabled_tools(self, data_dir: Path) -> None:
        path = data_dir / "s.json"
        path.write_text(
            json.dumps({"mcpServers": {"x": {"url": "http://x"}}})
        )
        assert get_disabled_tools(path) == set()


# ── Preset management ────────────────────────────────────────────────────────


class TestPresets:
    def test_load_presets_returns_empty_when_missing(self, data_dir: Path) -> None:
        assert load_presets() == {"presets": [], "activePresetID": None}

    def test_save_and_load_roundtrip(self, data_dir: Path) -> None:
        payload = {
            "presets": [
                {
                    "id": "1",
                    "name": "work",
                    "filePath": str(data_dir / "a.json"),
                }
            ],
            "activePresetID": "1",
        }
        save_presets(payload)
        assert load_presets() == payload

    def test_save_creates_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        nested = tmp_path / "nested" / "jarvis"
        # do *not* pre-create the dir; save_presets must do it
        monkeypatch.setattr(config_mod, "DATA_DIR", nested)
        monkeypatch.setattr(config_mod, "PRESETS_PATH", nested / "presets.json")
        save_presets({"presets": [], "activePresetID": None})
        assert (nested / "presets.json").exists()


# ── active_config_from_presets ───────────────────────────────────────────────


class TestActiveConfigFromPresets:
    def test_returns_default_when_no_presets(self, data_dir: Path) -> None:
        path = active_config_from_presets()
        assert path == data_dir / "servers.json"
        # default file should have been created
        assert path.exists()
        assert json.loads(path.read_text()) == {"mcpServers": {}}

    def test_returns_preset_file_when_active(self, data_dir: Path) -> None:
        preset_file = data_dir / "work.json"
        preset_file.write_text('{"mcpServers": {}}')
        save_presets(
            {
                "presets": [
                    {"id": "p1", "name": "work", "filePath": str(preset_file)}
                ],
                "activePresetID": "p1",
            }
        )
        assert active_config_from_presets() == preset_file

    def test_falls_back_to_default_if_preset_file_missing(
        self, data_dir: Path
    ) -> None:
        save_presets(
            {
                "presets": [
                    {
                        "id": "p1",
                        "name": "work",
                        "filePath": str(data_dir / "missing.json"),
                    }
                ],
                "activePresetID": "p1",
            }
        )
        assert active_config_from_presets() == data_dir / "servers.json"

    def test_falls_back_to_default_if_id_not_found(self, data_dir: Path) -> None:
        save_presets(
            {
                "presets": [{"id": "p1", "name": "w", "filePath": "/nope"}],
                "activePresetID": "bogus",
            }
        )
        assert active_config_from_presets() == data_dir / "servers.json"


# ── configure_servers ────────────────────────────────────────────────────────


class TestConfigureServers:
    def test_expands_env_in_env_values(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastmcp.mcp_config import MCPConfig

        monkeypatch.setenv("MY_TOKEN", "abc123")
        cfg = MCPConfig.model_validate(
            {
                "mcpServers": {
                    "s": {
                        "command": "echo",
                        "args": ["hi"],
                        "env": {"TOKEN": "${MY_TOKEN}", "LITERAL": "plain"},
                    }
                }
            }
        )
        configure_servers(cfg)
        assert cfg.mcpServers["s"].env == {"TOKEN": "abc123", "LITERAL": "plain"}

    def test_no_env_is_noop(self) -> None:
        from fastmcp.mcp_config import MCPConfig

        cfg = MCPConfig.model_validate(
            {"mcpServers": {"s": {"command": "echo", "args": []}}}
        )
        configure_servers(cfg)
        # should not raise, env stays None/empty
        assert not getattr(cfg.mcpServers["s"], "env", None)

    def test_oauth_server_gets_oauth_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp.mcp_config import MCPConfig

        captured: dict = {}

        class FakeOAuth:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(config_mod, "OAuth", FakeOAuth)

        cfg = MCPConfig.model_validate(
            {
                "mcpServers": {
                    "o": {
                        "url": "https://o.example.com/mcp",
                        "transport": "http",
                        "auth": "oauth",
                    }
                }
            }
        )
        configure_servers(cfg)
        assert isinstance(cfg.mcpServers["o"].auth, FakeOAuth)
        assert captured["callback_port"] == 9876
        assert captured["client_name"] == "Jarvis Proxy"
        assert captured["token_storage"] is config_mod.token_storage


# ── clear_tokens ─────────────────────────────────────────────────────────────


class TestClearTokens:
    def test_calls_cache_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        class FakeCache:
            def clear(self) -> None:
                calls.append("clear")

        class FakeStore:
            _cache = FakeCache()

        monkeypatch.setattr(config_mod, "token_storage", FakeStore())
        config_mod.clear_tokens()
        assert calls == ["clear"]
