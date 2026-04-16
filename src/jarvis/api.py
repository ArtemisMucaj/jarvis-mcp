import asyncio
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from jarvis.config import (
    DATA_DIR,
    active_config_from_presets,
    load_presets,
    load_raw_config,
    save_presets,
)
from jarvis.probe import probe_all_servers


# ── Helpers ───────────────────────────────────────────────────────────────────

config_locks: dict[str, asyncio.Lock] = {}


def get_lock(path: Path) -> asyncio.Lock:
    return config_locks.setdefault(str(path.resolve()), asyncio.Lock())


def atomic_write(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* atomically via a temp file + os.replace."""
    content = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── REST API ──────────────────────────────────────────────────────────────────


def create_api_app(mcp_port: int, on_config_reload=None, on_tool_toggle=None):
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
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def resolve_config(request: Request, param: str = "config") -> Path:
        override = request.query_params.get(param)
        if not override:
            return active_config_from_presets()

        # Only allow files in the config directory (prevents path traversal)
        try:
            resolved = Path(override).resolve()
            if resolved.parent == DATA_DIR and resolved.suffix == ".json":
                return resolved
        except Exception:
            pass

        raise HTTPException(status_code=400, detail="invalid config")

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "mcp_port": mcp_port, "api_port": mcp_port + 1}
        )

    async def get_tools(request: Request) -> JSONResponse:
        # Always probe each backend directly so the management UI gets the real
        # per-server tool lists.  Going through the running proxy's list_tools()
        # would return only the 2 synthetic BM25 tools, not the individual tools
        # needed for the enable/disable UI.
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
            body = await request.json()
            async with get_lock(config_path):
                atomic_write(config_path, body)
            if on_config_reload is not None:
                on_config_reload()
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def toggle_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        config_path = resolve_config(request, param="path")
        try:
            enabled = (await request.json()).get("enabled", True)
            async with get_lock(config_path):
                raw = json.loads(config_path.read_text())
                servers = raw.get("mcpServers", {})
                if name not in servers:
                    return JSONResponse(
                        {"error": f"Server '{name}' not found"}, status_code=404
                    )
                if enabled:
                    servers[name].pop("enabled", None)
                else:
                    servers[name]["enabled"] = False
                atomic_write(config_path, raw)
            if on_config_reload is not None:
                on_config_reload()
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def toggle_tool(request: Request) -> JSONResponse:
        config_path = resolve_config(request, param="path")
        try:
            body = await request.json()
            server_name, tool_name = body["server"], body["tool"]
            enabled = body.get("enabled", True)
            async with get_lock(config_path):
                raw = json.loads(config_path.read_text())
                servers = raw.get("mcpServers", {})
                if server_name not in servers:
                    return JSONResponse(
                        {"error": f"Server '{server_name}' not found"}, status_code=404
                    )
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
                atomic_write(config_path, raw)
            if on_tool_toggle is not None:
                on_tool_toggle(server_name, tool_name, enabled)
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── Preset endpoints ──────────────────────────────────────────────────────

    async def list_presets(request: Request) -> JSONResponse:
        data = load_presets()
        return JSONResponse(
            {
                **data,
                "activeConfigPath": str(active_config_from_presets()),
            }
        )

    async def create_preset(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            preset_id = str(uuid.uuid4())
            preset = {
                "id": preset_id,
                "name": body["name"],
                "filePath": body["filePath"],
            }
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
                    if (
                        on_config_reload is not None
                        and data.get("activePresetID") == preset_id
                        and "filePath" in body
                    ):
                        on_config_reload()
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
        was_active = data.get("activePresetID") == preset_id
        if was_active:
            data["activePresetID"] = None
        save_presets(data)
        if was_active and on_config_reload is not None:
            on_config_reload()
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
        if on_config_reload is not None:
            on_config_reload()
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


def start_api_thread(
    mcp_port: int,
    api_port: int,
    on_config_reload=None,
    on_tool_toggle=None,
) -> None:
    """Start the REST API server in a daemon thread alongside the MCP server."""
    import uvicorn

    app = create_api_app(
        mcp_port,
        on_config_reload=on_config_reload,
        on_tool_toggle=on_tool_toggle,
    )
    threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": app,
            "host": "127.0.0.1",
            "port": api_port,
            "log_level": "error",
        },
        daemon=True,
    ).start()
