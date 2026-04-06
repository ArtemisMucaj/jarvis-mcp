import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

from mcp import McpError
from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import MCPConfig
from fastmcp.server import create_proxy
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server.transforms.search import BM25SearchTransform
from key_value.aio.stores.disk import DiskStore


# ── Constants ─────────────────────────────────────────────────────────────────

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")
_NON_STANDARD_KEYS = {"enabled", "disabledTools"}
TOKEN_DIR = Path.home() / ".jarvis"
token_storage = DiskStore(directory=str(TOKEN_DIR))


class _SuppressMcpSessionWarning(logging.Filter):
    """Demote 'Failed to connect' warnings caused by McpError to DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING and record.exc_info:
            if isinstance(record.exc_info[1], McpError):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True


logging.getLogger("fastmcp.client.transports.config").addFilter(
    _SuppressMcpSessionWarning()
)


# ── Config utilities ──────────────────────────────────────────────────────────

def _expand_env_vars(value: str) -> str:
    """Replace ${VAR} placeholders with their os.environ values."""
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def _configure_servers(cfg: MCPConfig) -> None:
    """Apply OAuth auth and environment-variable expansion to every server."""
    for name, server in cfg.mcpServers.items():
        if getattr(server, "auth", None) == "oauth":
            server.auth = OAuth(
                token_storage=token_storage,
                callback_port=9876,
                client_name="Jarvis MCP Proxy",
            )
        env = getattr(server, "env", None)
        if env:
            server.env = {
                k: _expand_env_vars(v) if isinstance(v, str) else v
                for k, v in env.items()
            }


def _load_raw_config(config_path: Path) -> tuple[dict, dict]:
    """Load config from *config_path*.

    Returns:
        mcp_dict   – cleaned dict suitable for MCPConfig (non-standard keys and
                     disabled servers removed)
        raw_servers – same servers as plain dicts (for per-server probing)
    """
    raw = json.loads(config_path.read_text())
    cleaned = {
        name: {k: v for k, v in srv.items() if k not in _NON_STANDARD_KEYS}
        for name, srv in raw.get("mcpServers", {}).items()
        if srv.get("enabled", True) is not False
    }
    mcp_dict = {**raw, "mcpServers": cleaned}
    return mcp_dict, dict(cleaned)


def _get_disabled_tools(config_path: Path) -> set[str]:
    """Return the set of 'servername_toolname' entries for disabled tools."""
    raw = json.loads(config_path.read_text())
    disabled: set[str] = set()
    for name, srv in raw.get("mcpServers", {}).items():
        if srv.get("enabled", True) is False:
            continue
        for tool in srv.get("disabledTools", []):
            disabled.add(f"{name}_{tool}")
    return disabled


# ── Server probing ────────────────────────────────────────────────────────────

async def _probe_server(name: str, raw: dict) -> list[dict[str, str]]:
    """Probe a single MCP server and return its tool list."""
    mini = MCPConfig.model_validate({"mcpServers": {name: raw}})
    _configure_servers(mini)
    proxy = create_proxy(mini, name=f"_probe_{name}")
    tools = await proxy.list_tools()
    prefix = f"{name}_"
    return [
        {"name": t.name.removeprefix(prefix), "description": t.description or ""}
        for t in tools
    ]


async def _probe_all_servers(
    raw_servers: dict,
    timeout: float = 30,
) -> dict[str, list[dict[str, str]]]:
    """Probe all servers in parallel; failures produce empty lists."""
    import asyncio

    async def safe_probe(name: str, raw: dict) -> list[dict]:
        try:
            return await asyncio.wait_for(_probe_server(name, raw), timeout=timeout)
        except Exception as exc:
            print(f"[{name}] probe failed: {exc}", file=sys.stderr)
            return []

    names = list(raw_servers.keys())
    results = await asyncio.gather(*(safe_probe(n, raw_servers[n]) for n in names))
    return dict(zip(names, results))


# ── REST API ──────────────────────────────────────────────────────────────────

def _create_api_app(default_config_path: Path, mcp_port: int):
    """Build a Starlette REST API app that runs alongside the MCP server.

    Endpoints
    ---------
    GET  /api/health                       → server status
    GET  /api/tools[?config=PATH]          → probe servers, return tool catalogue
    GET  /api/config[?path=PATH]           → read servers.json
    PUT  /api/config[?path=PATH]           → overwrite servers.json
    POST /api/servers/{name}/toggle        → body {enabled: bool}
    POST /api/tools/toggle                 → body {server, tool, enabled: bool}
    """
    import asyncio as _asyncio

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def _resolve_config(request: Request, param: str = "config") -> Path:
        override = request.query_params.get(param)
        return Path(override) if override else default_config_path

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "mcp_port": mcp_port, "api_port": mcp_port + 1})

    async def get_tools(request: Request) -> JSONResponse:
        config_path = _resolve_config(request)
        try:
            _, raw_servers = _load_raw_config(config_path)
            result = await _probe_all_servers(raw_servers)
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def config_endpoint(request: Request) -> JSONResponse:
        config_path = _resolve_config(request, param="path")
        if request.method == "GET":
            try:
                return JSONResponse(json.loads(config_path.read_text()))
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)
        # PUT
        try:
            body = await request.json()
            config_path.write_text(json.dumps(body, indent=2))
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def toggle_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        config_path = _resolve_config(request, param="path")
        try:
            body = await request.json()
            enabled = body.get("enabled", True)
            raw = json.loads(config_path.read_text())
            servers = raw.get("mcpServers", {})
            if name not in servers:
                return JSONResponse({"error": f"Server '{name}' not found"}, status_code=404)
            if enabled:
                servers[name].pop("enabled", None)
            else:
                servers[name]["enabled"] = False
            config_path.write_text(json.dumps(raw, indent=2))
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def toggle_tool(request: Request) -> JSONResponse:
        config_path = _resolve_config(request, param="path")
        try:
            body = await request.json()
            server_name = body["server"]
            tool_name = body["tool"]
            enabled = body.get("enabled", True)
            raw = json.loads(config_path.read_text())
            servers = raw.get("mcpServers", {})
            if server_name not in servers:
                return JSONResponse({"error": f"Server '{server_name}' not found"}, status_code=404)
            srv = servers[server_name]
            disabled = srv.get("disabledTools", [])
            if enabled:
                disabled = [t for t in disabled if t != tool_name]
            elif tool_name not in disabled:
                disabled.append(tool_name)
            if disabled:
                srv["disabledTools"] = disabled
            else:
                srv.pop("disabledTools", None)
            config_path.write_text(json.dumps(raw, indent=2))
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    return Starlette(
        routes=[
            Route("/api/health", health),
            Route("/api/tools", get_tools),
            Route("/api/config", config_endpoint, methods=["GET", "PUT"]),
            Route("/api/servers/{name}/toggle", toggle_server, methods=["POST"]),
            Route("/api/tools/toggle", toggle_tool, methods=["POST"]),
        ]
    )


def _start_api_thread(config_path: Path, mcp_port: int, api_port: int) -> None:
    """Start the REST API server in a daemon thread alongside the MCP server."""
    import uvicorn

    app = _create_api_app(config_path, mcp_port)
    t = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": api_port, "log_level": "error"},
        daemon=True,
    )
    t.start()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    # ── Resolve config path (global --config flag applies to every mode) ──────
    _default_config = Path.home() / ".jarvis" / "servers.json"
    if not _default_config.exists():
        _default_config = Path(__file__).parent / "servers.json"

    if "--config" in sys.argv:
        _idx = sys.argv.index("--config")
        if _idx + 1 < len(sys.argv):
            _override = Path(sys.argv[_idx + 1])
            if _override.exists():
                _default_config = _override
            else:
                print(f"Error: config file not found: {sys.argv[_idx + 1]}", file=sys.stderr)
                sys.exit(1)

    # ── Positional subcommands (TUI modes, no MCP proxy needed) ──────────────
    _subcmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None

    if _subcmd == "mcp":
        from jarvis_tui import MCPManagerApp
        MCPManagerApp(_default_config).run()
        sys.exit(0)

    if _subcmd == "auth":
        from jarvis_tui import AuthManagerApp
        AuthManagerApp(_default_config).run()
        sys.exit(0)

    # ── --list-tools: probe only, no proxy ───────────────────────────────────
    if "--list-tools" in sys.argv:
        _, _raw_servers = _load_raw_config(_default_config)

        async def _discover() -> None:
            result = await _probe_all_servers(_raw_servers)
            json.dump(result, sys.stdout, indent=2)

        try:
            asyncio.run(_discover())
        except KeyboardInterrupt:
            pass
        sys.exit(0)

    # ── All remaining modes need the MCP proxy ────────────────────────────────
    _mcp_dict, _ = _load_raw_config(_default_config)
    _disabled_tools = _get_disabled_tools(_default_config)
    _is_code_mode = "--code-mode" in sys.argv

    config = MCPConfig.model_validate(_mcp_dict)
    _configure_servers(config)
    mcp = create_proxy(config, name="jarvis")

    if _disabled_tools:
        mcp.disable(names=_disabled_tools)

    # ── --auth: trigger OAuth login flow ─────────────────────────────────────
    if "--auth" in sys.argv:
        _target = next((a for a in sys.argv[2:] if not a.startswith("-")), None)
        if _target and _target not in config.mcpServers:
            print(f"Unknown server '{_target}'. Available: {', '.join(config.mcpServers)}")
            sys.exit(1)

        mcp.add_transform(BM25SearchTransform(max_results=5))

        async def _auth() -> None:
            tools = await mcp.list_tools()
            print(f"Authenticated. {len(tools)} tools available:")
            for t in tools:
                print(f"  - {t.name}")

        try:
            asyncio.run(_auth())
        except KeyboardInterrupt:
            print("\nAuth cancelled.")

    # ── --http: HTTP MCP server + REST API server ─────────────────────────────
    elif "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        port = int(port_arg) if port_arg.isdigit() else 7070
        api_port = port + 1

        if _is_code_mode:
            mcp.add_transform(CodeMode())
        else:
            mcp.add_transform(BM25SearchTransform(max_results=5))

        _start_api_thread(_default_config, port, api_port)
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port, show_banner=False)

    # ── stdio MCP server (default) ────────────────────────────────────────────
    else:
        if _is_code_mode:
            mcp.add_transform(CodeMode())
        else:
            mcp.add_transform(BM25SearchTransform(max_results=5))
        mcp.run(show_banner=False)
