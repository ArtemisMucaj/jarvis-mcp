"""Textual TUI apps for jarvis management.

Commands
--------
  jarvis mcp    – manage enabled/disabled servers and tools
  jarvis auth   – manage OAuth authentication for proxied MCPs
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static, Tree
from textual import work


def load_config(config_path: Path) -> tuple[dict[str, Any], str | None]:
    """Read and parse the config file. Returns (raw_config, error_message)."""
    try:
        return json.loads(config_path.read_text()), None
    except FileNotFoundError:
        return {"mcpServers": {}}, None
    except json.JSONDecodeError as exc:
        return {"mcpServers": {}}, f"Config parse error: {exc}"


# ── MCP Manager ───────────────────────────────────────────────────────────────


class MCPManagerApp(App[None]):
    """Browse and toggle MCP servers and their tools.

    Servers and tools shown with [✓]/[ ] toggle state.  Changes are written back
    to the config file on quit.  Tool lists are probed from live servers in the
    background after the tree is first populated from the config.
    """

    TITLE = "Jarvis Manager"
    CSS = """
    Screen {
        layout: vertical;
        background: $background;
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
        Binding("space", "toggle_item", "Toggle", priority=True),
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
        self.raw_config, err = load_config(self.config_path)
        if err:
            self._set_status(err)
        self._populate_tree()
        self._probe_all()

    # ── Config I/O ────────────────────────────────────────────────────────────

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

            # Omit "enabled: true" to keep config minimal since true is the default
            if d["enabled"]:
                servers[name].pop("enabled", None)
            else:
                servers[name]["enabled"] = False

            if d.get("probed_tools") and name in self._disabled_tools_cache:
                disabled_set = self._disabled_tools_cache[name]
                disabled = sorted(disabled_set)
                if disabled:
                    servers[name]["disabledTools"] = disabled
                else:
                    servers[name].pop("disabledTools", None)

        self.config_path.write_text(json.dumps(self.raw_config, indent=2))

    # ── Tree ──────────────────────────────────────────────────────────────────

    def _populate_tree(self) -> None:
        tree = self.query_one(Tree)
        servers: dict = self.raw_config.get("mcpServers", {})

        self._disabled_tools_cache: dict[str, set[str]] = {}

        for name, srv in sorted(servers.items()):
            enabled = srv.get("enabled", True) is not False
            mark = "[✓]" if enabled else "[ ]"
            disabled_tools = set(srv.get("disabledTools", []))
            self._disabled_tools_cache[name] = disabled_tools
            node = tree.root.add(
                f"{mark} {name}",
                data={
                    "type": "server",
                    "name": name,
                    "enabled": enabled,
                    "disabled_tools": disabled_tools,
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

            for child in list(node.children):
                child.remove()

            disabled = self._disabled_tools_cache.get(server_name, set())
            for tool in tools:
                t_name = tool["name"]
                t_enabled = t_name not in disabled
                mark = "  [✓]" if t_enabled else "  [ ]"
                node.add_leaf(
                    f"{mark} {t_name}",
                    data={
                        "type": "tool",
                        "name": t_name,
                        "server": server_name,
                        "enabled": t_enabled,
                        "disabled_tools": disabled,
                    },
                )

            if tools and node.data.get("enabled", True):
                node.expand()
            break

    # ── Background probing ────────────────────────────────────────────────────

    @work
    async def _probe_all(self) -> None:
        """Probe all enabled servers in the background (runs in app event loop)."""
        from jarvis.probe import probe_server

        tree = self.query_one(Tree)
        servers_config = self.raw_config.get("mcpServers", {})
        raw_servers = {
            d["name"]: servers_config[d["name"]]
            for node in tree.root.children
            if (d := node.data)
            and d.get("type") == "server"
            and d.get("enabled", True)
            and d["name"] in servers_config
        }

        total = len(raw_servers)
        if total == 0:
            self._set_status("No enabled servers.")
            return

        done = 0

        async def probe_one(name: str, raw: dict) -> None:
            nonlocal done
            try:
                tools = await asyncio.wait_for(probe_server(name, raw), timeout=30)
            except (SystemExit, KeyboardInterrupt, GeneratorExit):
                raise
            except BaseException:
                tools = []
            if not self.is_running:
                return
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
            mark = "[✓]" if d["enabled"] else "[ ]"
            cursor.label = f"{mark} {d['name']}"

        elif d.get("type") == "tool":
            parent = cursor.parent
            if parent and parent.data and not parent.data.get("enabled", True):
                self._set_status("Enable the server first before toggling its tools.")
                return
            d["enabled"] = not d["enabled"]
            mark = "  [✓]" if d["enabled"] else "  [ ]"
            cursor.label = f"{mark} {d['name']}"
            server_name = d.get("server")
            tool_name = d["name"]
            if server_name and server_name in self._disabled_tools_cache:
                disabled = self._disabled_tools_cache[server_name]
                if d["enabled"]:
                    disabled.discard(tool_name)
                else:
                    disabled.add(tool_name)

    def action_quit_save(self) -> None:
        self._save_config()
        self.exit()

    def action_refresh(self) -> None:
        """Re-probe all servers and refresh the tree."""
        tree = self.query_one(Tree)
        for node in tree.root.children:
            d = node.data
            if d and d.get("type") == "server" and d.get("enabled", True):
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
    user can trigger a login flow (opens the browser) or clear all cached
    tokens.
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
        self.raw_config, _ = load_config(self.config_path)
        self._populate_table()

    # ── Table ─────────────────────────────────────────────────────────────────

    def _populate_table(self) -> None:
        table = self.query_one(DataTable)
        if not table.columns:
            table.add_columns("Server", "Auth Type", "Token Files")

        servers: dict = self.raw_config.get("mcpServers", {})
        self._server_names = sorted(servers.keys())

        from jarvis.config import token_storage

        all_keys = list(token_storage._cache.iterkeys())

        for name in self._server_names:
            srv = servers[name]
            auth = srv.get("auth", "")
            auth_label = auth.upper() if auth else "—"
            if auth == "oauth":
                url = srv.get("url", "")
                count = sum(1 for k in all_keys if url and url in k)
                status = f"{count} token(s) cached" if count else "none cached"
            else:
                status = "N/A"
            table.add_row(name, auth_label, status, key=name)

        self._set_status("[l] Login (OAuth)  [x] Clear all tokens  [q] Quit")

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

        self._set_status(
            f"Starting OAuth for '{server}'… complete the flow in your browser."
        )

        from jarvis.probe import probe_server

        async def do_login() -> None:
            try:
                tools = await probe_server(server, srv_config)
                self._set_status(
                    f"✓ Authenticated '{server}' — {len(tools)} tool(s) available."
                )
                table = self.query_one(DataTable)
                table.clear(columns=False)
                self._server_names = []
                self._populate_table()
            except Exception as exc:
                self._set_status(f"✗ Auth failed for '{server}': {exc}")

        self.run_worker(do_login())

    def action_logout(self) -> None:
        """Wipe all cached OAuth tokens."""
        from jarvis.config import clear_tokens

        try:
            clear_tokens()
        except Exception as exc:
            self._set_status(f"✗ Failed to clear tokens: {exc}")
            return

        self._set_status("✓ All OAuth tokens cleared.")
        table = self.query_one(DataTable)
        table.clear(columns=False)
        self._server_names = []
        self._populate_table()

    def action_quit(self) -> None:
        self.exit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
