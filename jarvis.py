import json
import logging
import os
import re
import sys
from pathlib import Path

from mcp import McpError
from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import MCPConfig
from fastmcp.server import create_proxy
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server.transforms.search import BM25SearchTransform
from key_value.aio.stores.disk import DiskStore


_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env_vars(value: str) -> str:
    """Replace ${VAR} placeholders with their os.environ values."""
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


class _SuppressMcpSessionWarning(logging.Filter):
    """Demote 'Failed to connect' warnings caused by McpError to DEBUG.

    Unexpected exceptions (non-McpError) are still shown at WARNING level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING and record.exc_info:
            if isinstance(record.exc_info[1], McpError):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True


logging.getLogger("fastmcp.client.transports.config").addFilter(
    _SuppressMcpSessionWarning()
)

# Persistent token storage — survives proxy restarts
TOKEN_DIR = Path.home() / ".jarvis"
token_storage = DiskStore(directory=str(TOKEN_DIR))

# Load base config: prefer ~/.jarvis/servers.json (shared with JarvisMCP.app),
# fall back to the bundled file next to this script (local dev).
# Filter disabled servers and strip the non-standard `enabled` field.
_config_path = Path.home() / ".jarvis" / "servers.json"
if not _config_path.exists():
    _config_path = Path(__file__).parent / "servers.json"
_raw = json.loads(_config_path.read_text())
_raw["mcpServers"] = {
    name: {k: v for k, v in srv.items() if k != "enabled"}
    for name, srv in _raw.get("mcpServers", {}).items()
    if srv.get("enabled", True) is not False
}
config = MCPConfig.model_validate(_raw)
for name, server in config.mcpServers.items():
    auth = getattr(server, "auth", None)
    if auth == "oauth":
        server.auth = OAuth(
            token_storage=token_storage,
            callback_port=9876,
            client_name="Jarvis MCP Proxy",
        )
    # Expand ${VAR} references in env values (e.g. ${GITLAB_TOKEN})
    env = getattr(server, "env", None)
    if env:
        server.env = {
            k: _expand_env_vars(v) if isinstance(v, str) else v for k, v in env.items()
        }


mcp = create_proxy(
    config,
    name="jarvis",
)

if "--code-mode" in sys.argv:
    mcp.add_transform(CodeMode())
else:
    mcp.add_transform(BM25SearchTransform(max_results=5))

if __name__ == "__main__":
    import asyncio

    if "--auth" in sys.argv:
        target = next((a for a in sys.argv[2:] if not a.startswith("-")), None)
        if target and target not in config.mcpServers:
            print(
                f"Unknown server '{target}'. Available: {', '.join(config.mcpServers)}"
            )
            sys.exit(1)

        async def auth():
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
        port = int(port_arg) if port_arg.isdigit() else 7070
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)
