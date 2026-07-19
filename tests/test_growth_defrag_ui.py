#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify the FAT-only Growth Defrag capability and GUI wiring.

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gui = (ROOT / "gui" / "linux_defragger_gui.py").read_text()
helper = (ROOT / "gui" / "privileged_helper.py").read_text()
base = (ROOT / "gui" / "backends" / "base.py").read_text()
fat = (ROOT / "gui" / "backends" / "fat_common.py").read_text()

assert "CAP_GROWTH_DEFRAG = 1 << 6" in base
assert "CAP_GROWTH_DEFRAG" in fat
assert 'Gtk.Button.new_with_label("Growth Defrag")' in gui
assert 'self.start_mutation("growth-defrag")' in gui
assert '"--growth-percent", "10"' in gui
assert '"growth-defrag"' in helper
print("FAT Growth Defrag GUI wiring test passed")
