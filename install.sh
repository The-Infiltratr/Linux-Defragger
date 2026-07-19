#!/bin/sh
# Linux Defragger
# Author: Shannon Smith
# Purpose: Build, install or test support script.

set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -Dm755 "$ROOT/build/linux-defragger-engine" /usr/bin/linux-defragger-engine
install -Dm755 "$ROOT/gui/linux_defragger_gui.py" /usr/lib/linux-defragger/linux_defragger_gui.py
install -Dm755 "$ROOT/gui/allocation_mapper.py" /usr/lib/linux-defragger/allocation_mapper.py
install -Dm755 "$ROOT/gui/privileged_helper.py" /usr/lib/linux-defragger/privileged_helper.py
install -Dm755 "$ROOT/gui/exfat_engine.py" /usr/lib/linux-defragger/exfat_engine.py
install -Dm755 "$ROOT/gui/affs_engine.py" /usr/lib/linux-defragger/affs_engine.py
mkdir -p /usr/lib/linux-defragger/vendor
cp -a "$ROOT/vendor/amitools" /usr/lib/linux-defragger/vendor/
mkdir -p /usr/lib/linux-defragger/backends
install -m644 "$ROOT"/gui/backends/*.py /usr/lib/linux-defragger/backends/
install -Dm755 "$ROOT/packaging/linux-defragger" /usr/bin/linux-defragger
install -Dm644 "$ROOT/packaging/io.github.linuxdefragger.desktop" /usr/share/applications/io.github.linuxdefragger.desktop
install -Dm644 "$ROOT/packaging/io.github.linuxdefragger.svg" /usr/share/icons/hicolor/scalable/apps/io.github.linuxdefragger.svg
