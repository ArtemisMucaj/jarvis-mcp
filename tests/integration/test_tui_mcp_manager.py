"""Integration tests for ``MCPManagerApp`` (the ``jarvis mcp`` TUI).

Drives the Textual app via ``run_test`` / ``Pilot``.  ``probe_server`` is
stubbed so the app never opens sockets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import probe as probe_mod
from jarvis.tui import MCPManagerApp


async def _await_probe(pilot, attempts: int = 40) -> None:
    """Wait until the background worker has replaced every ``probing…``
    placeholder with actual tool nodes.

    Raises ``AssertionError`` (with the list of servers that are still
    probing) if the worker hasn't finished within ``attempts`` ticks, so
    tests fail fast and loudly instead of silently passing with a half-
    populated tree.
    """
    app = pilot.app
    tree = app.query_one("Tree")
    still_probing: list[str] = []
    for _ in range(attempts):
        still_probing = []
        for node in tree.root.children:
            d = node.data
            if d and d.get("type") == "server" and d.get("enabled", True):
                labels = [str(c.label) for c in node.children]
                if any("probing" in lbl for lbl in labels):
                    still_probing.append(d["name"])
        if not still_probing:
            return
        await pilot.pause(0.05)
    raise AssertionError(
        f"_await_probe timed out after {attempts} attempts "
        f"(~{attempts * 0.05:.2f}s); still probing: {still_probing}"
    )


@pytest.fixture
def stub_probe(monkeypatch: pytest.MonkeyPatch):
    """Return deterministic probe results for any server name."""

    async def fake_probe(name: str, raw: dict) -> list[dict]:
        return [
            {"name": "tool_a", "description": "first tool"},
            {"name": "tool_b", "description": "second tool"},
        ]

    monkeypatch.setattr(probe_mod, "probe_server", fake_probe)
    # The TUI imports ``probe_server`` lazily via
    # ``from jarvis.probe import probe_server`` inside the worker, so
    # patching the module attribute is sufficient.
    return fake_probe


@pytest.fixture
def mcp_config(data_dir: Path) -> Path:
    path = data_dir / "servers.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"url": "http://alpha", "transport": "http"},
                    "beta": {
                        "url": "http://beta",
                        "transport": "http",
                        "disabledTools": ["tool_b"],
                    },
                    "gamma": {
                        "url": "http://gamma",
                        "transport": "http",
                        "enabled": False,
                    },
                }
            },
            indent=2,
        )
    )
    return path


class TestMCPManagerLoad:
    async def test_populates_tree_from_config(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            tree = app.query_one("Tree")
            server_names = {
                n.data["name"]
                for n in tree.root.children
                if n.data and n.data.get("type") == "server"
            }
            assert server_names == {"alpha", "beta", "gamma"}

    async def test_initial_enabled_state_reflects_config(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            tree = app.query_one("Tree")
            enabled_map = {
                n.data["name"]: n.data["enabled"]
                for n in tree.root.children
                if n.data and n.data.get("type") == "server"
            }
            assert enabled_map == {"alpha": True, "beta": True, "gamma": False}

    async def test_probing_replaces_placeholder_with_tools(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)
            tree = app.query_one("Tree")
            alpha_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "alpha"
            )
            tool_labels = [str(c.label) for c in alpha_node.children]
            assert any("tool_a" in lbl for lbl in tool_labels)
            assert any("tool_b" in lbl for lbl in tool_labels)

    async def test_disabled_server_shows_hint(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            tree = app.query_one("Tree")
            gamma_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "gamma"
            )
            labels = [str(c.label) for c in gamma_node.children]
            assert any("server disabled" in lbl for lbl in labels)


class TestMCPManagerToggleAndSave:
    async def test_toggle_server_disables_and_saves(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            # move cursor to the alpha node (first child of root)
            alpha_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "alpha"
            )
            tree.select_node(alpha_node)
            await pilot.pause(0.02)

            app.action_toggle_item()
            await pilot.pause(0.02)
            assert alpha_node.data["enabled"] is False

            app.action_quit_save()
            await pilot.pause(0.05)

        saved = json.loads(mcp_config.read_text())
        assert saved["mcpServers"]["alpha"]["enabled"] is False

    async def test_quit_save_without_changes_preserves_config(
        self, mcp_config: Path, stub_probe
    ) -> None:
        before = json.loads(mcp_config.read_text())
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)
            app.action_quit_save()
            await pilot.pause(0.05)

        after = json.loads(mcp_config.read_text())
        # enabled-true servers should still not carry an explicit "enabled"
        assert after["mcpServers"]["alpha"] == before["mcpServers"]["alpha"]

    async def test_toggle_tool_updates_disabled_set(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            alpha_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "alpha"
            )
            # first tool under alpha → tool_a (enabled)
            tool_node = alpha_node.children[0]
            tree.select_node(tool_node)
            await pilot.pause(0.02)

            app.action_toggle_item()
            await pilot.pause(0.02)
            assert tool_node.data["enabled"] is False
            assert "tool_a" in app._disabled_tools_cache["alpha"]

            app.action_quit_save()
            await pilot.pause(0.05)

        saved = json.loads(mcp_config.read_text())
        assert saved["mcpServers"]["alpha"]["disabledTools"] == ["tool_a"]

    async def test_cannot_toggle_tool_under_disabled_server(
        self, mcp_config: Path, stub_probe
    ) -> None:
        """beta starts enabled; disable it then try to toggle one of its tools."""
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            beta_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "beta"
            )
            # disable the server
            tree.select_node(beta_node)
            await pilot.pause(0.02)
            app.action_toggle_item()  # beta now disabled
            await pilot.pause(0.02)

            # cursor still on beta; attempt to toggle a tool — nothing should
            # happen (and the status bar should warn).  The tools are still
            # children from the earlier probe.
            if beta_node.children:
                tool_node = beta_node.children[0]
                before = tool_node.data["enabled"]
                tree.select_node(tool_node)
                await pilot.pause(0.02)
                app.action_toggle_item()
                await pilot.pause(0.02)
                # state must be unchanged
                assert tool_node.data["enabled"] == before

    async def test_enabling_previously_disabled_server_removes_key(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            tree = app.query_one("Tree")
            gamma_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "gamma"
            )
            tree.select_node(gamma_node)
            await pilot.pause(0.02)
            app.action_toggle_item()
            await pilot.pause(0.02)
            app.action_quit_save()
            await pilot.pause(0.05)

        saved = json.loads(mcp_config.read_text())
        assert "enabled" not in saved["mcpServers"]["gamma"]


class TestMCPManagerRefresh:
    async def test_refresh_resets_probed_tools(
        self, mcp_config: Path, stub_probe
    ) -> None:
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)
            tree = app.query_one("Tree")
            alpha_node = next(
                n
                for n in tree.root.children
                if n.data and n.data.get("name") == "alpha"
            )
            assert alpha_node.data["probed_tools"]

            app.action_refresh()
            # action_refresh immediately wipes probed_tools and re-adds a
            # "probing…" placeholder; the worker then re-populates.
            assert alpha_node.data["probed_tools"] == []
            await _await_probe(pilot)
            assert alpha_node.data["probed_tools"]


class TestMCPManagerEdgeCases:
    async def test_toggle_with_no_cursor_is_noop(
        self, data_dir: Path, stub_probe
    ) -> None:
        """An empty tree has no cursor node — action_toggle_item must early-
        return instead of crashing."""
        empty = data_dir / "empty.json"
        empty.write_text(json.dumps({"mcpServers": {}}))
        app = MCPManagerApp(empty)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            # no cursor node at all
            app.action_toggle_item()

    async def test_tool_toggle_blocked_when_parent_disabled_sets_status(
        self, mcp_config: Path, stub_probe
    ) -> None:
        """Select a tool under a server, then disable the server, then select
        the tool again and attempt to toggle — the status bar should warn."""
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            alpha = next(
                n for n in tree.root.children if n.data and n.data.get("name") == "alpha"
            )
            tool = alpha.children[0]
            # disable alpha directly via its node data — simulates the user
            # having disabled it while the cursor sat on the tool
            alpha.data["enabled"] = False

            tree.select_node(tool)
            await pilot.pause(0.02)

            before = tool.data["enabled"]
            app.action_toggle_item()
            await pilot.pause(0.02)

            assert tool.data["enabled"] == before  # unchanged
            status = str(app.query_one("#status").render())
            assert "Enable the server first" in status

    async def test_toggle_tool_off_then_on_uses_discard_path(
        self, mcp_config: Path, stub_probe
    ) -> None:
        """Hits line 254 — ``disabled.discard(tool_name)`` when re-enabling
        a tool that *is* in the disabled set."""
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            alpha = next(
                n for n in tree.root.children if n.data and n.data.get("name") == "alpha"
            )
            tool = alpha.children[0]
            tree.select_node(tool)
            await pilot.pause(0.02)
            app.action_toggle_item()  # disable
            await pilot.pause(0.02)
            assert tool.data["name"] in app._disabled_tools_cache["alpha"]

            app.action_toggle_item()  # re-enable
            await pilot.pause(0.02)
            assert tool.data["name"] not in app._disabled_tools_cache["alpha"]

    async def test_save_skips_server_nodes_missing_from_config(
        self, mcp_config: Path, stub_probe
    ) -> None:
        """Hits line 101 — when a tree node references a server name that's
        been removed from ``raw_config`` under our feet, ``_save_config``
        must skip it without crashing."""
        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            # yank alpha out of the in-memory config
            del app.raw_config["mcpServers"]["alpha"]
            app.action_quit_save()
            await pilot.pause(0.05)

        saved = json.loads(mcp_config.read_text())
        assert "alpha" not in saved["mcpServers"]
        assert "beta" in saved["mcpServers"]

    async def test_probe_failure_yields_empty_tool_list(
        self,
        mcp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hits lines 209-214 — an exception in ``probe_server`` inside the
        background worker must downgrade to an empty tool list for that
        server without killing the app."""

        async def angry_probe(name: str, raw: dict) -> list[dict]:
            if name == "alpha":
                raise RuntimeError("nope")
            return [{"name": "only_beta_tool", "description": ""}]

        monkeypatch.setattr(probe_mod, "probe_server", angry_probe)

        app = MCPManagerApp(mcp_config)
        async with app.run_test() as pilot:
            await _await_probe(pilot)

            tree = app.query_one("Tree")
            alpha = next(
                n for n in tree.root.children if n.data and n.data.get("name") == "alpha"
            )
            # no tool children on alpha — probe failed
            assert list(alpha.children) == []
            # but beta got its tool
            beta = next(
                n for n in tree.root.children if n.data and n.data.get("name") == "beta"
            )
            beta_labels = [str(c.label) for c in beta.children]
            assert any("only_beta_tool" in lbl for lbl in beta_labels)


class TestLoadConfigInMCPManager:
    async def test_parse_error_does_not_crash_app(
        self, data_dir: Path, stub_probe
    ) -> None:
        """A malformed config must not crash ``MCPManagerApp`` on mount.

        The parse-error status set by ``on_mount`` is immediately overwritten
        by ``_populate_tree``'s "No servers configured." and then by
        ``_probe_all``'s "No enabled servers." (a known pre-existing quirk
        of the TUI), so the final observable status is the latter.
        """
        bad = data_dir / "bad.json"
        bad.write_text("{ not json")
        app = MCPManagerApp(bad)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            status = str(app.query_one("#status").render())  # type: ignore[union-attr]
            # the final status must be exactly one of the two terminal
            # messages emitted by _populate_tree / _probe_all
            assert status in (
                "No servers configured.",
                "No enabled servers.",
            ), f"unexpected status: {status!r}"
