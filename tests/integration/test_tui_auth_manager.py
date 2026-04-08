"""Integration tests for ``AuthManagerApp`` (the ``jarvis auth`` TUI).

Uses Textual's ``run_test`` harness with a fake in-memory token store so
that no files in ``~/.jarvis/cache.db`` are touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis import config as config_mod
from jarvis import probe as probe_mod
from jarvis.tui import AuthManagerApp


class FakeCache:
    def __init__(self, keys: list[str] | None = None) -> None:
        self._keys = list(keys or [])
        self.cleared = False

    def iterkeys(self):
        return iter(self._keys)

    def clear(self) -> None:
        self.cleared = True
        self._keys.clear()


class FakeTokenStorage:
    def __init__(self, keys: list[str] | None = None) -> None:
        self._cache = FakeCache(keys)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeTokenStorage:
    store = FakeTokenStorage(
        keys=["https://atlassian.example.com/mcp|token|abc"]
    )
    monkeypatch.setattr(config_mod, "token_storage", store)
    return store


@pytest.fixture
def auth_config(data_dir: Path) -> Path:
    path = data_dir / "servers.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "atlassian": {
                        "url": "https://atlassian.example.com/mcp",
                        "transport": "http",
                        "auth": "oauth",
                    },
                    "github": {
                        "url": "https://github.example.com/mcp",
                        "transport": "http",
                        "auth": "oauth",
                    },
                    "local": {"command": "echo", "args": ["hi"]},
                }
            },
            indent=2,
        )
    )
    return path


class TestAuthManagerPopulate:
    async def test_lists_all_servers(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            assert set(app._server_names) == {"atlassian", "github", "local"}

    async def test_shows_token_count_for_oauth_server(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        from textual.widgets import DataTable

        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            table = app.query_one(DataTable)
            # collect all cell strings from each row
            rows = []
            for row_key in table.rows:
                rows.append(
                    [str(c) for c in table.get_row(row_key)]
                )
            flat = [cell for row in rows for cell in row]
            # atlassian's url is in the fake store, so 1 token cached
            assert any("1 token" in c for c in flat)
            # github has no matching token → "none cached"
            assert any("none cached" in c for c in flat)
            # local is non-oauth → "N/A"
            assert any("N/A" in c for c in flat)

    async def test_auth_type_uppercased(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        from textual.widgets import DataTable

        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            table = app.query_one(DataTable)
            rows = [
                [str(c) for c in table.get_row(row_key)]
                for row_key in table.rows
            ]
            oauth_rows = [row for row in rows if "OAUTH" in row]
            assert len(oauth_rows) == 2


class TestAuthManagerLogout:
    async def test_logout_clears_tokens_and_refreshes(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        """On success the store is cleared and the table is re-populated
        from scratch — note that the ``✓ All OAuth tokens cleared`` status
        is immediately overwritten by ``_populate_table``'s hint line, which
        is a pre-existing quirk of the TUI."""
        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            app.action_logout()
            await pilot.pause(0.05)
            assert fake_store._cache.cleared
            # server list was rebuilt by _populate_table → same names,
            # fresh ordering
            assert set(app._server_names) == {"atlassian", "github", "local"}

    async def test_logout_reports_error(
        self,
        auth_config: Path,
        fake_store: FakeTokenStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom() -> None:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(config_mod, "clear_tokens", boom)
        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            app.action_logout()
            await pilot.pause(0.05)
            status = str(app.query_one("#status").render())  # type: ignore[union-attr]
            assert "failed" in status.lower()
            assert "kaboom" in status


class TestAuthManagerLogin:
    async def test_login_for_non_oauth_server_is_noop(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            # move cursor to the ``local`` row (non-oauth)
            from textual.widgets import DataTable

            table = app.query_one(DataTable)
            local_idx = app._server_names.index("local")
            table.move_cursor(row=local_idx)
            await pilot.pause(0.02)

            await app.action_login()
            status = str(app.query_one("#status").render())  # type: ignore[union-attr]
            assert "does not use oauth" in status.lower()

    async def test_login_success_refreshes_table(
        self,
        auth_config: Path,
        fake_store: FakeTokenStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On success the worker calls ``probe_server`` with the selected
        server's raw config and refreshes the table."""
        probe_calls: list[tuple[str, dict]] = []

        async def fake_probe(name: str, raw: dict) -> list[dict]:
            probe_calls.append((name, raw))
            return [{"name": "t1", "description": ""}, {"name": "t2", "description": ""}]

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)

        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            from textual.widgets import DataTable

            table = app.query_one(DataTable)
            atl_idx = app._server_names.index("atlassian")
            table.move_cursor(row=atl_idx)
            await pilot.pause(0.02)

            await app.action_login()
            # wait for the background worker to finish
            for _ in range(40):
                if probe_calls:
                    break
                await pilot.pause(0.05)
            # extra tick to let the post-probe table refresh complete
            await pilot.pause(0.05)

        assert len(probe_calls) == 1
        assert probe_calls[0][0] == "atlassian"
        assert probe_calls[0][1]["auth"] == "oauth"
        # table was rebuilt after success
        assert set(app._server_names) == {"atlassian", "github", "local"}

    async def test_login_failure_reports_error(
        self,
        auth_config: Path,
        fake_store: FakeTokenStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_probe(name: str, raw: dict) -> list[dict]:
            raise RuntimeError("auth denied")

        monkeypatch.setattr(probe_mod, "probe_server", fake_probe)

        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            from textual.widgets import DataTable

            table = app.query_one(DataTable)
            atl_idx = app._server_names.index("atlassian")
            table.move_cursor(row=atl_idx)
            await pilot.pause(0.02)

            await app.action_login()
            for _ in range(40):
                status = str(app.query_one("#status").render())  # type: ignore[union-attr]
                if "Auth failed" in status:
                    break
                await pilot.pause(0.05)
            status = str(app.query_one("#status").render())  # type: ignore[union-attr]
            assert "Auth failed" in status
            assert "auth denied" in status

    async def test_login_without_selection_is_noop(
        self,
        auth_config: Path,
        fake_store: FakeTokenStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # empty config → no rows → ``_selected_server`` returns None
        empty = auth_config.parent / "empty.json"
        empty.write_text(json.dumps({"mcpServers": {}}))
        app = AuthManagerApp(empty)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            # should just return, no crash
            await app.action_login()


class TestAuthManagerQuit:
    async def test_quit_exits_cleanly(
        self, auth_config: Path, fake_store: FakeTokenStorage
    ) -> None:
        app = AuthManagerApp(auth_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            app.action_quit()
            await pilot.pause(0.05)
            assert not app.is_running
