# Jarvis

Your agent knows about 200 tools. It uses 5. The other 195 are just burning context on every single request.

Jarvis fixes that. It proxies all your MCP servers behind a single endpoint and exposes just **2 tools** to the agent — `search_tools` and `call_tool`. The agent describes what it wants in plain language, gets back the top matching tools with full schemas, and calls the right one. You can connect 10 servers and 300 tools; the agent still sees 2.

For agents that need to do more in fewer round-trips, Jarvis also ships **Code Mode**: instead of searching and calling tools one at a time, the agent writes a small sandboxed Python script that batches multiple tool calls in a single step. Less back-and-forth, more done per turn.

## Install

### macOS app (recommended)

Download `Jarvis-<version>.dmg` from the [latest release](https://github.com/ArtemisMucaj/jarvis-mcp/releases/latest), open it, and drag **Jarvis** to `/Applications`.

On first launch macOS may show a Gatekeeper warning — right-click the app and choose **Open** to bypass it. No Python or additional dependencies required.

### Standalone binary

Download the binary for your platform from the [latest release](https://github.com/ArtemisMucaj/jarvis-mcp/releases/latest):

| Platform | File |
|---|---|
| macOS (Apple Silicon) | `jarvis-<version>-macos-arm64` |
| Linux (x86_64) | `jarvis-<version>-linux-x86_64` |

```bash
chmod +x jarvis-<version>-linux-x86_64
./jarvis-<version>-linux-x86_64 --http 7070
```

### From source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run python -m jarvis --http 7070
```

## Connecting your agent

Point your agent at the Jarvis HTTP endpoint:

```json
{
  "mcp": {
    "jarvis": {
      "type": "http",
      "url": "http://127.0.0.1:7070/mcp"
    }
  }
}
```

The port is configurable (default `7070`).

## Configuration

Jarvis reads from `~/.jarvis/servers.json`. The format follows the standard MCP config schema.

**HTTP server:**
```json
{
  "mcpServers": {
    "my-server": {
      "url": "https://example.com/mcp",
      "transport": "http"
    }
  }
}
```

**stdio server:**
```json
{
  "mcpServers": {
    "my-tool": {
      "command": "npx",
      "args": ["-y", "@some/mcp-server"]
    }
  }
}
```

**OAuth server:**
```json
{
  "mcpServers": {
    "atlassian": {
      "url": "https://mcp.atlassian.com/v1/mcp",
      "transport": "http",
      "auth": "oauth"
    }
  }
}
```

**Disable a server** without removing it:
```json
{ "enabled": false }
```

**Disable individual tools** from a server:
```json
{ "disabledTools": ["dangerous_tool", "noisy_tool"] }
```

**Environment variable substitution** in `env` values:
```json
{ "env": { "API_KEY": "${MY_API_KEY}" } }
```

## Managing servers and tools

### macOS app

The menu bar app keeps Jarvis running as a persistent HTTP server. From the menu bar icon you can start/stop the server, copy the endpoint URL, and open the main window to browse servers, switch presets, and tail the log. Server and tool toggles, as well as preset switches, apply live — no restart needed.

### TUI

Two terminal UIs are available for interactive management.

**Browse and toggle servers and tools:**
```bash
jarvis mcp
```
Opens a tree of all configured servers and their tools. `Space` to enable/disable, `r` to re-probe, `q` to save and quit.

**Manage OAuth authentication:**
```bash
jarvis auth
```
Lists all servers and their auth status. `l` to trigger the OAuth login flow for the selected server (opens the browser), `x` to clear all cached tokens.

## OAuth authentication

Servers with `"auth": "oauth"` require a one-time browser login. Use the `auth` TUI to trigger the flow:

```bash
jarvis auth
```

Select the server and press `l` to open the browser login flow. Tokens are stored in `~/.jarvis/` and reused automatically on subsequent runs.

If a proxied tool call returns a 401/Unauthorized error, Jarvis silently exchanges the stored refresh token for a new access token and asks the caller to retry — no browser prompt as long as the refresh token is still valid. Only when the refresh token has also expired do you need to re-run `jarvis auth`.

## Modes

### Default — BM25 search

The agent uses `search_tools` to find relevant tools by natural language query, then `call_tool` to invoke them. Keeps context minimal regardless of how many tools are configured.

### Code Mode

Instead of searching one tool at a time, the agent writes a sandboxed Python script that batches multiple tool calls in a single step. Useful for tasks that require many sequential tool interactions.

```bash
jarvis --http 7070 --code-mode
```

Can also be toggled in the macOS app under **Settings**.

## CLI reference

```
Usage: jarvis [--config PATH] [COMMAND] [OPTIONS]

Commands:
  mcp               Browse and toggle MCP servers and tools (TUI)
  auth              Manage OAuth authentication for MCP servers (TUI)

Options:
  --config PATH     Use a specific config file
  --http PORT       Run as an HTTP server on PORT (management UI)
  --code-mode       Enable code mode transform
  --help, -h        Show this message and exit

With no command or options, runs as a stdio MCP server.
```

**Config resolution order** (when `--config` is not passed):
1. Active preset from `~/.jarvis/presets.json` (if set and the file exists)
2. `~/.jarvis/servers.json` (auto-created empty if missing)

## REST API

When running with `--http PORT`, a companion REST API starts on `PORT + 1` (default `7071`), bound to `127.0.0.1`. The macOS app uses this internally.

Changes made through the API — activating a preset, toggling a server, toggling an individual tool, or overwriting `servers.json` — are applied **live**. The inner proxy is rebuilt (or mutated in place for single-tool toggles) and a `notifications/tools/list_changed` message is pushed to every connected MCP session, so clients pick up the new tool set without reconnecting or losing their session.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Server status and ports |
| GET | `/api/tools` | Probe all servers, return full tool catalogue |
| GET | `/api/config` | Read active `servers.json` |
| PUT | `/api/config` | Overwrite `servers.json` |
| POST | `/api/servers/{name}/toggle` | Enable/disable a server — `{"enabled": bool}` |
| POST | `/api/tools/toggle` | Enable/disable a tool — `{"server", "tool", "enabled"}` |
| GET | `/api/presets` | List presets and active preset ID |
| POST | `/api/presets` | Create a preset — `{"name", "filePath"}` |
| PATCH | `/api/presets/{id}` | Rename or update a preset |
| DELETE | `/api/presets/{id}` | Delete a preset |
| POST | `/api/presets/{id}/activate` | Switch to a preset (use `id=default` to revert to `~/.jarvis/servers.json`) |

## File locations

| Item | Path |
|---|---|
| Server config | `~/.jarvis/servers.json` |
| Preset list | `~/.jarvis/presets.json` |
| OAuth tokens | `~/.jarvis/cache.db` |
| Logs | `~/.jarvis/jarvis.log` |

## Building from source

```bash
# Binary only
bash scripts/build_jarvis_binary.sh        # macOS → macOs/Jarvis/Jarvis/Resources/jarvis
bash scripts/build_jarvis_binary_linux.sh  # Linux → dist/jarvis

# macOS app (build binary first)
xcodebuild -project macOs/Jarvis/Jarvis.xcodeproj -scheme Jarvis -configuration Debug build
```

Requires `uv` at build time. PyInstaller 6.19.0 is fetched automatically.
