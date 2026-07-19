#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -Dm755 "$ROOT/fat32defrag" /usr/bin/fat32defrag
install -Dm755 "$ROOT/gui/fat32defrag_gui.py" /usr/lib/fat32defrag/fat32defrag_gui.py
install -Dm755 "$ROOT/gui/allocation_mapper.py" /usr/lib/fat32defrag/allocation_mapper.py
install -Dm755 "$ROOT/gui/privileged_helper.py" /usr/lib/fat32defrag/privileged_helper.py
install -Dm755 "$ROOT/gui/exfat_engine.py" /usr/lib/fat32defrag/exfat_engine.py
mkdir -p /usr/lib/fat32defrag/backends
install -m644 "$ROOT"/gui/backends/*.py /usr/lib/fat32defrag/backends/
install -Dm755 "$ROOT/packaging/liux-defragger" /usr/bin/liux-defragger
install -Dm755 "$ROOT/packaging/fat32defrag-gui" /usr/bin/fat32defrag-gui
install -Dm644 "$ROOT/packaging/io.github.liuxdefragger.desktop" /usr/share/applications/io.github.liuxdefragger.desktop
install -Dm644 "$ROOT/packaging/io.github.liuxdefragger.svg" /usr/share/icons/hicolor/scalable/apps/io.github.liuxdefragger.svg
echo "Installed Liux Defragger 1.5.0."
echo "Launch it from the application menu or run: liux-defragger"
