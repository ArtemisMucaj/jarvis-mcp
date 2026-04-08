import asyncio
import sys
from pathlib import Path

from fastmcp.mcp_config import MCPConfig
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
        "With no command or options, runs as a stdio MCP server."
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

mcp_dict, raw_servers = load_raw_config(config_path)
disabled_tools = get_disabled_tools(config_path)
code_mode = "--code-mode" in sys.argv

config = MCPConfig.model_validate(mcp_dict)

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

    configure_servers(config)
    mcp = build_proxy(config, "jarvis")
    if disabled_tools:
        mcp.disable(names=disabled_tools)
    mcp.add_middleware(AuthErrorMiddleware(raw_servers))
    mcp.add_transform(CodeMode() if code_mode else BM25SearchTransform(max_results=5))

    async def _run_http() -> None:
        start_api_thread(port, port + 1)
        await mcp.run_async(
            transport="streamable-http",
            host="127.0.0.1",
            port=port,
            show_banner=False,
        )

    asyncio.run(_run_http())

else:
    configure_servers(config)
    mcp = build_proxy(config, "jarvis")
    if disabled_tools:
        mcp.disable(names=disabled_tools)
    mcp.add_middleware(AuthErrorMiddleware(raw_servers))
    mcp.add_transform(CodeMode() if code_mode else BM25SearchTransform(max_results=5))
    mcp.run(show_banner=False)
