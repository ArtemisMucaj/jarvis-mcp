"""Middleware that auto-refreshes OAuth tokens on 401 errors from proxied tools.

When a proxied MCP server returns a 401/Unauthorized error inside a tool
result (e.g. "GitLab API error: 401 Unauthorized"), FastMCP's own OAuth
handler never sees it because the MCP transport itself returned HTTP 200.

This middleware:
1. Catches those ToolErrors.
2. Calls ``probe_server()`` directly — a function that opens a fresh
   connection to the backend using the same shared ``token_storage``.
   FastMCP's OAuth handler will use the stored refresh token to obtain a
   new access token silently (no browser needed when the refresh token is
   still valid).  The refreshed token is written back to disk.
3. Re-raises the original error with a "please retry" hint.

On retry the connection is rebuilt from scratch (MCPConfigTransport creates
a new StatefulProxyClient per tool call), so the fresh disk token is picked
up automatically — no proxy restart required.

A short timeout (5 s) guards against blocking on a browser-based full
re-auth flow: if the refresh token has also expired and a browser would need
to open, we fall back to directing the user to ``jarvis --auth <server>``.
"""

from __future__ import annotations

import asyncio

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

_AUTH_MARKERS = ("401", "unauthorized")
_REFRESH_TIMEOUT = 5.0  # seconds; silent refresh via refresh_token should be instant


def _is_auth_error(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _AUTH_MARKERS)


class AuthErrorMiddleware(Middleware):
    """On 401/Unauthorized ToolErrors from OAuth-backed servers, try to
    refresh the access token inline then tell the caller to retry.

    The stored refresh_token is left intact; ``probe_server()`` exchanges it
    for a new access token without any user interaction.  If the refresh
    token is also expired (browser flow needed), a fallback message is shown.
    """

    def __init__(self, raw_servers: dict[str, dict]) -> None:
        """
        Args:
            raw_servers: ``{server_name: server_config_dict}`` from
                :func:`jarvis.config.load_raw_config` (pre-OAuth-injection).
        """
        # Sort names longest-first to avoid prefix collisions
        # (e.g. "git" matching before "gitlab").
        self._servers_by_len = sorted(
            raw_servers.items(), key=lambda kv: len(kv[0]), reverse=True
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next,
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ToolError as exc:
            error_text = str(exc)
            if not _is_auth_error(error_text):
                raise

            server_name, srv_config = self._find_server(context.message.name)
            if server_name is None:
                raise

            if srv_config.get("auth") == "oauth":
                refreshed = await self._try_refresh(server_name, srv_config)
                if refreshed:
                    raise ToolError(
                        f"{error_text}\n\n"
                        f"The OAuth token for '{server_name}' has been refreshed. "
                        "Please retry your request."
                    ) from exc
                raise ToolError(
                    f"{error_text}\n\n"
                    f"Authentication failed for '{server_name}' and the token could "
                    "not be refreshed automatically. The refresh token may have expired — "
                    "please re-authenticate through your OAuth provider."
                ) from exc

            # Non-OAuth server (e.g. stdio with GITLAB_TOKEN env var).
            raise ToolError(
                f"{error_text}\n\n"
                f"Authentication failed for '{server_name}'. "
                "Check the token configuration for this server "
                "(e.g. the GITLAB_TOKEN environment variable)."
            ) from exc

    async def _try_refresh(self, server_name: str, srv_config: dict) -> bool:
        """Probe the server to trigger a silent OAuth token refresh.

        Returns True if the refresh succeeded within ``_REFRESH_TIMEOUT``
        seconds (i.e. the refresh token was still valid and no browser
        interaction was required).
        """
        from jarvis.probe import probe_server

        try:
            await asyncio.wait_for(
                probe_server(server_name, srv_config),
                timeout=_REFRESH_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            # Timed out — likely waiting for browser-based re-auth, not a
            # silent refresh.  Cancel and let the caller handle it.
            return False
        except Exception:
            return False

    def _find_server(self, tool_name: str) -> tuple[str, dict] | tuple[None, None]:
        """Return ``(server_name, config)`` whose prefix matches *tool_name*."""
        for name, cfg in self._servers_by_len:
            if tool_name.startswith(f"{name}_"):
                return name, cfg
        return None, None
