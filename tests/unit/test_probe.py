"""Unit tests for ``jarvis.probe`` (network-free).

``probe_server`` itself actually connects to a backend, so we don't exercise it
directly here.  Instead we verify the helpers (``free_port``, the warning
filter) and ``probe_all_servers`` with ``probe_server`` monkeypatched.
"""

from __future__ import annotations

import logging
import socket

import pytest

from jarvis import probe as probe_mod
from jarvis.probe import SuppressMcpSessionWarning, free_port, probe_all_servers


# ── free_port ────────────────────────────────────────────────────────────────


class TestFreePort:
    def test_returns_port_in_valid_range(self) -> None:
        port = free_port()
        assert 1 <= port <= 65535

    def test_port_is_actually_bindable(self) -> None:
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_consecutive_calls_return_different_ports(self) -> None:
        # Not strictly guaranteed by the OS but overwhelmingly likely; this
        # protects against returning a hard-coded constant.
        ports = {free_port() for _ in range(5)}
        assert len(ports) > 1


# ── SuppressMcpSessionWarning ────────────────────────────────────────────────


class TestSuppressMcpSessionWarning:
    def _make_record(self, exc: BaseException | None) -> logging.LogRecord:
        record = logging.LogRecord(
            name="fastmcp.client.transports.config",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Failed to connect",
            args=(),
            exc_info=(type(exc), exc, None) if exc else None,
        )
        return record

    def test_demotes_mcp_error_warning_to_debug(self) -> None:
        from mcp import McpError
        from mcp.types import ErrorData

        err = McpError(ErrorData(code=-32000, message="boom"))
        record = self._make_record(err)
        assert SuppressMcpSessionWarning().filter(record) is True
        assert record.levelno == logging.DEBUG
        assert record.levelname == "DEBUG"

    def test_unrelated_warning_is_unchanged(self) -> None:
        record = self._make_record(RuntimeError("unrelated"))
        assert SuppressMcpSessionWarning().filter(record) is True
        assert record.levelno == logging.WARNING

    def test_warning_without_exc_info_is_unchanged(self) -> None:
        record = self._make_record(None)
        assert SuppressMcpSessionWarning().filter(record) is True
        assert record.levelno == logging.WARNING


# ── probe_all_servers ────────────────────────────────────────────────────────


class TestProbeAllServers:
    async def test_returns_results_per_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(name: str, raw: dict) -> list[dict]:
            return [{"name": f"{name}_tool", "description": ""}]

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)

        result = await probe_all_servers(
            {
                "a": {"url": "http://a"},
                "b": {"url": "http://b"},
            }
        )
        assert result == {
            "a": [{"name": "a_tool", "description": ""}],
            "b": [{"name": "b_tool", "description": ""}],
        }

    async def test_failed_probe_yields_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(name: str, raw: dict) -> list[dict]:
            if name == "bad":
                raise RuntimeError("boom")
            return [{"name": "t", "description": ""}]

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)

        result = await probe_all_servers(
            {"good": {"url": "http://g"}, "bad": {"url": "http://b"}}
        )
        assert result["good"] == [{"name": "t", "description": ""}]
        assert result["bad"] == []

    async def test_timeout_yields_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def hanging_probe(name: str, raw: dict) -> list[dict]:
            await asyncio.sleep(10)
            return []

        monkeypatch.setattr(probe_mod, "probe_server", hanging_probe)

        result = await probe_all_servers({"slow": {"url": "http://s"}}, timeout=0.05)
        assert result == {"slow": []}

    async def test_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_probe(name: str, raw: dict) -> list[dict]:  # pragma: no cover
            raise AssertionError("should not be called")

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)
        assert await probe_all_servers({}) == {}

    async def test_base_exception_still_caught_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A BaseException subclass that is *not* KeyboardInterrupt/SystemExit/
        # GeneratorExit must still produce an empty list for that server.
        class Weird(BaseException):
            pass

        async def fake_probe(name: str, raw: dict) -> list[dict]:
            raise Weird("weird")

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)
        result = await probe_all_servers({"x": {"url": "http://x"}})
        assert result == {"x": []}

    async def test_prints_probe_failure_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def fake_probe(name: str, raw: dict) -> list[dict]:
            raise ValueError("bad things")

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)
        await probe_all_servers({"myserver": {"url": "http://x"}})
        err = capsys.readouterr().err
        assert "[myserver]" in err
        assert "ValueError" in err
        assert "bad things" in err


# ── silence() context manager ────────────────────────────────────────────────


