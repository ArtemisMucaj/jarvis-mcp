import json
import logging
import os
import re
import sys
import threading
import uuid
from pathlib import Path

from mcp import McpError
from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import MCPConfig
from fastmcp.server import create_proxy
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server.transforms.search import BM25SearchTransform
from key_value.aio.stores.disk import DiskStore


# ── Constants ─────────────────────────────────────────────────────────────────

ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")
NON_STANDARD_KEYS = {"enabled", "disabledTools"}
TOKEN_DIR = Path.home() / ".jarvis"
PRESETS_PATH = TOKEN_DIR / "presets.json"
token_storage = DiskStore(directory=str(TOKEN_DIR))


class SuppressMcpSessionWarning(logging.Filter):
    """Demote 'Failed to connect' warnings caused by McpError to DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING and record.exc_info:
            if isinstance(record.exc_info[1], McpError):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True


logging.getLogger("fastmcp.client.transports.config").addFilter(
    SuppressMcpSessionWarning()
)


# ── Preset management ─────────────────────────────────────────────────────────

def load_presets() -> dict:
    """Load ~/.jarvis/presets.json; returns empty structure if absent."""
    try:
        return json.loads(PRESETS_PATH.read_text())
    except FileNotFoundError:
        return {"presets": [], "activePresetID": None}
    except Exception:
        return {"presets": [], "activePresetID": None}


def save_presets(data: dict) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_PATH.write_text(json.dumps(data, indent=2))


def active_config_from_presets() -> Path:
    """Return the config path for the active preset, or the default."""
    data = load_presets()
    active_id = data.get("activePresetID")
    if active_id:
        for p in data.get("presets", []):
            if p.get("id") == active_id:
                path = Path(p["filePath"])
                if path.exists():
                    return path
    default = TOKEN_DIR / "servers.json"
    return default if default.exists() else Path(__file__).parent / "servers.json"


# ── Config utilities ──────────────────────────────────────────────────────────

def expand_env_vars(value: str) -> str:
    """Replace ${VAR} placeholders with their os.environ values."""
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def configure_servers(cfg: MCPConfig) -> None:
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
                k: expand_env_vars(v) if isinstance(v, str) else v
                for k, v in env.items()
            }


def load_raw_config(config_path: Path) -> tuple[dict, dict]:
    """Load config from *config_path*.

    Returns:
        mcp_dict    – cleaned dict suitable for MCPConfig (non-standard keys and
                      disabled servers removed)
        raw_servers – same servers as plain dicts (for per-server probing)
    """
    raw = json.loads(config_path.read_text())
    cleaned = {
        name: {k: v for k, v in srv.items() if k not in NON_STANDARD_KEYS}
        for name, srv in raw.get("mcpServers", {}).items()
        if srv.get("enabled", True) is not False
    }
    return {**raw, "mcpServers": cleaned}, dict(cleaned)


def get_disabled_tools(config_path: Path) -> set[str]:
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

async def probe_server(name: str, raw: dict) -> list[dict[str, str]]:
    """Probe a single MCP server and return its tool list."""
    mini = MCPConfig.model_validate({"mcpServers": {name: raw}})
    configure_servers(mini)
    proxy = create_proxy(mini, name=f"probe_{name}")
    tools = await proxy.list_tools()
    prefix = f"{name}_"
    return [
        {"name": t.name.removeprefix(prefix), "description": t.description or ""}
        for t in tools
    ]


async def probe_all_servers(
    raw_servers: dict,
    timeout: float = 30,
) -> dict[str, list[dict[str, str]]]:
    """Probe all servers in parallel; failures produce empty lists."""
    import asyncio

    async def safe_probe(name: str, raw: dict) -> list[dict]:
        try:
            return await asyncio.wait_for(probe_server(name, raw), timeout=timeout)
        except Exception as exc:
            print(f"[{name}] probe failed: {exc}", file=sys.stderr)
            return []

    names = list(raw_servers.keys())
    results = await asyncio.gather(*(safe_probe(n, raw_servers[n]) for n in names))
    return dict(zip(names, results))


# ── REST API ──────────────────────────────────────────────────────────────────

def create_api_app(default_config_path: Path, mcp_port: int):
    """Build a Starlette REST API app that runs alongside the MCP server.

    Endpoints
    ---------
    GET  /api/health                       → server status
    GET  /api/tools[?config=PATH]          → probe servers, return tool catalogue
    GET  /api/config[?path=PATH]           → read servers.json
    PUT  /api/config[?path=PATH]           → overwrite servers.json
    POST /api/servers/{name}/toggle        → body {enabled: bool}
    POST /api/tools/toggle                 → body {server, tool, enabled: bool}
    GET  /api/presets                      → list presets + active
    POST /api/presets                      → create preset
    PATCH/DELETE /api/presets/{id}         → update / remove
    POST /api/presets/{id}/activate        → switch active preset
    POST /api/presets/default/activate     → revert to default
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def resolve_config(request: Request, param: str = "config") -> Path:
        from fastapi import HTTPException
        override = request.query_params.get(param)
        if not override:
            return default_config_path

        # Only allow the default config or preset files in the config directory
        override_path = Path(override)
        config_dir = TOKEN_DIR

        # Resolve to absolute path and check it's within config directory
        try:
            resolved = override_path.resolve()
            # Allow default config path
            if resolved == default_config_path.resolve():
                return resolved
            # Allow files in the config directory (no traversal)
            if resolved.parent == config_dir and resolved.suffix == ".json":
                return resolved
        except Exception:
            pass

        raise HTTPException(status_code=400, detail="invalid config")

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "mcp_port": mcp_port, "api_port": mcp_port + 1})

    async def get_tools(request: Request) -> JSONResponse:
        config_path = resolve_config(request)
        try:
            _, raw_servers = load_raw_config(config_path)
            return JSONResponse(await probe_all_servers(raw_servers))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def config_endpoint(request: Request) -> JSONResponse:
        config_path = resolve_config(request, param="path")
        if request.method == "GET":
            try:
                return JSONResponse(json.loads(config_path.read_text()))
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)
        try:
            config_path.write_text(json.dumps(await request.json(), indent=2))
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def toggle_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        config_path = resolve_config(request, param="path")
        try:
            enabled = (await request.json()).get("enabled", True)
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
        config_path = resolve_config(request, param="path")
        try:
            body = await request.json()
            server_name, tool_name = body["server"], body["tool"]
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

    # ── Preset endpoints ──────────────────────────────────────────────────────

    async def list_presets(request: Request) -> JSONResponse:
        data = load_presets()
        return JSONResponse({**data, "activeConfigPath": str(active_config_from_presets())})

    async def create_preset(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            preset_id = str(uuid.uuid4())
            preset = {"id": preset_id, "name": body["name"], "filePath": body["filePath"]}
            data = load_presets()
            data["presets"].append(preset)
            save_presets(data)
            return JSONResponse({"preset": preset}, status_code=201)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def update_preset(request: Request) -> JSONResponse:
        preset_id = request.path_params["id"]
        try:
            body = await request.json()
            data = load_presets()
            for p in data["presets"]:
                if p["id"] == preset_id:
                    p.update({k: body[k] for k in ("name", "filePath") if k in body})
                    save_presets(data)
                    return JSONResponse({"preset": p})
            return JSONResponse({"error": "Preset not found"}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def delete_preset(request: Request) -> JSONResponse:
        preset_id = request.path_params["id"]
        data = load_presets()
        before = len(data["presets"])
        data["presets"] = [p for p in data["presets"] if p["id"] != preset_id]
        if len(data["presets"]) == before:
            return JSONResponse({"error": "Preset not found"}, status_code=404)
        if data.get("activePresetID") == preset_id:
            data["activePresetID"] = None
        save_presets(data)
        return JSONResponse({"status": "ok"})

    async def activate_preset(request: Request) -> JSONResponse:
        preset_id = request.path_params.get("id")
        data = load_presets()
        if preset_id and preset_id != "default":
            if not any(p["id"] == preset_id for p in data["presets"]):
                return JSONResponse({"error": "Preset not found"}, status_code=404)
            data["activePresetID"] = preset_id
        else:
            data["activePresetID"] = None
        save_presets(data)
        return JSONResponse({"status": "ok", "activePresetID": data["activePresetID"]})

    return Starlette(
        routes=[
            Route("/api/health", health),
            Route("/api/tools", get_tools),
            Route("/api/config", config_endpoint, methods=["GET", "PUT"]),
            Route("/api/servers/{name}/toggle", toggle_server, methods=["POST"]),
            Route("/api/tools/toggle", toggle_tool, methods=["POST"]),
            Route("/api/presets", list_presets, methods=["GET"]),
            Route("/api/presets", create_preset, methods=["POST"]),
            Route("/api/presets/{id}", update_preset, methods=["PATCH"]),
            Route("/api/presets/{id}", delete_preset, methods=["DELETE"]),
            Route("/api/presets/{id}/activate", activate_preset, methods=["POST"]),
        ]
    )


def start_api_thread(config_path: Path, mcp_port: int, api_port: int) -> None:
    """Start the REST API server in a daemon thread alongside the MCP server."""
    import uvicorn

    app = create_api_app(config_path, mcp_port)
    threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": api_port, "log_level": "error"},
        daemon=True,
    ).start()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    # Priority: --config flag  >  active preset in presets.json  >  ~/.jarvis/servers.json
    config_path = active_config_from_presets()

    # Preprocess argv to extract --config and filter it out before deriving subcommand
    filtered_argv = []
    skip_next = False
    for i, arg in enumerate(sys.argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if arg == "--config":
            if i + 1 < len(sys.argv):
                override = Path(sys.argv[i + 1])
                if override.exists():
                    config_path = override
                else:
                    print(f"Error: config file not found: {sys.argv[i + 1]}", file=sys.stderr)
                    sys.exit(1)
                skip_next = True
            else:
                print("Error: --config requires a path argument", file=sys.stderr)
                sys.exit(1)
        else:
            filtered_argv.append(arg)

    # Derive subcommand from filtered argv (first non-flag token)
    subcmd = next((arg for arg in filtered_argv if not arg.startswith("-")), None)

    if subcmd == "mcp":
        from jarvis_tui import MCPManagerApp
        MCPManagerApp(config_path).run()
        sys.exit(0)

    if subcmd == "auth":
        from jarvis_tui import AuthManagerApp
        AuthManagerApp(config_path).run()
        sys.exit(0)

    if "--list-tools" in sys.argv:
        _, raw_servers = load_raw_config(config_path)

        async def discover() -> None:
            json.dump(await probe_all_servers(raw_servers), sys.stdout, indent=2)

        try:
            asyncio.run(discover())
        except KeyboardInterrupt:
            pass
        sys.exit(0)

    mcp_dict, _ = load_raw_config(config_path)
    disabled_tools = get_disabled_tools(config_path)
    code_mode = "--code-mode" in sys.argv

    config = MCPConfig.model_validate(mcp_dict)
    configure_servers(config)
    mcp = create_proxy(config, name="jarvis")

    if disabled_tools:
        mcp.disable(names=disabled_tools)

    if "--auth" in sys.argv:
        # Scan filtered_argv for auth target (first non-flag after --auth)
        auth_idx = next((i for i, arg in enumerate(filtered_argv) if arg == "--auth"), None)
        if auth_idx is not None:
            target = next((filtered_argv[i] for i in range(auth_idx + 1, len(filtered_argv)) if not filtered_argv[i].startswith("-")), None)
        else:
            target = None
        if target and target not in config.mcpServers:
            print(f"Unknown server '{target}'. Available: {', '.join(config.mcpServers)}")
            sys.exit(1)

        mcp.add_transform(BM25SearchTransform(max_results=5))

        async def auth() -> None:
            tools = await mcp.list_tools()
            print(f"Authenticated. {len(tools)} tools available:")
            for t in tools:
                print(f"  - {t.name}")

        try:
            asyncio.run(auth())
        except KeyboardInterrupt:
            print("\nAuth cancelled.")

    elif "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        # Validate port is < 65535 (need room for API port at port+1)
        if port_arg.isdigit():
            parsed_port = int(port_arg)
            port = parsed_port if parsed_port <= 65534 else 7070
        else:
            port = 7070

        mcp.add_transform(CodeMode() if code_mode else BM25SearchTransform(max_results=5))
        start_api_thread(config_path, port, port + 1)
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port, show_banner=False)

    else:
        mcp.add_transform(CodeMode() if code_mode else BM25SearchTransform(max_results=5))
        mcp.run(show_banner=False)