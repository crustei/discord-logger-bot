"""backfill reads ./exports unless EXPORTS_DIR or a command-line path is set."""

from pathlib import Path

import backfill


def test_export_roots_default_to_a_relative_directory(monkeypatch):
    monkeypatch.delenv("EXPORTS_DIR", raising=False)
    assert backfill.export_roots() == ["./exports"]
    monkeypatch.setenv("EXPORTS_DIR", "  ")
    assert backfill.export_roots() == ["./exports"]
    monkeypatch.setenv("EXPORTS_DIR", "custom/exports")
    assert backfill.export_roots() == ["custom/exports"]


def test_backfill_source_has_no_machine_paths():
    text = Path("backfill.py").read_text(encoding="utf-8")
    for needle in ("/cursor/", "/workspace", "/home/box"):
        assert needle not in text
