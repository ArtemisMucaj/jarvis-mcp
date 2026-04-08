# AGENTS.md

## What this is

Jarvis is an MCP proxy that aggregates multiple MCP servers behind 2 synthetic tools (`search_tools` + `call_tool`). Python 3.11+, managed with **uv**.

## Layout

```
src/jarvis/        # the package (6 modules)
  __main__.py      # CLI entrypoint — arg parsing, server startup
  config.py        # DATA_DIR, presets, config loading, OAuth wiring
  proxy.py         # builds FastMCP proxy (stdio vs HTTP client selection)
  api.py           # REST management API (runs on port+1)
  probe.py         # server/tool discovery
  tui.py           # Textual TUIs (mcp manager, auth manager)
tests/unit/        # pure unit tests
tests/integration/ # API endpoint + TUI tests
scripts/           # PyInstaller build scripts
macOs/             # Xcode project for the menu bar app
```

## Commands

```bash
# Install deps (no separate install step — uv handles it)
uv sync --group dev

# Run locally
uv run python -m jarvis --http 7070

# Run all tests
uv run --group dev pytest tests

# Run a single test file or test
uv run --group dev pytest tests/unit/test_config.py
uv run --group dev pytest tests/unit/test_config.py::test_name -k test_name

# Build standalone binary (macOS arm64)
bash scripts/build_jarvis_binary.sh

# Build standalone binary (Linux x86_64)
bash scripts/build_jarvis_binary_linux.sh
```

## Testing quirks

- **pytest-asyncio `auto` mode** is on (`asyncio_mode = "auto"` in pyproject.toml). Do not add `@pytest.mark.asyncio` to async tests.
- **`conftest.py` sets `JARVIS_DATA_DIR` at import time** before any jarvis module is imported. This isolates tests from `~/.jarvis`. If you add a new conftest or rearrange imports, preserve this ordering — the module-level `DATA_DIR` and `token_storage` in `config.py` bind once on first import.
- Use the `data_dir` fixture for per-test isolation. It monkeypatches `DATA_DIR`, `PRESETS_PATH`, and `token_storage` across `config`, `api`, and `probe` modules.
- Use the `servers_json` fixture when you need a pre-populated `servers.json` in the isolated data dir.

## Architecture notes

- `config.py` resolves `DATA_DIR` and creates `token_storage` (DiskStore) **at module level**. The env var `JARVIS_DATA_DIR` overrides the default `~/.jarvis` — this is the only mechanism for test isolation.
- `proxy.py` chooses `StatefulProxyClient` (persistent subprocess) for stdio servers and `ProxyClient` (fresh connection) for HTTP/SSE. The stateful clients are pinned to `mcp._stateful_clients` to avoid GC.
- The hatchling build uses `packages = ["src/jarvis"]` — the wheel package is `jarvis`, not `jarvis_mcp`.

## macOS app

The menu bar app (`macOs/Jarvis/`) is a Swift/Xcode project that embeds the PyInstaller binary. **Build order matters** — the binary must exist before Xcode can bundle it:

```bash
# 1. Build the Python binary into the Xcode Resources dir
bash scripts/build_jarvis_binary.sh   # → macOs/Jarvis/Jarvis/Resources/jarvis

# 2. Build the app
xcodebuild -project macOs/Jarvis/Jarvis.xcodeproj -scheme Jarvis -configuration Debug build
```
## CI

- Every push/PR: pytest + binary builds (macOS arm64, Linux x86_64).
- No lint or typecheck step in CI. Ruff cache exists locally but there is no enforced config.
- Releases trigger on `v*` tags and produce binaries + a macOS `.dmg`.
