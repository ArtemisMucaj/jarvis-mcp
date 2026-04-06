"""Textual TUI apps for jarvis-mcp management.

Commands
--------
  jarvis mcp    – manage enabled/disabled servers and tools
  jarvis auth   – manage OAuth authentication for proxied MCPs
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static, Tree
from textual import work


# ── MCP Manager ───────────────────────────────────────────────────────────────


class MCPManagerApp(App[None]):
    """Browse and toggle MCP servers and their tools.

    Servers and tools shown with ☑/☐ toggle state.  Changes are written back
    to the config file on quit.  Tool lists are probed from live servers in the
    background after the tree is first populated from the config.
    """

    TITLE = "Jarvis MCP Manager"
    CSS = """
    Screen {
        layout: vertical;
    }
    Tree {
        height: 1fr;
        border: solid $accent;
        margin: 0 1;
    }
    #status {
        height: 1;
        background: $boost;
        padding: 0 2;
        color: $text-muted;
    }
    """
    BINDINGS = [
        Binding("q", "quit_save", "Save & Quit"),
        Binding("space", "toggle_item", "Toggle"),
        Binding("r", "refresh", "Re-probe"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.raw_config: dict[str, Any] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("Servers & Tools")
        yield Static("Loading…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._load_config()
        self._populate_tree()
        self._probe_all()

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            self.raw_config = json.loads(self.config_path.read_text())
        except FileNotFoundError:
            self.raw_config = {"mcpServers": {}}
        except json.JSONDecodeError as exc:
            self.raw_config = {"mcpServers": {}}
            self._set_status(f"Config parse error: {exc}")

    def _save_config(self) -> None:
        """Persist toggle state back to the config file."""
        tree = self.query_one(Tree)
        servers: dict = self.raw_config.setdefault("mcpServers", {})

        for server_node in tree.root.children:
            d = server_node.data
            if not d or d.get("type") != "server":
                continue
            name: str = d["name"]
            if name not in servers:
                continue

            # Enabled state (omit key when True to keep config minimal)
            if d["enabled"]:
                servers[name].pop("enabled", None)
            else:
                servers[name]["enabled"] = False

            # Disabled tools (only saved when we have the probed tool list)
            if d.get("probed_tools"):
                disabled = [
                    child.data["name"]
                    for child in server_node.children
                    if child.data
                    and child.data.get("type") == "tool"
                    and not child.data.get("enabled", True)
                ]
                if disabled:
                    servers[name]["disabledTools"] = disabled
                else:
                    servers[name].pop("disabledTools", None)

        self.config_path.write_text(json.dumps(self.raw_config, indent=2))

    # ── Tree ──────────────────────────────────────────────────────────────────

    def _populate_tree(self) -> None:
        tree = self.query_one(Tree)
        servers: dict = self.raw_config.get("mcpServers", {})

        for name, srv in sorted(servers.items()):
            enabled = srv.get("enabled", True) is not False
            mark = "☑" if enabled else "☐"
            node = tree.root.add(
                f"{mark} {name}",
                data={
                    "type": "server",
                    "name": name,
                    "enabled": enabled,
                    "disabled_tools": set(srv.get("disabledTools", [])),
                    "probed_tools": [],
                },
            )
            node.allow_expand = True
            if enabled:
                node.add_leaf("  ⟳ probing…", data={"type": "hint"})
            else:
                node.add_leaf("  (server disabled)", data={"type": "hint"})

        tree.root.expand()
        n = len(servers)
        self._set_status(f"Probing {n} server(s)…" if n else "No servers configured.")

    def _update_server_tools(self, server_name: str, tools: list[dict]) -> None:
        """Replace a server's placeholder children with actual tool nodes."""
        tree = self.query_one(Tree)
        for node in tree.root.children:
            if not node.data or node.data.get("name") != server_name:
                continue
            node.data["probed_tools"] = [t["name"] for t in tools]

            # Remove all existing children (placeholders)
            for child in list(node.children):
                child.remove()

            disabled = node.data.get("disabled_tools", set())
            for tool in tools:
                t_name = tool["name"]
                t_enabled = t_name not in disabled
                mark = "  ☑" if t_enabled else "  ☐"
                node.add_leaf(
                    f"{mark} {t_name}",
                    data={
                        "type": "tool",
                        "name": t_name,
                        "server": server_name,
                        "enabled": t_enabled,
                    },
                )

            if tools and node.data.get("enabled", True):
                node.expand()
            break

    # ── Background probing ────────────────────────────────────────────────────

    @work
    async def _probe_all(self) -> None:
        """Probe all enabled servers in the background (runs in app event loop)."""
        from jarvis import _load_raw_config, _probe_server

        try:
            _, raw_servers = _load_raw_config(self.config_path)
        except Exception as exc:
            self._set_status(f"Config error: {exc}")
            return

        total = len(raw_servers)
        if total == 0:
            self._set_status("No enabled servers.")
            return

        done = 0

        async def probe_one(name: str, raw: dict) -> None:
            nonlocal done
            try:
                tools = await asyncio.wait_for(_probe_server(name, raw), timeout=30)
            except Exception:
                tools = []
            self._update_server_tools(name, tools)
            done += 1
            if done < total:
                self._set_status(f"Probing… {done}/{total} done")
            else:
                self._set_status(
                    "All servers probed.  [Space] toggle  [q] save & quit  [r] re-probe"
                )

        await asyncio.gather(*(probe_one(n, r) for n, r in raw_servers.items()))

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_toggle_item(self) -> None:
        tree = self.query_one(Tree)
        cursor = tree.cursor_node
        if not cursor or not cursor.data:
            return

        d = cursor.data

        if d.get("type") == "server":
            d["enabled"] = not d["enabled"]
            mark = "☑" if d["enabled"] else "☐"
            cursor.label = f"{mark} {d['name']}"

        elif d.get("type") == "tool":
            parent = cursor.parent
            if parent and parent.data and not parent.data.get("enabled", True):
                self._set_status("Enable the server first before toggling its tools.")
                return
            d["enabled"] = not d["enabled"]
            mark = "  ☑" if d["enabled"] else "  ☐"
            cursor.label = f"{mark} {d['name']}"

    def action_quit_save(self) -> None:
        self._save_config()
        self.exit()

    def action_refresh(self) -> None:
        """Re-probe all servers and refresh the tree."""
        tree = self.query_one(Tree)
        for node in tree.root.children:
            if node.data and node.data.get("enabled", True):
                for child in list(node.children):
                    child.remove()
                node.add_leaf("  ⟳ probing…", data={"type": "hint"})
                node.data["probed_tools"] = []
        self._set_status("Re-probing…")
        self._probe_all()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)


