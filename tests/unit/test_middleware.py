"""Unit tests for ``jarvis.middleware`` (AuthErrorMiddleware).

Tests exercise every branch through the public ``on_call_tool`` interface
without touching real MCP servers.  ``probe_server`` and the middleware's
``call_next`` callable are stubbed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from jarvis.middleware import AuthErrorMiddleware


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_context(tool_name: str):
    """Build a minimal fake MiddlewareContext with the given tool name."""
    return SimpleNamespace(message=SimpleNamespace(name=tool_name))


def make_call_next(error_msg: str | None = None):
    """Return an async ``call_next`` that either succeeds or raises."""

    async def call_next(ctx):
        if error_msg is not None:
            raise ToolError(error_msg)
        return "ok"

    return call_next


# ── Passthrough (no error / non-auth error / unmatched server) ───────────────


class TestPassthrough:
    async def test_success_returns_result(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        result = await mw.on_call_tool(make_context("gitlab_list"), make_call_next())
        assert result == "ok"

    async def test_non_auth_error_propagates_unchanged(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        with pytest.raises(ToolError, match="Connection refused"):
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("Connection refused"),
            )

    async def test_auth_error_for_unmatched_tool_propagates_unchanged(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        with pytest.raises(ToolError, match="^401 Unauthorized$"):
            await mw.on_call_tool(
                make_context("unknown_tool"),
                make_call_next("401 Unauthorized"),
            )

    async def test_no_configured_servers_propagates_unchanged(self) -> None:
        mw = AuthErrorMiddleware({})
        with pytest.raises(ToolError, match="Unauthorized"):
            await mw.on_call_tool(
                make_context("any_tool"),
                make_call_next("401 Unauthorized"),
            )


# ── Auth error detection ─────────────────────────────────────────────────────


class TestAuthErrorDetection:
    """Verify that different error messages are correctly classified as auth
    or non-auth errors by observing middleware behavior."""

    async def test_detects_401_in_message(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"command": "echo"}})
        with pytest.raises(ToolError, match="token configuration"):
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("GitLab API error: 401 Unauthorized"),
            )

    async def test_detects_unauthorized_word(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"command": "echo"}})
        with pytest.raises(ToolError, match="token configuration"):
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("Request was Unauthorized"),
            )

    async def test_case_insensitive(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"command": "echo"}})
        with pytest.raises(ToolError, match="token configuration"):
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("UNAUTHORIZED access"),
            )

    async def test_ignores_unrelated_errors(self) -> None:
        """404 or 500 errors must not trigger auth handling."""
        mw = AuthErrorMiddleware({"gitlab": {"command": "echo"}})
        for msg in ("404 Not Found", "Internal server error 500"):
            with pytest.raises(ToolError, match=msg):
                await mw.on_call_tool(make_context("gitlab_list"), make_call_next(msg))


# ── Server prefix matching ───────────────────────────────────────────────────


class TestServerMatching:
    """Verify longest-prefix-wins behaviour by observing which server name
    appears in the error message."""

    async def test_longest_prefix_wins(self) -> None:
        """``gitlab_create_issue`` must match ``gitlab``, not ``git``."""
        mw = AuthErrorMiddleware(
            {
                "git": {"command": "git-mcp"},
                "gitlab": {"auth": "oauth"},
            }
        )

        async def fail_401(ctx):
            raise ToolError("401 Unauthorized")

        # gitlab is OAuth → message mentions refresh, not token config
        with pytest.raises(ToolError, match="re-authenticate"):
            await mw.on_call_tool(make_context("gitlab_create_issue"), fail_401)

    async def test_short_prefix_still_matches(self) -> None:
        """``git_push`` must match ``git``, not ``gitlab``."""
        mw = AuthErrorMiddleware(
            {
                "git": {"command": "git-mcp"},
                "gitlab": {"auth": "oauth"},
            }
        )

        async def fail_401(ctx):
            raise ToolError("401 Unauthorized")

        # git is non-OAuth → message mentions token configuration
        with pytest.raises(ToolError, match="token configuration"):
            await mw.on_call_tool(make_context("git_push"), fail_401)


# ── OAuth server: refresh flow ───────────────────────────────────────────────


class TestOAuthRefresh:
    async def test_successful_refresh_hints_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jarvis import probe as probe_mod

        async def fake_probe(name, raw):
            return [{"name": "t", "description": ""}]

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)

        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        with pytest.raises(ToolError, match="has been refreshed") as exc_info:
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("GitLab API error: 401 Unauthorized"),
            )
        assert "Please retry" in str(exc_info.value)
        # Original error message is preserved
        assert "401 Unauthorized" in str(exc_info.value)

    async def test_failed_refresh_hints_reauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jarvis import probe as probe_mod

        async def failing_probe(name, raw):
            raise RuntimeError("probe failed")

        monkeypatch.setattr(probe_mod, "probe_server", failing_probe)

        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        with pytest.raises(ToolError, match="could not be refreshed") as exc_info:
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("401 Unauthorized"),
            )
        assert "re-authenticate" in str(exc_info.value)

    async def test_timeout_during_refresh_hints_reauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jarvis.middleware as mw_mod
        from jarvis import probe as probe_mod

        async def hanging_probe(name, raw):
            await asyncio.sleep(10)

        monkeypatch.setattr(probe_mod, "probe_server", hanging_probe)
        monkeypatch.setattr(mw_mod, "REFRESH_TIMEOUT", 0.05)

        mw = AuthErrorMiddleware({"gitlab": {"auth": "oauth"}})
        with pytest.raises(ToolError, match="could not be refreshed"):
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("401 Unauthorized"),
            )


# ── Non-OAuth server ─────────────────────────────────────────────────────────


class TestNonOAuth:
    async def test_hints_token_configuration(self) -> None:
        mw = AuthErrorMiddleware({"gitlab": {"command": "gitlab-mcp"}})
        with pytest.raises(ToolError, match="token configuration") as exc_info:
            await mw.on_call_tool(
                make_context("gitlab_list"),
                make_call_next("error: 401 Unauthorized"),
            )
        assert "GITLAB_TOKEN" in str(exc_info.value)
        # Original error preserved
        assert "401 Unauthorized" in str(exc_info.value)
