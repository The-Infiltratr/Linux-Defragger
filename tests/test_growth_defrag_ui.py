#!/usr/bin/env python3
"""Verify Growth Defrag capability, plugin declarations and generic GUI wiring."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

from backends.contracts import CAP_GROWTH_DEFRAG
from backends.exfat import BACKEND as EXFAT
from backends.fat12 import BACKEND as FAT12
from backends.fat16 import BACKEND as FAT16
from backends.fat32 import BACKEND as FAT32
from core.operations import build_standard_arguments

GUI = (ROOT / "gui" / "linux_defragger_gui.py").read_text()
HELPER = (ROOT / "gui" / "privileged_helper.py").read_text()

for backend in (FAT12, FAT16, FAT32, EXFAT):
    assert backend.info.capabilities & CAP_GROWTH_DEFRAG
    operation = backend.info.operation("growth-defrag")
    assert operation is not None

arguments = build_standard_arguments("growth-defrag", 262080)
assert arguments == [
    "--growth-percent", "10",
    "--batch-clusters", "4096",
    "--ram-buffer", "auto",
    "--workers", "auto",
    "--live-map-cells", "262080",
]
assert "growth_10_satisfied" in GUI
assert 'Gtk.Button.new_with_label("Growth Defrag")' in GUI
assert 'self.start_mutation("growth-defrag")' in GUI
assert 'program == "operation-engine"' in HELPER
assert 'Growth Defrag status:          Not needed;' in GUI
assert 'Growth Defrag not needed · existing 10% growth-space layout verified' in GUI
assert 'stopped_safely = returncode == 130' in GUI
assert 'self.post_analysis_progress_text = "Stopped safely"' in GUI
assert 'f"{display_name} stopped safely · allocation map refreshed"' in GUI
print("FAT/exFAT Growth Defrag plugin wiring test passed")
