#!/usr/bin/env python3
"""Static regression checks for revision 33 GUI behaviour."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
gui=(ROOT/'gui/linux_defragger_gui.py').read_text()
version=(ROOT/'gui/version.py').read_text()
helper=(ROOT/'gui/privileged_helper.py').read_text()
ext4=(ROOT/'gui/backends/ext4.py').read_text()

assert 'VERSION = "1.8.0-33"' in version
assert 'gi.require_version("Gdk", "3.0")' in gui
assert 'Packed tail outside filesystem' in gui
assert 'outside_filesystem_blocks' in ext4
assert 'physical_blocks = max(total_blocks, reader.size // block_size)' in ext4
assert 'def _first_overlapping_cell' in gui
assert 'while index < len(cells):' in gui
assert 'for cell in cells:' not in gui[gui.index('def _handle_engine_stream_line'):gui.index('def _handle_helper_message')]
assert 'self._schedule_live_redraw()' in gui
assert 'GLib.timeout_add(100, self._flush_live_redraw)' in gui
assert 'stop_id = self.helper_active_id' in gui
assert 'self.helper_request_id + 1' not in gui
assert 'clean.startswith(("@@LIVE_MAP ", "@@LIVE_RANGE ", "@@LIVE_RANGES "))' in gui
assert 'ranges_prefix = "@@LIVE_RANGES "' in gui
assert '"outside": int(changed.get("outside", 0))' in gui
assert '"growth-defrag"' in helper
assert 'never creates fragmentation' in gui
assert 'lowest suitable free run' in gui
print('revision 33 GUI regression checks passed')
