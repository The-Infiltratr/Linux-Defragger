#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify that read-only analysis is available for mounted volumes while writes remain offline-only.

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gui = (ROOT / "gui" / "linux_defragger_gui.py").read_text()
engine = (ROOT / "src" / "linux_defragger_engine.c").read_text()

assert "self.analyze_button.set_sensitive(enabled and bool(caps & CAP_ANALYSE))" in gui
assert "Unmount it before raw allocation analysis" not in gui
assert "live mounted snapshot" in gui
assert "is_block && writable && block_device_is_mounted" in engine
assert "if (is_block && writable) flags |= O_EXCL;" in engine
assert "if (fs->dev.writable)" in engine
print("mounted read-only analysis policy test passed")
