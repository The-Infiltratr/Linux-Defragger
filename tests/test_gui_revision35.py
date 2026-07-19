#!/usr/bin/env python3
"""Static regression checks for the revision 35 modular GUI architecture."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gui = (ROOT / "gui" / "linux_defragger_gui.py").read_text()
helper = (ROOT / "gui" / "privileged_helper.py").read_text()
version = (ROOT / "gui" / "version.py").read_text()
ext4 = (ROOT / "gui" / "backends" / "ext4.py").read_text()

assert 'VERSION = "1.8.0-35"' in version
assert 'gi.require_version("Gdk", "3.0")' in gui
assert "Packed tail outside filesystem" in gui
assert "outside_filesystem_blocks" in ext4
assert "physical_blocks = max(total_blocks, reader.size // block_size)" in ext4
assert "def _first_overlapping_cell" in gui
assert "while index < len(cells):" in gui
stream_handler = gui[gui.index("def _handle_engine_stream_line"):gui.index("def _handle_helper_message")]
assert "for cell in cells:" not in stream_handler
assert "self._schedule_live_redraw()" in gui
assert "GLib.timeout_add(100, self._flush_live_redraw)" in gui
assert "stop_id = self.helper_active_id" in gui
assert "self.helper_request_id + 1" not in gui
assert 'clean.startswith(("@@LIVE_MAP ", "@@LIVE_RANGE ", "@@LIVE_RANGES "))' in gui
assert 'ranges_prefix = "@@LIVE_RANGES "' in gui
assert '"outside": int(changed.get("outside", 0))' in gui

# Mutation dispatch is now filesystem-neutral in both the GUI and root helper.
assert "self.operation_engine" in gui
assert 'program == "operation-engine"' in helper
assert "native-compact-engine" not in gui
assert "NATIVE_COMPACT_ENGINE" not in helper
assert "find_native_compact_engine" not in gui
print("revision 35 GUI architecture checks passed")
