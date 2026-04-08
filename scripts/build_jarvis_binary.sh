#!/usr/bin/env bash
set -euo pipefail

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not installed. See https://docs.astral.sh/uv/"; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/macOs/Jarvis/Jarvis/Resources"

[[ -f "$REPO_ROOT/src/jarvis/__main__.py" ]] || { echo "ERROR: src/jarvis/__main__.py not found at $REPO_ROOT"; exit 1; }

echo "==> Building jarvis binary with PyInstaller..."

mkdir -p "$OUT_DIR"

uv run --with 'pyinstaller==6.19.0' pyinstaller \
  --onefile \
  --name jarvis \
  --distpath "$OUT_DIR" \
  --workpath /tmp/jarvis-pyinstaller-build \
  --specpath /tmp/jarvis-pyinstaller-spec \
  --clean \
  --copy-metadata fastmcp \
  --copy-metadata mcp \
  --copy-metadata anyio \
  --copy-metadata httpx \
  --copy-metadata pydantic \
  --copy-metadata starlette \
  --copy-metadata uvicorn \
  --copy-metadata textual \
  --copy-metadata pydantic-monty \
  --hidden-import pydantic_monty \
  "$REPO_ROOT/src/jarvis/__main__.py"

echo "==> Done. Binary at: $OUT_DIR/jarvis"
ls -lh "$OUT_DIR/jarvis"
