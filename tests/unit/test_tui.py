"""Unit tests for pure helpers in ``jarvis.tui``.

We avoid instantiating the Textual ``App`` classes — those require a running
event loop and a terminal.  Instead we exercise the module-level ``load_config``
helper which is the only piece of non-UI logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from jarvis.tui import load_config


class TestLoadConfig:
    def test_reads_existing_config(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        payload = {"mcpServers": {"x": {"url": "http://x"}}}
        path.write_text(json.dumps(payload))
        raw, err = load_config(path)
        assert raw == payload
        assert err is None

    def test_missing_file_returns_empty_no_error(self, tmp_path: Path) -> None:
        raw, err = load_config(tmp_path / "nope.json")
        assert raw == {"mcpServers": {}}
        assert err is None

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{ not json")
        raw, err = load_config(path)
        assert raw == {"mcpServers": {}}
        assert err is not None
        assert "parse error" in err.lower()
