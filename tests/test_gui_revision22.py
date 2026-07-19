#!/usr/bin/env python3
"""Static regression checks for revision 22 GUI behaviour."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
gui=(ROOT/'gui/linux_defragger_gui.py').read_text()
version=(ROOT/'gui/version.py').read_text()
exfat=(ROOT/'gui/backends/exfat.py').read_text()
helper=(ROOT/'gui/privileged_helper.py').read_text()
assert 'VERSION = "1.8.0-22"' in version
assert 'PACKAGE_REVISION' not in gui
assert '[self.engine, "--version"]' in gui
assert 'GLib.idle_add(self._auto_analyse_selected' in gui
resize=gui[gui.index('def _refresh_map_after_resize'):gui.index('def _apply_map')]
assert '.analyze(' not in resize and '.queue_draw()' in resize
assert 'self.map_cache' in gui
assert 'growth_10_satisfied' in gui
assert 'new_window_item' in gui and 'def new_window(self)' in gui
assert 'Create fragmented test data…' in gui
assert 'CAP_GROWTH_DEFRAG' in exfat
assert '"growth-defrag"' in helper
print('revision 22 GUI regression checks passed')
