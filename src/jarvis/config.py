import json
import os
import re
from pathlib import Path

from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import MCPConfig
from key_value.aio.stores.disk import DiskStore


# ── Constants ─────────────────────────────────────────────────────────────────

ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")
NON_STANDARD_KEYS = {"enabled", "disabledTools"}
# Data directory is overridable via ``JARVIS_DATA_DIR`` so tests (and alternate
# deployments) can isolate their state from the user's real ``~/.jarvis``.
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR") or (Path.home() / ".jarvis"))
PRESETS_PATH = DATA_DIR / "presets.json"
token_storage = DiskStore(directory=str(DATA_DIR))


def clear_tokens() -> None:
    """Wipe all OAuth tokens from the diskcache store."""
    token_storage._cache.clear()


# ── Preset management ─────────────────────────────────────────────────────────


def load_presets() -> dict:
    """Load ~/.jarvis/presets.json; returns empty structure if absent."""
    try:
        return json.loads(PRESETS_PATH.read_text())
    except FileNotFoundError:
        return {"presets": [], "activePresetID": None}


def save_presets(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    default = DATA_DIR / "servers.json"
    if not default.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        default.write_text(json.dumps({"mcpServers": {}}, indent=2))
    return default


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
                client_name="Jarvis Proxy",
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