class TestSilence:
    def test_redirects_stderr_and_restores_it(
        self, data_dir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``silence`` writes to DATA_DIR / "jarvis.log" — the ``data_dir``
        # fixture already points DATA_DIR at an isolated temp dir.
        import sys
        from jarvis.probe import silence

        original_stderr = sys.stderr
        with silence():
            assert sys.stderr is not original_stderr
            print("swallowed stderr", file=sys.stderr)
        assert sys.stderr is original_stderr

        log_path = data_dir / "jarvis.log"
        assert log_path.exists()
        assert "swallowed stderr" in log_path.read_text()

    def test_removes_log_handler_on_exit(self, data_dir) -> None:
        import logging
        from jarvis.probe import silence

        root = logging.getLogger()
        before = list(root.handlers)
        with silence():
            assert len(root.handlers) == len(before) + 1
        assert list(root.handlers) == before

    def test_cleans_up_on_exception(self, data_dir) -> None:
        import logging
        import sys
        from jarvis.probe import silence

        original_stderr = sys.stderr
        root = logging.getLogger()
        before = list(root.handlers)

        with pytest.raises(RuntimeError, match="boom"):
            with silence():
                raise RuntimeError("boom")

        assert sys.stderr is original_stderr
        assert list(root.handlers) == before


# ── probe_server() ───────────────────────────────────────────────────────────


class TestProbeServer:
    async def test_returns_tool_list_with_prefix_stripped(
        self, monkeypatch: pytest.MonkeyPatch, data_dir
    ) -> None:
        """``probe_server`` should call ``create_proxy`` on a single-server
        config, then list_tools() and strip the ``{name}_`` prefix."""
        from types import SimpleNamespace
        from jarvis import probe as probe_mod_inner

        class FakeProxy:
            async def list_tools(self) -> list:
                return [
                    SimpleNamespace(name="myserver_alpha", description="first"),
                    SimpleNamespace(name="myserver_beta", description=None),
                    SimpleNamespace(name="unprefixed", description="c"),
                ]

        captured: dict = {}

        def fake_create_proxy(cfg, *, name: str):
            captured["cfg"] = cfg
            captured["name"] = name
            return FakeProxy()

        monkeypatch.setattr(probe_mod_inner, "create_proxy", fake_create_proxy)
        # avoid configure_servers running for real (not needed, but safer)
        monkeypatch.setattr(probe_mod_inner, "configure_servers", lambda cfg: None)

        result = await probe_mod_inner.probe_server(
            "myserver", {"url": "http://x", "transport": "http"}
        )
        assert result == [
            {"name": "alpha", "description": "first"},
            {"name": "beta", "description": ""},
            {"name": "unprefixed", "description": "c"},
        ]
        assert captured["name"] == "probe_myserver"
        assert "myserver" in captured["cfg"].mcpServers

    async def test_oauth_server_uses_free_port(
        self, monkeypatch: pytest.MonkeyPatch, data_dir
    ) -> None:
        """OAuth servers should receive an OAuth client with a *free* callback
        port, not the hard-coded 9876 that the long-running server uses."""
        from types import SimpleNamespace
        from jarvis import probe as probe_mod_inner

        captured_oauth: dict = {}

        class FakeOAuth:
            def __init__(self, **kwargs) -> None:
                captured_oauth.update(kwargs)

        class FakeProxy:
            async def list_tools(self) -> list:
                return []

        monkeypatch.setattr(probe_mod_inner, "OAuth", FakeOAuth)
        monkeypatch.setattr(probe_mod_inner, "create_proxy", lambda cfg, *, name: FakeProxy())
        monkeypatch.setattr(probe_mod_inner, "free_port", lambda: 55555)

        result = await probe_mod_inner.probe_server(
            "oauth_server",
            {"url": "http://o", "transport": "http", "auth": "oauth"},
        )
        assert result == []
        assert captured_oauth["callback_port"] == 55555
        assert captured_oauth["client_name"] == "Jarvis Proxy"

    async def test_system_exit_is_converted_to_oserror(
        self, monkeypatch: pytest.MonkeyPatch, data_dir
    ) -> None:
        """``list_tools`` raising SystemExit (e.g. uvicorn bail-out) must
        be surfaced as OSError, not propagated as SystemExit."""
        from jarvis import probe as probe_mod_inner

        class FakeProxy:
            async def list_tools(self) -> list:
                raise SystemExit(1)

        monkeypatch.setattr(probe_mod_inner, "create_proxy", lambda cfg, *, name: FakeProxy())
        monkeypatch.setattr(probe_mod_inner, "configure_servers", lambda cfg: None)

        with pytest.raises(OSError, match="uvicorn exited"):
            await probe_mod_inner.probe_server(
                "x", {"url": "http://x", "transport": "http"}
            )
