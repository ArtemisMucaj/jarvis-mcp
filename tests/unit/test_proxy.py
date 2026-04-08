"""Unit tests for jarvis.proxy.build_proxy."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastmcp.mcp_config import MCPConfig
from fastmcp.server import FastMCP


def _make_config() -> MCPConfig:
    return MCPConfig.model_validate(
        {
            "mcpServers": {
                "gl": {"command": "npx", "args": ["-y", "some-mcp"]},
                "remote": {
                    "url": "https://remote.example.com/mcp",
                    "transport": "http",
                },
            }
        }
    )


def test_build_proxy_returns_fastmcp():
    from jarvis.proxy import build_proxy

    with (
        patch("jarvis.proxy.StatefulProxyClient"),
        patch("jarvis.proxy.ProxyClient"),
        patch("jarvis.proxy.ProxyProvider"),
    ):
        result = build_proxy(_make_config(), name="test")
    assert isinstance(result, FastMCP)


def test_build_proxy_uses_stateful_for_stdio():
    from jarvis.proxy import build_proxy

    with (
        patch("jarvis.proxy.StatefulProxyClient") as mock_stateful,
        patch("jarvis.proxy.ProxyClient") as mock_proxy,
        patch("jarvis.proxy.ProxyProvider"),
    ):
        build_proxy(_make_config(), name="test")

    # stdio server "gl" → StatefulProxyClient called once
    assert mock_stateful.call_count == 1
    # http server "remote" → ProxyClient called once
    assert mock_proxy.call_count == 1


def test_build_proxy_uses_new_stateful_as_factory_for_stdio():
    from jarvis.proxy import build_proxy
    from fastmcp.server.providers.proxy import ProxyProvider

    captured_factories = []
    real_init = ProxyProvider.__init__

    def capturing_init(self, client_factory, **kwargs):
        captured_factories.append(client_factory)
        real_init(self, client_factory, **kwargs)

    with (
        patch.object(ProxyProvider, "__init__", capturing_init),
        patch("jarvis.proxy.StatefulProxyClient") as mock_stateful,
        patch("jarvis.proxy.ProxyClient") as mock_proxy,
    ):
        mock_stateful_instance = MagicMock()
        mock_stateful.return_value = mock_stateful_instance
        mock_proxy_instance = MagicMock()
        mock_proxy.return_value = mock_proxy_instance
        build_proxy(_make_config(), name="test")

    # Two providers added: one per server
    assert len(captured_factories) == 2
    # Relies on dict insertion order (Python 3.7+): "gl" first, "remote" second.
    # Factory for stdio server must be new_stateful bound method
    assert captured_factories[0] == mock_stateful_instance.new_stateful
    # Factory for http server must be new bound method
    assert captured_factories[1] == mock_proxy_instance.new


def test_build_proxy_adds_provider_per_server():
    from jarvis.proxy import build_proxy
    from fastmcp.server import FastMCP

    added = []
    real_add = FastMCP.add_provider

    def capturing_add(self, provider, *, namespace=""):
        added.append(namespace)
        real_add(self, provider, namespace=namespace)

    with (
        patch.object(FastMCP, "add_provider", capturing_add),
        patch("jarvis.proxy.StatefulProxyClient"),
        patch("jarvis.proxy.ProxyClient"),
        patch("jarvis.proxy.ProxyProvider"),
    ):
        build_proxy(_make_config(), name="test")

    # FastMCP.__init__ calls add_provider once internally (namespace="") for the
    # local provider, so we expect 3 total: 1 from init + 2 from build_proxy.
    named = [ns for ns in added if ns]
    assert len(named) == 2
    assert set(named) == {"gl", "remote"}
