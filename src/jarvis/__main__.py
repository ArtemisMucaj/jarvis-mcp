# Inject the system trust store (macOS Keychain, Windows cert store, etc.)
# so that corporate proxies like Zscaler work out of the box.
# This MUST run before any other imports that might create SSL contexts.
import sys

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import datetime
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp.mcp_config import MCPConfig
from fastmcp.server import FastMCP
from jarvis.proxy import build_proxy
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server.transforms.search import BM25SearchTransform

from jarvis.config import (
    active_config_from_presets,
    configure_servers,
    get_disabled_tools,
    load_raw_config,
)
from jarvis.middleware import AuthErrorMiddleware
from jarvis.api import start_api_thread

# ── Logging setup ─────────────────────────────────────────────────────────────
# All output goes to stderr. The macOS app captures stderr → ~/.jarvis/jarvis.log.
# JSON is the default format so both Jarvis and FastMCP emit structured logs.

log = logging.getLogger("jarvis")
log.setLevel(logging.INFO)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        ts = (
            datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )
        data: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, separators=(",", ":"))


root = logging.getLogger()
if not root.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


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
                print(
                    f"Error: config file not found: {sys.argv[i + 1]}",
                    file=sys.stderr,
                )
                sys.exit(1)
            skip_next = True
        else:
            print("Error: --config requires a path argument", file=sys.stderr)
            sys.exit(1)
    else:
        filtered_argv.append(arg)

# Derive subcommand from filtered argv (first non-flag token)
subcmd = next((arg for arg in filtered_argv if not arg.startswith("-")), None)

if subcmd == "help" or "--help" in filtered_argv or "-h" in filtered_argv:
    print(
        "Usage: jarvis [--config PATH] [COMMAND] [OPTIONS]\n"
        "\n"
        "Commands:\n"
        "  mcp               Browse and toggle MCP servers and tools (TUI)\n"
        "  auth              Manage OAuth authentication for MCP servers (TUI)\n"
        "\n"
        "Options:\n"
        "  --config PATH     Use a specific config file\n"
        "  --http PORT       Run as an HTTP server on PORT (management UI)\n"
        "  --code-mode       Enable code mode transform\n"
        "  --help, -h        Show this message and exit\n"
        "\n"
        "With no command or options, runs as a stdio MCP server.\n"
        "\n"
        "HTTP mode:\n"
        "  MCP endpoint:     http://HOST:PORT/mcp\n"
        "  Management API:   http://HOST:(PORT+1)/api/...\n"
        "  Preset activation, server toggles, and tool toggles hot-swap the\n"
        "  active config live \u2014 connected clients keep their sessions."
    )
    sys.exit(0)

if subcmd == "mcp":
    from jarvis.tui import MCPManagerApp

    MCPManagerApp(config_path).run()
    sys.exit(0)

if subcmd == "auth":
    from jarvis.tui import AuthManagerApp

    AuthManagerApp(config_path).run()
    sys.exit(0)

code_mode = "--code-mode" in sys.argv


def build_mcp(cfg_path: Path, name: str) -> FastMCP:
    """Load *cfg_path* and return a fully configured FastMCP proxy."""
    mcp_dict, raw_servers = load_raw_config(cfg_path)
    disabled = get_disabled_tools(cfg_path)
    cfg = MCPConfig.model_validate(mcp_dict)
    configure_servers(cfg)

    server_names = list(cfg.mcpServers.keys())
    log.info(
        "Loading config %s — %d server(s): %s",
        cfg_path,
        len(server_names),
        ", ".join(server_names) or "(none)",
    )

    m = build_proxy(cfg, name)
    if disabled:
        log.info("Disabled tools: %s", ", ".join(sorted(disabled)))
        m.disable(names=disabled)
    m.add_middleware(AuthErrorMiddleware(raw_servers))
    m.add_transform(CodeMode() if code_mode else BM25SearchTransform(max_results=5))
    return m