# ── Auth Manager ──────────────────────────────────────────────────────────────


class AuthManagerApp(App[None]):
    """Manage OAuth authentication for proxied MCP servers.

    Lists every configured server and its auth type.  For OAuth servers the
    user can trigger a login flow (opens the browser via the existing
    ``jarvis --auth SERVER`` flow) or clear all cached tokens.
    """

    TITLE = "Jarvis Auth Manager"
    CSS = """
    Screen {
        layout: vertical;
    }
    DataTable {
        height: 1fr;
        margin: 0 1;
    }
    #status {
        height: 1;
        background: $boost;
        padding: 0 2;
        color: $text-muted;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "login", "Login"),
        Binding("x", "logout", "Clear Tokens"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.raw_config: dict[str, Any] = {}
        self._server_names: list[str] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(zebra_stripes=True, cursor_type="row")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._load_config()
        self._populate_table()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            self.raw_config = json.loads(self.config_path.read_text())
        except Exception:
            self.raw_config = {"mcpServers": {}}

    # ── Table ─────────────────────────────────────────────────────────────────

    def _populate_table(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Server", "Auth Type", "Token Files")

        servers: dict = self.raw_config.get("mcpServers", {})
        self._server_names = sorted(servers.keys())

        token_count = self._count_token_files()

        for name in self._server_names:
            srv = servers[name]
            auth = srv.get("auth", "")
            auth_label = auth.upper() if auth else "—"
            if auth == "oauth":
                status = f"{token_count} file(s) in ~/.jarvis" if token_count else "none found"
            else:
                status = "N/A"
            table.add_row(name, auth_label, status, key=name)

        self._set_status("[l] Login (OAuth)  [x] Clear all tokens  [q] Quit")

    def _count_token_files(self) -> int:
        """Count non-config files in TOKEN_DIR (heuristic for token presence)."""
        token_dir = Path.home() / ".jarvis"
        excluded = {"servers.json", "jarvis.log"}
        try:
            return sum(
                1
                for f in token_dir.iterdir()
                if f.is_file() and f.name not in excluded and not f.name.endswith(".log")
            )
        except Exception:
            return 0

    # ── Actions ───────────────────────────────────────────────────────────────

    def _selected_server(self) -> str | None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._server_names):
            return self._server_names[row]
        return None

    async def action_login(self) -> None:
        server = self._selected_server()
        if not server:
            return

        srv_config = self.raw_config.get("mcpServers", {}).get(server, {})
        if srv_config.get("auth") != "oauth":
            self._set_status(f"'{server}' does not use OAuth — no login needed.")
            return

        jarvis_script = Path(__file__).with_name("jarvis.py")
        self._set_status(
            f"Starting OAuth for '{server}'… complete the flow in your browser."
        )

        # Suspend the TUI so the browser/callback interaction has a clean terminal
        async with self.suspend():
            result = subprocess.run(
                [sys.executable, str(jarvis_script), "--auth", server],
            )

        if result.returncode == 0:
            self._set_status(f"✓ Authenticated '{server}' successfully.")
        else:
            self._set_status(f"✗ Auth failed for '{server}' (exit {result.returncode}).")

    def action_logout(self) -> None:
        """Delete all non-config files from TOKEN_DIR (clears all OAuth tokens)."""
        token_dir = Path.home() / ".jarvis"
        excluded = {"servers.json", "jarvis.log"}
        cleared = 0
        errors = 0
        try:
            for f in token_dir.iterdir():
                if f.is_file() and f.name not in excluded and not f.name.endswith(".log"):
                    try:
                        f.unlink()
                        cleared += 1
                    except Exception:
                        errors += 1
        except Exception as exc:
            self._set_status(f"Error scanning token dir: {exc}")
            return

        if errors:
            self._set_status(f"Cleared {cleared} token file(s); {errors} could not be removed.")
        else:
            self._set_status(
                f"✓ Cleared {cleared} token file(s) from {token_dir}"
                if cleared
                else "No token files found."
            )

        # Refresh table to show updated counts
        table = self.query_one(DataTable)
        table.clear(columns=False)
        self._server_names = []
        self._populate_table()

    def action_quit(self) -> None:
        self.exit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
