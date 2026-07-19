#!/bin/sh
# Linux Defragger
# Author: Shannon Smith
# Purpose: Install a completed local build and its desktop integration.

set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -Dm755 "$ROOT/build/linux-defragger-engine" /usr/bin/linux-defragger-engine
install -Dm755 "$ROOT/gui/linux_defragger_gui.py" /usr/lib/linux-defragger/linux_defragger_gui.py
install -Dm755 "$ROOT/gui/allocation_mapper.py" /usr/lib/linux-defragger/allocation_mapper.py
install -Dm755 "$ROOT/gui/privileged_helper.py" /usr/lib/linux-defragger/privileged_helper.py
install -Dm755 "$ROOT/gui/operation_engine.py" /usr/lib/linux-defragger/operation_engine.py
install -Dm755 "$ROOT/gui/exfat_engine.py" /usr/lib/linux-defragger/exfat_engine.py
install -Dm755 "$ROOT/gui/affs_engine.py" /usr/lib/linux-defragger/affs_engine.py
install -Dm755 "$ROOT/gui/apple_engine.py" /usr/lib/linux-defragger/apple_engine.py
install -Dm755 "$ROOT/gui/ntfs_engine.py" /usr/lib/linux-defragger/ntfs_engine.py
install -Dm755 "$ROOT/gui/native_compact_engine.py" /usr/lib/linux-defragger/native_compact_engine.py
install -Dm644 "$ROOT/gui/version.py" /usr/lib/linux-defragger/version.py
install -Dm755 "$ROOT/tools/linux-defragger-testdata.py" /usr/bin/linux-defragger-testdata
install -Dm755 "$ROOT/build/hfs_engine" /usr/lib/linux-defragger/hfs_engine
mkdir -p /usr/lib/linux-defragger/vendor
cp -a "$ROOT/vendor/amitools" /usr/lib/linux-defragger/vendor/
mkdir -p /usr/lib/linux-defragger/backends /usr/lib/linux-defragger/core
install -m644 "$ROOT"/gui/backends/*.py /usr/lib/linux-defragger/backends/
install -m644 "$ROOT"/gui/core/*.py /usr/lib/linux-defragger/core/
install -Dm755 "$ROOT/packaging/linux-defragger" /usr/bin/linux-defragger
install -Dm644 "$ROOT/packaging/io.github.linuxdefragger.desktop" /usr/share/applications/io.github.linuxdefragger.desktop
install -Dm644 "$ROOT/packaging/io.github.linuxdefragger.svg" /usr/share/icons/hicolor/scalable/apps/io.github.linuxdefragger.svg
