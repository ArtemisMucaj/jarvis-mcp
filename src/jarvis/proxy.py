"""Proxy builder for Jarvis.

Replaces ``fastmcp.server.create_proxy(MCPConfig)`` with a builder that uses
``StatefulProxyClient`` for stdio backends (persistent subprocess per frontend
session) and ``ProxyClient`` for HTTP/SSE backends (fresh connection per request).
"""

from __future__ import annotations

import logging

from fastmcp.mcp_config import MCPConfig, StdioMCPServer
from fastmcp.server import FastMCP
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyProvider,
    StatefulProxyClient,
)

log = logging.getLogger("jarvis.proxy")

# Timeout (seconds) for a backend MCP server to complete its initial
# handshake.  Without this, a single unreachable backend (e.g. SSL
# handshake hanging behind a corporate proxy) blocks the entire
# tools/list response indefinitely.
BACKEND_INIT_TIMEOUT = 10


def build_proxy(config: MCPConfig, name: str = "jarvis") -> FastMCP:
    """Build a FastMCP proxy server from an MCPConfig.

    For each server in *config*:
    - stdio servers get a ``StatefulProxyClient`` with ``new_stateful`` as the
      client factory, so the subprocess lives for the duration of each frontend
      session rather than being respawned on every tool call.
    - HTTP/SSE servers get a ``ProxyClient`` with ``new`` as the factory,
      giving a fresh connection per request (stateless, correct for HTTP).

    All clients are given an ``init_timeout`` so that unreachable backends
    fail fast instead of blocking the entire tools/list response.

    Args:
        config: Validated MCPConfig with servers already configured
                (OAuth injected, env vars expanded).
        name:   Name for the resulting FastMCP server.

    Returns:
        A ``FastMCP`` server with one ``ProxyProvider`` per backend, namespaced
        by server name.
    """
    mcp: FastMCP = FastMCP(name=name)
    # Keep strong references to StatefulProxyClient instances so they are not
    # garbage-collected while the server is alive (new_stateful reads _caches).
    mcp._stateful_clients: list = []  # type: ignore[attr-defined]

    for server_name, server in config.mcpServers.items():
        transport = server.to_transport()

        if isinstance(server, StdioMCPServer):
            log.info(
                "Backend %-20s  stdio  (stateful, timeout=%ds)",
                server_name,
                BACKEND_INIT_TIMEOUT,
            )
            client = StatefulProxyClient(transport, init_timeout=BACKEND_INIT_TIMEOUT)
            mcp._stateful_clients.append(client)
            factory = client.new_stateful
        else:
            url = getattr(server, "url", "?")
            log.info(
                "Backend %-20s  http   %s (timeout=%ds)",
                server_name,
                url,
                BACKEND_INIT_TIMEOUT,
            )
            client = ProxyClient(transport, init_timeout=BACKEND_INIT_TIMEOUT)
            factory = client.new

        mcp.add_provider(ProxyProvider(factory), namespace=server_name)

    return mcp