if "--http" in sys.argv:
    idx = sys.argv.index("--http")
    port_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
    if not port_arg.isdigit():
        print("Error: --http requires a port number (1..65534)", file=sys.stderr)
        sys.exit(1)
    parsed_port = int(port_arg)
    if not (1 <= parsed_port <= 65534):
        print(
            f"Error: port must be between 1 and 65534, got {parsed_port}",
            file=sys.stderr,
        )
        sys.exit(1)
    port = parsed_port

    from fastmcp.server.http import RequestContextMiddleware, StreamableHTTPASGIApp
    from fastmcp.server.providers.fastmcp_provider import FastMCPProvider
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route
    import uvicorn

    # ── Build the proxy ───────────────────────────────────────────────────────
    # The outer shell is a thin FastMCP that hosts a single FastMCPProvider
    # wrapping the real inner proxy. Swapping ``swappable_provider.server``
    # instantly changes which config's tools are visible to all connected
    # sessions — no session disruption, no reconnect required.

    initial_inner = build_mcp(config_path, "jarvis-proxy")
    log.info("Starting HTTP mode — MCP on :%d, API on :%d", port, port + 1)
    outer_mcp = FastMCP("jarvis")
    swappable_provider = FastMCPProvider(initial_inner)
    outer_mcp.add_provider(swappable_provider)
    asgi_app = StreamableHTTPASGIApp(None)
    session_tasks: list[asyncio.Task] = []

    async def launch_session_manager(app: StreamableHTTPASGIApp, mcp: FastMCP) -> None:
        """Start *mcp*'s session manager, wire it to *app*, wait until ready."""
        ready: asyncio.Future = asyncio.get_event_loop().create_future()

        async def run() -> None:
            session_mgr = StreamableHTTPSessionManager(
                app=mcp._mcp_server,
                json_response=False,
                stateless=False,
            )
            app.session_manager = session_mgr
            async with mcp._lifespan_manager(), session_mgr.run():
                if not ready.done():
                    ready.set_result(None)
                # Stay alive until the task is cancelled on shutdown.
                await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(run())
        session_tasks.append(task)
        await asyncio.shield(ready)

    @asynccontextmanager
    async def lifespan(app):
        await launch_session_manager(asgi_app, outer_mcp)
        try:
            yield
        finally:
            for task in session_tasks:
                task.cancel()
            if session_tasks:
                await asyncio.gather(*session_tasks, return_exceptions=True)

    parent_app = Starlette(
        routes=[
            Route("/mcp", endpoint=asgi_app, methods=["GET", "POST", "DELETE"]),
        ],
        middleware=[Middleware(RequestContextMiddleware)],
        lifespan=lifespan,
    )
    parent_app.state.transport_type = "streamable-http"

    async def run_http() -> None:
        loop = asyncio.get_event_loop()

        async def broadcast_tools_changed() -> None:
            """Send tools/list_changed to every active MCP session on /mcp."""
            from mcp.shared.message import SessionMessage
            from mcp.types import JSONRPCMessage, JSONRPCNotification

            session_mgr = asgi_app.session_manager
            if session_mgr is None:
                return
            notif = JSONRPCNotification(
                jsonrpc="2.0", method="notifications/tools/list_changed"
            )
            session_msg = SessionMessage(message=JSONRPCMessage(notif))
            sends = [
                stream.send(session_msg)
                for transport in list(session_mgr._server_instances.values())
                if (stream := getattr(transport, "_write_stream", None)) is not None
            ]
            if sends:
                await asyncio.gather(*sends, return_exceptions=True)

        async def on_config_reload() -> None:
            """Rebuild the inner proxy from the active config and swap it in.

            Used for any change that alters the set of backend servers or
            requires reloading from disk (preset activation, server toggle,
            config PUT, etc.). Subprocess backends are restarted as a result
            — unavoidable when the server set changes.
            """
            new_cfg = active_config_from_presets()
            try:
                new_inner = build_mcp(new_cfg, "jarvis-proxy")
            except Exception as exc:
                log.error("Config reload failed: %s", exc)
                return
            swappable_provider.server = new_inner
            log.info("Config reloaded from %s", new_cfg)
            await broadcast_tools_changed()

        async def on_tool_toggle(server: str, tool: str, enabled: bool) -> None:
            """Enable or disable a single tool on the running inner proxy.

            Unlike ``on_config_reload``, this mutates the live proxy without
            rebuilding it — backend subprocesses keep running untouched.
            """
            inner = swappable_provider.server
            names = {f"{server}_{tool}"}
            if enabled:
                inner.enable(names=names)
                log.info("Enabled tool %s_%s", server, tool)
            else:
                inner.disable(names=names)
                log.info("Disabled tool %s_%s", server, tool)
            await broadcast_tools_changed()

        def config_reload_cb() -> None:
            asyncio.run_coroutine_threadsafe(on_config_reload(), loop)

        def tool_toggle_cb(server: str, tool: str, enabled: bool) -> None:
            asyncio.run_coroutine_threadsafe(
                on_tool_toggle(server, tool, enabled), loop
            )

        start_api_thread(
            port,
            port + 1,
            on_config_reload=config_reload_cb,
            on_tool_toggle=tool_toggle_cb,
        )
        cfg = uvicorn.Config(
            parent_app,
            host="127.0.0.1",
            port=port,
            timeout_graceful_shutdown=2,
            lifespan="on",
            ws="websockets-sansio",
            log_config=None,
        )
        await uvicorn.Server(cfg).serve()

    asyncio.run(run_http())

else:
    mcp = build_mcp(config_path, "jarvis")
    log.info("Starting stdio mode")
    mcp.run(show_banner=False)
