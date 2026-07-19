#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: GTK interface for analysis, compaction, defragmentation, FAT/exFAT growth layouts and recovery.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""GTK3 user interface for Linux Defragger.

The GUI discovers volumes, selects a filesystem backend, renders allocation
maps and gives each independent window its own privileged helper for raw-device work.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from version import VERSION

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except (ImportError, ValueError) as exc:
    print(
        "Linux Defragger requires GTK 3 Python bindings.\n"
        "Install them on Linux Mint with:\n"
        "  sudo apt install python3-gi python3-cairo gir1.2-gtk-3.0",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

APP_ID = "io.github.linuxdefragger"
APP_NAME = "Linux Defragger"
PROJECT_URL = "https://github.com/The-Infiltratr/Linux-Defragger"
MIN_MAP_CELLS = 256
MAX_MAP_CELLS = 1048576
CAP_ANALYSE = 1 << 0
CAP_MAP = 1 << 1
CAP_COMPACT = 1 << 2
CAP_DEFRAG = 1 << 3
CAP_RECOVER = 1 << 4
CAP_LIVE_MAP = 1 << 5
CAP_GROWTH_DEFRAG = 1 << 6
BACKEND_CAPABILITIES: dict[str, int] = {}
# Linux filesystem names are normalised to backend identifiers here.
SUPPORTED_FILESYSTEMS = {
    "vfat": "fat",
    "fat": "fat",
    "fat12": "fat12",
    "fat16": "fat16",
    "fat32": "fat32",
    "msdos": "fat",
    "exfat": "exfat",
    "ntfs": "ntfs",
    "ntfs3": "ntfs",
    "ext2": "ext4",
    "ext3": "ext4",
    "ext4": "ext4",
    "btrfs": "btrfs",
    "xfs": "xfs",
    "hfs": "hfs",
    "hfsplus": "hfsplus",
    "hfs+": "hfsplus",
    "hfsx": "hfsplus",
    "apfs": "apfs",
}


def human_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{value} B"


def safe_journal_name(path: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.strip("/"))
    return cleaned or "volume"


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    target = root / "linux-defragger"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _configured_executable(variable: str, installed_path: str, description: str) -> str:
    candidate = os.environ.get(variable, installed_path)
    path = Path(candidate)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    raise FileNotFoundError(f"Could not locate {description}: {path}")


def find_engine() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_ENGINE",
        "/usr/bin/linux-defragger-engine",
        "the Linux Defragger engine",
    )


def find_mapper() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_MAPPER",
        "/usr/lib/linux-defragger/allocation_mapper.py",
        "the allocation mapper",
    )


def find_exfat_engine() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_EXFAT_ENGINE",
        "/usr/lib/linux-defragger/exfat_engine.py",
        "the native exFAT engine",
    )


def find_apple_engine() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_APPLE_ENGINE",
        "/usr/lib/linux-defragger/apple_engine.py",
        "the native Apple filesystem engine",
    )


def find_ntfs_engine() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_NTFS_ENGINE",
        "/usr/lib/linux-defragger/ntfs_engine.py",
        "the native NTFS maintenance engine",
    )


def find_native_compact_engine() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_NATIVE_COMPACT_ENGINE",
        "/usr/lib/linux-defragger/native_compact_engine.py",
        "the native ext4, XFS and Btrfs compact engine",
    )


def find_privileged_helper() -> str:
    return _configured_executable(
        "LINUX_DEFRAGGER_HELPER",
        "/usr/lib/linux-defragger/privileged_helper.py",
        "the privileged helper",
    )


@dataclass
class Volume:
    path: str
    name: str
    fstype: str
    label: str
    size: int
    mountpoints: list[str]
    removable: bool
    readonly: bool
    model: str
    transport: str
    image: bool = False

    @property
    def mounted(self) -> bool:
        return any(self.mountpoints)

    @property
    def normalized_fstype(self) -> str:
        return SUPPORTED_FILESYSTEMS.get(self.fstype.lower(), self.fstype.lower())

    @property
    def is_fat(self) -> bool:
        return self.normalized_fstype in {"fat", "fat12", "fat16", "fat32"}

    @property
    def is_fat32(self) -> bool:
        """Compatibility alias: all supported FAT variants use the native FAT engine."""
        return self.is_fat

    @property
    def capabilities(self) -> int:
        capabilities = BACKEND_CAPABILITIES.get(self.normalized_fstype, 0)
        # The ext backend analyses ext2, ext3 and ext4, but the native extent
        # exchange compactor is deliberately limited to actual ext4 volumes.
        if self.normalized_fstype == "ext4" and self.fstype.lower() != "ext4":
            capabilities &= ~CAP_COMPACT
        return capabilities

    @property
    def display_name(self) -> str:
        label = self.label or self.model or self.name
        status = "mounted" if self.mounted else "unmounted"
        kind = "image" if self.image else (self.transport or "device")
        filesystem = self.normalized_fstype.upper()
        return f"{self.path} — {label} — {filesystem} — {human_bytes(self.size)} — {kind}, {status}"


def json_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def flatten_lsblk(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        children = node.get("children") or []
        yield from flatten_lsblk(children)


def discover_volumes() -> list[Volume]:
    columns = "NAME,PATH,TYPE,FSTYPE,LABEL,SIZE,MOUNTPOINTS,RM,RO,MODEL,TRAN"
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--output", columns],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )
    data = json.loads(result.stdout)
    volumes: list[Volume] = []
    for node in flatten_lsblk(data.get("blockdevices", [])):
        fstype = str(node.get("fstype") or "")
        if fstype.lower() not in SUPPORTED_FILESYSTEMS:
            continue
        mountpoints = [str(x) for x in (node.get("mountpoints") or []) if x]
        volumes.append(
            Volume(
                path=str(node.get("path") or ""),
                name=str(node.get("name") or ""),
                fstype=fstype,
                label=str(node.get("label") or ""),
                size=int(node.get("size") or 0),
                mountpoints=mountpoints,
                removable=json_bool(node.get("rm")),
                readonly=json_bool(node.get("ro")),
                model=str(node.get("model") or "").strip(),
                transport=str(node.get("tran") or ""),
            )
        )
    volumes.sort(key=lambda v: (not v.removable, v.path))
    return volumes


class DiskMap(Gtk.DrawingArea):
    """Render backend allocation data as a dense, dynamically sized pixel map."""

    COLORS = {
        "free": (0.92, 0.94, 0.96),
        "used": (0.13, 0.43, 0.76),
        "fragmented": (0.94, 0.28, 0.22),
        "directory": (0.48, 0.28, 0.72),
        "unknown": (0.38, 0.40, 0.44),
        "bad": (0.08, 0.08, 0.10),
        "grid": (0.74, 0.77, 0.81),
        "background": (0.98, 0.98, 0.99),
    }

    def __init__(self) -> None:
        super().__init__()
        self.cells: list[dict[str, int]] = []
        self.unit_label = "clusters"
        self._layout: tuple[int, int, int] | None = None
        self.set_size_request(640, 260)
        self.set_has_tooltip(True)
        self.connect("draw", self._draw)
        self.connect("query-tooltip", self._query_tooltip)

    def set_cells(self, cells: list[dict[str, int]]) -> None:
        self.cells = cells
        self.queue_draw()

    def set_unit_label(self, label: str) -> None:
        self.unit_label = label

    def desired_cell_count(self, width: int | None = None, height: int | None = None) -> int:
        """Return the live Amiga-style map resolution for the drawable area."""
        if width is None or height is None:
            allocation = self.get_allocation()
            width = allocation.width
            height = allocation.height
        # One allocation sample per actual drawable device pixel.  The old
        # renderer divided each dimension by four, then stretched the sparse
        # rows vertically; that produced the visible horizontal bands.
        drawable_pixels = max(1, int(width)) * max(1, int(height))
        return max(MIN_MAP_CELLS, min(MAX_MAP_CELLS, drawable_pixels))

    @staticmethod
    def _mix(a: tuple[float, float, float], b: tuple[float, float, float], ratio: float):
        ratio = max(0.0, min(1.0, ratio))
        return tuple(a[i] * (1.0 - ratio) + b[i] * ratio for i in range(3))

    def _cell_colour(self, cell: dict[str, int]) -> tuple[float, float, float]:
        known_total = max(1, cell["free"] + cell["used"])
        used_ratio = cell["used"] / known_total
        colour = self._mix(self.COLORS["free"], self.COLORS["used"], used_ratio)

        # Overlay categories according to how much of the sampled cell they
        # actually occupy.  The former any-nonzero rule painted an entire map
        # pixel black when only one of its many filesystem blocks was metadata,
        # greatly exaggerating ext4's distributed inode tables and bitmaps.
        directory = cell.get("directory", 0)
        if directory:
            colour = self._mix(colour, self.COLORS["directory"],
                               min(1.0, (directory / known_total) ** 0.5))
        fragmented = cell.get("fragmented", 0)
        if fragmented:
            colour = self._mix(colour, self.COLORS["fragmented"],
                               min(1.0, (fragmented / known_total) ** 0.5))
        metadata = cell.get("bad", 0)
        if metadata:
            colour = self._mix(colour, self.COLORS["bad"],
                               min(1.0, (metadata / known_total) ** 0.5))

        unknown = cell.get("unknown", 0)
        total = cell["free"] + cell["used"] + unknown
        if unknown and total:
            colour = self._mix(colour, self.COLORS["unknown"], unknown / total)
        return colour

    def _draw(self, widget: Gtk.Widget, cr: Any) -> bool:
        allocation = widget.get_allocation()
        width = max(1, allocation.width)
        height = max(1, allocation.height)
        cr.set_source_rgb(*self.COLORS["background"])
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if not self.cells:
            cr.set_source_rgb(0.38, 0.40, 0.44)
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(15)
            message = "Select a supported volume and click Analyse"
            extents = cr.text_extents(message)
            cr.move_to((width - extents.width) / 2 - extents.x_bearing,
                       (height - extents.height) / 2 - extents.y_bearing)
            cr.show_text(message)
            return False

        # The analysis samples remain cached in memory.  Resample them over the
        # complete drawable pixel grid so resizing is an immediate redraw and
        # never causes another raw-device scan.
        columns = max(1, width)
        rows = max(1, height)
        total_pixels = columns * rows
        self._layout = (columns, rows, total_pixels)
        cell_count = len(self.cells)
        last_source = -1
        colour = self.COLORS["background"]
        for pixel_index in range(total_pixels):
            source_index = min(cell_count - 1, (pixel_index * cell_count) // total_pixels)
            if source_index != last_source:
                colour = self._cell_colour(self.cells[source_index])
                last_source = source_index
            row, col = divmod(pixel_index, columns)
            cr.set_source_rgb(*colour)
            cr.rectangle(float(col), float(row), 1.0, 1.0)
            cr.fill()
        return False

    def _query_tooltip(
        self, _widget: Gtk.Widget, x: int, y: int, _keyboard_mode: bool, tooltip: Gtk.Tooltip
    ) -> bool:
        if not self.cells or self._layout is None:
            return False
        columns, rows, total_pixels = self._layout
        col = int(x)
        row = int(y)
        if col < 0 or row < 0 or col >= columns or row >= rows:
            return False
        pixel_index = row * columns + col
        index = min(len(self.cells) - 1, (pixel_index * len(self.cells)) // total_pixels)
        cell = self.cells[index]
        tooltip.set_text(
            f"{self.unit_label.capitalize()} {cell['start']:,}–{cell['end']:,}\n"
            f"Used {cell['used']:,} · Free {cell['free']:,} · Unknown {cell.get('unknown', 0):,}\n"
            f"Fragmented {cell['fragmented']:,} · Directory {cell['directory']:,} · Metadata/reserved {cell.get('bad', 0):,}"
        )
        return True


class SummaryCard(Gtk.Frame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.set_shadow_type(Gtk.ShadowType.IN)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(10)
        self.title = Gtk.Label(label=title)
        self.title.set_xalign(0)
        self.title.get_style_context().add_class("summary-title")
        self.value = Gtk.Label(label="—")
        self.value.set_xalign(0)
        self.value.get_style_context().add_class("summary-value")
        box.pack_start(self.title, False, False, 0)
        box.pack_start(self.value, False, False, 0)
        self.add(box)

    def set_title(self, title: str) -> None:
        self.title.set_text(title)

    def set_value(self, value: str) -> None:
        self.value.set_text(value)


class MainWindow(Gtk.ApplicationWindow):
    """Coordinate device discovery, authentication, operations and live map updates."""
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=f"{APP_NAME} {VERSION}")
        self.set_default_size(1050, 760)
        self.set_position(Gtk.WindowPosition.CENTER)

        self.engine = find_engine()
        self.mapper = find_mapper()
        self._load_backend_registry()
        self.privileged_helper = find_privileged_helper()
        self.exfat_engine = find_exfat_engine()
        self.apple_engine = find_apple_engine()
        self.ntfs_engine = find_ntfs_engine()
        self.native_compact_engine = find_native_compact_engine()
        self.affs_engine = str(Path(__file__).resolve().parent / "affs_engine.py")
        if not Path(self.affs_engine).is_file():
            self.affs_engine = "/usr/lib/linux-defragger/affs_engine.py"
        self.volumes: list[Volume] = []
        self.current_volume: Volume | None = None
        self.map_data: dict[str, Any] | None = None
        self.map_cache: dict[str, dict[str, Any]] = {}
        self.process: subprocess.Popen[str] | None = None
        self.process_privileged = False
        self.stop_requested = False
        self.busy = False
        self.pulse_id: int | None = None
        self.determinate_progress = False
        self.post_analysis_status: str | None = None
        self.post_analysis_progress_text: str | None = None
        self.map_resize_timeout_id: int | None = None
        self.last_map_cell_target = 0

        self.helper_process: subprocess.Popen[str] | None = None
        self.helper_ready = False
        self.helper_starting = False
        self.helper_write_lock = threading.Lock()
        self.helper_request_id = 0
        self.helper_active_id: int | None = None
        self.helper_pending_command: tuple[list[str], str, Callable[[str], None] | None, Callable[[int, str], None] | None, bool] | None = None
        self.helper_output_parts: list[str] = []
        self.helper_stderr_parts: list[str] = []

        self.connect("destroy", self._shutdown_helper)
        self.engine_version = self._query_engine_version()
        self._build_ui()
        self._load_css()
        self.refresh_devices()
        # Authenticate as soon as the GUI has entered the GTK main loop.
        # The persistent helper is then reused for the complete application session.
        GLib.timeout_add(150, self._authenticate_on_launch)

    def _query_engine_version(self) -> str:
        """Read the installed native-engine version instead of duplicating its label."""
        try:
            result = subprocess.run(
                [self.engine, "--version"], check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
            match = re.search(r"(\d+\.\d+\.\d+-\d+)", result.stdout)
            return match.group(1) if match else VERSION
        except Exception:
            return VERSION

    def _load_backend_registry(self) -> None:
        global SUPPORTED_FILESYSTEMS, BACKEND_CAPABILITIES
        result = subprocess.run(
            [self.mapper, "--list-backends"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )
        data = json.loads(result.stdout)
        aliases: dict[str, str] = {}
        capabilities: dict[str, int] = {}
        for entry in data.get("backends", []):
            backend_id = str(entry["id"]).lower()
            caps = int(entry.get("capabilities", 0))
            capabilities[backend_id] = caps
            aliases[backend_id] = backend_id
            for alias in entry.get("aliases", []):
                aliases[str(alias).lower()] = backend_id
        # Linux normally reports all classic FAT variants as vfat. The native
        # engine probes the precise FAT width after opening the volume.
        aliases.setdefault("vfat", "fat32")
        aliases.setdefault("fat", "fat32")
        aliases.setdefault("msdos", "fat32")
        SUPPORTED_FILESYSTEMS = aliases
        BACKEND_CAPABILITIES = capabilities

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)
        outer.pack_start(self._build_menu_bar(), False, False, 0)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(12)
        outer.pack_start(root, True, True, 0)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label()
        title.set_markup("<span size='x-large' weight='bold'>Linux Defragger</span>")
        title.set_xalign(0)
        subtitle = Gtk.Label(
            label="Analyse fragmentation, compact free space, defragment files, or create FAT/exFAT growth-space layouts"
        )
        subtitle.set_xalign(0)
        subtitle.set_line_wrap(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        title_row.pack_start(title_box, True, True, 0)
        version = Gtk.Label(label=f"Engine {self.engine_version} · GUI {VERSION}")
        version.get_style_context().add_class("dim-label")
        title_row.pack_end(version, False, False, 0)
        root.pack_start(title_row, False, False, 0)

        device_frame = Gtk.Frame(label="Volume")
        device_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        device_box.set_border_width(8)
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)
        self.device_combo.connect("changed", self._on_device_changed)
        device_box.pack_start(self.device_combo, True, True, 0)
        self.refresh_button = Gtk.Button.new_with_label("Refresh")
        self.refresh_button.connect("clicked", lambda _b: self.refresh_devices(clear_cache=True))
        device_box.pack_start(self.refresh_button, False, False, 0)
        self.image_button = Gtk.Button.new_with_label("Open image…")
        self.image_button.connect("clicked", self._open_image)
        device_box.pack_start(self.image_button, False, False, 0)
        self.unmount_button = Gtk.Button.new_with_label("Unmount")
        self.unmount_button.connect("clicked", self._unmount_selected)
        device_box.pack_start(self.unmount_button, False, False, 0)
        device_frame.add(device_box)
        root.pack_start(device_frame, False, False, 0)

        cards = Gtk.Grid(column_spacing=8, row_spacing=8)
        cards.set_column_homogeneous(True)
        self.capacity_card = SummaryCard("Capacity")
        self.free_card = SummaryCard("Free space")
        self.files_card = SummaryCard("Files")
        self.fragmented_card = SummaryCard("Fragmentation")
        cards.attach(self.capacity_card, 0, 0, 1, 1)
        cards.attach(self.free_card, 1, 0, 1, 1)
        cards.attach(self.files_card, 2, 0, 1, 1)
        cards.attach(self.fragmented_card, 3, 0, 1, 1)
        root.pack_start(cards, False, False, 0)

        map_frame = Gtk.Frame(label="Allocation map")
        map_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        map_box.set_border_width(8)
        self.disk_map = DiskMap()
        self.disk_map.connect("size-allocate", self._on_map_size_allocate)
        map_box.pack_start(self.disk_map, True, True, 0)
        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        for label, colour in (
            ("Free", DiskMap.COLORS["free"]),
            ("Used", DiskMap.COLORS["used"]),
            ("Fragmented", DiskMap.COLORS["fragmented"]),
            ("Directory", DiskMap.COLORS["directory"]),
            ("Unknown", DiskMap.COLORS["unknown"]),
            ("Filesystem metadata/reserved", DiskMap.COLORS["bad"]),
        ):
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(15, 15)
            swatch.connect("draw", self._draw_swatch, colour)
            item.pack_start(swatch, False, False, 0)
            item.pack_start(Gtk.Label(label=label), False, False, 0)
            legend.pack_start(item, False, False, 0)
        self.map_caption = Gtk.Label(label="Each square represents a range of filesystem allocation units.")
        self.map_caption.set_xalign(1)
        self.map_caption.get_style_context().add_class("dim-label")
        legend.pack_end(self.map_caption, True, True, 0)
        map_box.pack_start(legend, False, False, 0)
        map_frame.add(map_box)
        root.pack_start(map_frame, True, True, 0)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.analyze_button = Gtk.Button.new_with_label("Analyse")
        self.analyze_button.connect("clicked", lambda _b: self.analyze())
        action_row.pack_start(self.analyze_button, False, False, 0)
        self.compact_button = Gtk.Button.new_with_label("Compact")
        self.compact_button.connect("clicked", lambda _b: self.start_mutation("compact"))
        action_row.pack_start(self.compact_button, False, False, 0)
        self.defrag_button = Gtk.Button.new_with_label("Defragment")
        self.defrag_button.connect("clicked", lambda _b: self.start_mutation("defrag"))
        action_row.pack_start(self.defrag_button, False, False, 0)
        self.growth_button = Gtk.Button.new_with_label("Growth Defrag")
        self.growth_button.set_tooltip_text(
            "FAT/exFAT: defragment files and leave 10% free expansion space after each file"
        )
        self.growth_button.connect("clicked", lambda _b: self.start_mutation("growth-defrag"))
        action_row.pack_start(self.growth_button, False, False, 0)
        self.recover_button = Gtk.Button.new_with_label("Recover")
        self.recover_button.connect("clicked", lambda _b: self.start_mutation("recover"))
        action_row.pack_start(self.recover_button, False, False, 0)
        self.stop_button = Gtk.Button.new_with_label("Stop safely")
        self.stop_button.connect("clicked", self._request_stop)
        self.stop_button.set_sensitive(False)
        action_row.pack_start(self.stop_button, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_show_text(True)
        self.progress.set_text("Ready")
        action_row.pack_start(self.progress, True, True, 8)
        root.pack_start(action_row, False, False, 0)

        expander = Gtk.Expander(label="Operation log")
        expander.set_expanded(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(150)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_buffer = self.log_view.get_buffer()
        scroll.add(self.log_view)
        expander.add(scroll)
        root.pack_start(expander, False, True, 0)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_xalign(0)
        self.status_label.get_style_context().add_class("dim-label")
        root.pack_start(self.status_label, False, False, 0)
        self._update_controls()

    def _build_menu_bar(self) -> Gtk.MenuBar:
        """Create conventional File and About menus for the desktop interface."""
        menu_bar = Gtk.MenuBar()

        file_item = Gtk.MenuItem.new_with_mnemonic("_File")
        file_menu = Gtk.Menu()
        file_item.set_submenu(file_menu)

        new_window_item = Gtk.MenuItem.new_with_mnemonic("_New window")
        new_window_item.connect("activate", lambda _item: self.get_application().new_window())
        file_menu.append(new_window_item)

        open_item = Gtk.MenuItem.new_with_mnemonic("_Open image…")
        open_item.connect("activate", self._open_image)
        file_menu.append(open_item)

        test_item = Gtk.MenuItem.new_with_label("Create fragmented test data…")
        test_item.connect("activate", self._create_fragmented_test_data)
        file_menu.append(test_item)

        refresh_item = Gtk.MenuItem.new_with_mnemonic("_Refresh volumes")
        refresh_item.connect("activate", lambda _item: self.refresh_devices(clear_cache=True))
        file_menu.append(refresh_item)

        file_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem.new_with_mnemonic("_Quit")
        quit_item.connect("activate", lambda _item: self.get_application().quit())
        file_menu.append(quit_item)

        about_item = Gtk.MenuItem.new_with_mnemonic("_About")
        about_menu = Gtk.Menu()
        about_item.set_submenu(about_menu)
        about_dialog_item = Gtk.MenuItem.new_with_label("About Linux Defragger")
        about_dialog_item.connect("activate", self._show_about)
        about_menu.append(about_dialog_item)

        menu_bar.append(file_item)
        menu_bar.append(about_item)
        return menu_bar

    def _create_fragmented_test_data(self, _item: Gtk.MenuItem) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Choose an empty folder on the test volume", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                            "Create test data", Gtk.ResponseType.OK)
        response = chooser.run()
        folder = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not folder:
            return
        if not self.confirm(
            "Create deliberately fragmented test data?",
            f"Linux Defragger will create and delete test files inside:\n{folder}\n\n"
            "Use an empty folder on a disposable test volume. Existing files outside that "
            "folder are not touched.",
        ):
            return
        tool = "/usr/bin/linux-defragger-testdata"
        self.clear_log()
        self.append_log(f"Creating fragmented test data in {folder}…")
        self._run_command([tool, folder], privileged=False, purpose="test-data")

    def _show_about(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(VERSION)
        dialog.set_comments(
            "Filesystem allocation analysis, free-space compaction, defragmentation, "
            "FAT and exFAT growth-space layouts and journalled recovery.\n"
            f"Native engine: {self.engine_version}"
        )
        dialog.set_authors(["Shannon Smith"])
        dialog.set_website(PROJECT_URL)
        dialog.set_website_label("Linux Defragger on GitHub")
        dialog.run()
        dialog.destroy()

    @staticmethod
    def _draw_swatch(_widget: Gtk.Widget, cr: Any, colour: tuple[float, float, float]) -> bool:
        cr.set_source_rgb(*colour)
        cr.rectangle(0, 0, 15, 15)
        cr.fill()
        return False

    def _load_css(self) -> None:
        css = b"""
        .summary-title { color: #68717d; font-size: 10pt; }
        .summary-value { font-size: 15pt; font-weight: bold; }
        .dim-label { color: #68717d; }
        button.suggested-action { font-weight: bold; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.defrag_button.get_style_context().add_class("suggested-action")

    def append_log(self, text: str) -> None:
        if not text:
            return
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text if text.endswith("\n") else text + "\n")
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)
        self.log_buffer.delete_mark(mark)

    def clear_log(self) -> None:
        self.log_buffer.set_text("")

    def show_error(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def confirm(self, title: str, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.add_button("Proceed", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def refresh_devices(self, preserve_path: str | None = None, clear_cache: bool = False) -> None:
        if self.busy:
            return
        if clear_cache:
            self.map_cache.clear()
        try:
            discovered = discover_volumes()
        except Exception as exc:
            self.show_error("Unable to enumerate storage devices", str(exc))
            return
        existing_images = [v for v in self.volumes if v.image]
        self.volumes = discovered + existing_images
        selected = preserve_path or (self.current_volume.path if self.current_volume else None)
        self.device_combo.remove_all()
        active = -1
        for index, volume in enumerate(self.volumes):
            self.device_combo.append_text(volume.display_name)
            if volume.path == selected:
                active = index
        if active < 0 and self.volumes:
            active = 0
        self.device_combo.set_active(active)
        if not self.volumes:
            self.current_volume = None
            self.status_label.set_text("No supported filesystems detected. Open an image or attach a supported volume.")
        self._update_controls()

    def _on_device_changed(self, combo: Gtk.ComboBoxText) -> None:
        index = combo.get_active()
        self.current_volume = self.volumes[index] if 0 <= index < len(self.volumes) else None
        self._reset_summary()
        if not self.current_volume:
            self.map_data = None
            self.disk_map.set_cells([])
            self._update_controls()
            return
        cached = self.map_cache.get(self.current_volume.path)
        if cached is not None:
            self._apply_map(cached)
            self.status_label.set_text(self.status_label.get_text() + " · cached analysis")
        else:
            self.map_data = None
            self.disk_map.set_cells([])
            self.status_label.set_text(self.current_volume.display_name + " · analysing…")
            selected_path = self.current_volume.path
            GLib.idle_add(self._auto_analyse_selected, selected_path)
        self._update_controls()

    def _auto_analyse_selected(self, selected_path: str) -> bool:
        if (self.current_volume is not None and self.current_volume.path == selected_path
                and selected_path not in self.map_cache and not self.busy):
            self.analyze(clear_log=True)
        return False

    def _detect_image_fstype(self, path: str) -> str:
        result = subprocess.run(
            ["blkid", "-p", "-o", "value", "-s", "TYPE", path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )
        detected = result.stdout.strip().lower() if result.returncode == 0 else ""
        if detected not in SUPPORTED_FILESYSTEMS:
            supported = "FAT12, FAT16, FAT32, exFAT, NTFS, ext2/3/4, Btrfs or XFS"
            detail = result.stderr.strip()
            raise RuntimeError(
                f"The image does not contain a recognised supported filesystem. "
                f"Supported types: {supported}." + (f"\n\n{detail}" if detail else "")
            )
        return detected

    def _open_image(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Open filesystem image",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        response = chooser.run()
        filename = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not filename:
            return
        path = str(Path(filename).resolve())
        try:
            fstype = self._detect_image_fstype(path)
            size = Path(path).stat().st_size
        except Exception as exc:
            self.show_error("Unable to open filesystem image", str(exc))
            return
        volume = Volume(
            path=path,
            name=Path(path).name,
            fstype=fstype,
            label=Path(path).name,
            size=size,
            mountpoints=[],
            removable=False,
            readonly=not os.access(path, os.W_OK),
            model="filesystem image",
            transport="file",
            image=True,
        )
        self.volumes = [v for v in self.volumes if v.path != path] + [volume]
        self.refresh_devices(path)

    def _unmount_selected(self, _button: Gtk.Button) -> None:
        volume = self.current_volume
        if not volume or volume.image or not volume.mounted:
            return
        self.clear_log()
        self.append_log(f"Unmounting {volume.path} through udisksctl…")
        self._run_command(
            ["udisksctl", "unmount", "-b", volume.path],
            privileged=True,
            purpose="unmount",
            on_success=lambda _out: self.refresh_devices(volume.path),
        )

    def journal_path(self) -> str:
        if not self.current_volume:
            return ""
        return str(state_dir() / f"{safe_journal_name(self.current_volume.path)}.journal")

    def _reset_summary(self) -> None:
        self.capacity_card.set_title("Capacity")
        self.free_card.set_title("Free space")
        self.files_card.set_title("Files")
        self.fragmented_card.set_title("Fragmentation")
        for card in (self.capacity_card, self.free_card, self.files_card, self.fragmented_card):
            card.set_value("—")
        self.map_caption.set_text("Pixel map · every available drawable pixel increases map detail.")

    def _desired_map_cells(self) -> int:
        return self.disk_map.desired_cell_count()

    def _on_map_size_allocate(self, _widget: Gtk.Widget, _allocation: Gdk.Rectangle) -> None:
        # The analysed allocation samples stay in memory.  DiskMap resamples those
        # samples to the new drawable size, so resizing never rereads the volume.
        if self.map_data:
            self.disk_map.queue_draw()

    def _refresh_map_after_resize(self) -> bool:
        self.map_resize_timeout_id = None
        self.disk_map.queue_draw()
        return False

    def _apply_map(self, data: dict[str, Any]) -> None:
        self.map_data = data
        if self.current_volume is not None:
            self.map_cache[self.current_volume.path] = data
        self.disk_map.set_cells(list(data["cells"]))
        self.last_map_cell_target = int(data.get("cell_count", len(data.get("cells", []))))
        filesystem = str(data.get("filesystem") or "fat32").upper()
        backend = str(data.get("backend") or "fat32-native")

        if backend == "read-only-domain":
            total_bytes = int(data["total_bytes"])
            free_bytes = int(data["free_bytes"])
            used_bytes = int(data["used_bytes"])
            unknown_bytes = int(data.get("unknown_bytes", 0))
            total_units = int(data["total_units"])
            cell_count = int(data["cell_count"])
            self.capacity_card.set_value(human_bytes(total_bytes))
            self.free_card.set_value(
                f"{human_bytes(free_bytes)} ({free_bytes * 100.0 / max(1, total_bytes):.1f}%)"
            )
            details = data.get("details") if isinstance(data.get("details"), dict) else {}
            is_swap = filesystem == "SWAP"
            has_fragmentation_summary = all(
                key in data
                for key in ("regular_files", "directories", "fragmented_files", "fragmented_directories")
            )
            if is_swap:
                self.files_card.set_title("Usage")
                used_pages = int(details.get("used_pages", 0))
                self.files_card.set_value(f"{human_bytes(used_bytes)} used · {used_pages:,} pages")
                self.fragmented_card.set_value("Not applicable")
            elif has_fragmentation_summary:
                self.files_card.set_value(
                    f"{int(data['regular_files']):,} files · {int(data['directories']):,} dirs"
                )
                if "fragmentation_percent" in data:
                    self.fragmented_card.set_value(
                        f"{float(data['fragmentation_percent']):.1f}% · "
                        f"{int(data['fragmented_files']):,} files"
                    )
                else:
                    self.fragmented_card.set_value(
                        f"{int(data['fragmented_files']):,} files · "
                        f"{int(data['fragmented_directories']):,} dirs"
                    )
            else:
                self.files_card.set_value(f"{human_bytes(used_bytes)} allocated")

                # Analysis-only filesystems do not expose file-level
                # fragmentation counts, so state their available operations.
                capabilities = self.current_volume.capabilities if self.current_volume else 0
                operations: list[str] = []
                if capabilities & CAP_COMPACT:
                    operations.append("Compact")
                if capabilities & CAP_DEFRAG:
                    operations.append("Defragment")
                if capabilities & CAP_GROWTH_DEFRAG:
                    operations.append("Growth Defrag")
                if capabilities & CAP_RECOVER:
                    operations.append("Recover")
                if operations:
                    self.fragmented_card.set_value("Not calculated")
                else:
                    self.fragmented_card.set_value("Not available")

            unit_size = int(data.get("unit_size", 512))
            if unit_size == 512:
                unit_name = "sectors"
            elif unit_size == 4096:
                unit_name = "4 KiB units"
            else:
                unit_name = f"{human_bytes(unit_size)} units"
            self.disk_map.set_unit_label(unit_name)
            per_cell = total_units / max(1, cell_count)
            if is_swap and bool(details.get("active")):
                self.map_caption.set_text(
                    "Physical occupied swap-slot locations are not exposed by the Linux kernel"
                )
            elif is_swap:
                self.map_caption.set_text(
                    f"Inactive swap area · approximately {per_cell:,.1f} {unit_name} per cell"
                )
            else:
                self.map_caption.set_text(
                    f"Pixel map: {cell_count:,} cells · approximately {per_cell:,.1f} {unit_name} per cell"
                )
            unknown = f" · {human_bytes(unknown_bytes)} location unknown" if unknown_bytes else ""
            if is_swap:
                state = "active" if bool(details.get("active")) else "inactive"
                self.status_label.set_text(
                    f"SWAP {state} · {human_bytes(used_bytes)} used · "
                    f"{human_bytes(free_bytes)} free · physical slot locations unavailable"
                )
            elif has_fragmentation_summary:
                self.status_label.set_text(
                    f"{filesystem} · {int(data['fragmented_files'])} fragmented files · "
                    f"{int(data['fragmented_directories'])} fragmented directories"
                )
            else:
                capabilities = self.current_volume.capabilities if self.current_volume else 0
                operations = []
                if capabilities & CAP_COMPACT:
                    operations.append("Compact")
                if capabilities & CAP_DEFRAG:
                    operations.append("Defragment")
                if capabilities & CAP_GROWTH_DEFRAG:
                    operations.append("Growth Defrag")
                if capabilities & CAP_RECOVER:
                    operations.append("Recover")
                if operations:
                    operation_text = ", ".join(operations)
                    self.status_label.set_text(
                        f"{filesystem} allocation map · available: {operation_text} · "
                        f"{human_bytes(used_bytes)} allocated{unknown}"
                    )
                else:
                    self.status_label.set_text(
                        f"{filesystem} read-only allocation map · "
                        f"{human_bytes(used_bytes)} allocated{unknown}"
                    )
            return

        cluster_size = int(data["cluster_size"])
        data_clusters = int(data["data_clusters"])
        free_clusters = int(data["free_clusters"])
        total_bytes = cluster_size * data_clusters
        free_bytes = cluster_size * free_clusters
        files = int(data["regular_files"])
        dirs = int(data["directories"])
        fragmented = int(data["fragmented_files"])
        fragmented_dirs = int(data["fragmented_directories"])
        self.capacity_card.set_value(human_bytes(total_bytes))
        self.free_card.set_value(
            f"{human_bytes(free_bytes)} ({free_clusters * 100.0 / max(1, data_clusters):.1f}%)"
        )
        self.files_card.set_value(f"{files:,} files · {dirs:,} dirs")
        self.fragmented_card.set_value(f"{fragmented:,} files · {fragmented_dirs:,} dirs")
        self.disk_map.set_unit_label("clusters")
        cell_count = int(data["cell_count"])
        per_cell = data_clusters / max(1, cell_count)
        self.map_caption.set_text(
            f"Pixel map: {cell_count:,} cells · approximately {per_cell:,.1f} clusters per cell"
        )
        self.status_label.set_text(
            f"{filesystem} {data['volume_id']} · {fragmented} fragmented files · "
            f"{int(data['free_gaps_below_highest']):,} free clusters below the high-water mark"
        )

    def analyze(
        self, clear_log: bool = True, target_cells: int | None = None, quiet: bool = False
    ) -> None:
        volume = self.current_volume
        if not volume or self.busy:
            return
        if not quiet:
            if clear_log:
                self.clear_log()
            else:
                self.append_log("\nRefreshing the allocation map…")
            self.append_log(f"Analysing {volume.normalized_fstype.upper()} volume {volume.path}…")
            if volume.mounted:
                self.append_log(
                    "The volume is mounted. Analysis is read-only; this is a live snapshot and "
                    "the map may change while the filesystem is active."
                )

        map_cells = target_cells if target_cells is not None else self._desired_map_cells()
        map_cells = max(MIN_MAP_CELLS, min(MAX_MAP_CELLS, int(map_cells)))

        if volume.is_fat32:
            args = [
                self.engine, "map", volume.path, "--cells", str(map_cells),
                "--journal", self.journal_path(),
            ]
        else:
            args = [
                self.mapper, volume.path, "--fstype", volume.normalized_fstype,
                "--cells", str(map_cells),
            ]

        def parsed(output: str) -> None:
            try:
                data = json.loads(output)
            except json.JSONDecodeError as exc:
                self.show_error(
                    "The analyser did not return a valid allocation map",
                    f"{exc}\n\n{output[-2000:]}",
                )
                return
            self._apply_map(data)
            if volume.mounted:
                self.status_label.set_text(self.status_label.get_text() + " · live mounted snapshot")
            if not quiet:
                if volume.is_fat32:
                    self.append_log(
                        f"Analysis complete: {data['fragmented_files']} fragmented files, "
                        f"{data['fragmented_directories']} fragmented directories."
                    )
                else:
                    if all(
                        key in data
                        for key in ("regular_files", "directories", "fragmented_files", "fragmented_directories")
                    ):
                        self.append_log(
                            f"Analysis complete: {data['fragmented_files']} fragmented files, "
                            f"{data['fragmented_directories']} fragmented directories."
                        )
                    else:
                        write_ops = []
                        if volume.capabilities & CAP_COMPACT: write_ops.append("Compact")
                        if volume.capabilities & CAP_DEFRAG: write_ops.append("Defragment")
                        if volume.capabilities & CAP_GROWTH_DEFRAG: write_ops.append("Growth Defrag")
                        if volume.capabilities & CAP_RECOVER: write_ops.append("Recover")
                        suffix = (" Available: " + ", ".join(write_ops) + ".") if write_ops else " Read-only analysis backend."
                        self.append_log(
                            f"Analysis complete: {human_bytes(int(data['used_bytes']))} allocated, "
                            f"{human_bytes(int(data['free_bytes']))} free." + suffix
                        )
            if self.post_analysis_status is not None:
                self.status_label.set_text(self.post_analysis_status)
                self.progress.set_fraction(1.0)
                self.progress.set_text(self.post_analysis_progress_text or "Complete")
                self.post_analysis_status = None
                self.post_analysis_progress_text = None

        self._run_engine_with_permission_retry(args, "analysis", parsed)

    def start_mutation(self, operation: str) -> None:
        volume = self.current_volume
        if not volume or self.busy:
            return
        required = {
            "compact": CAP_COMPACT,
            "defrag": CAP_DEFRAG,
            "growth-defrag": CAP_GROWTH_DEFRAG,
            "recover": CAP_RECOVER,
        }[operation]
        if not (volume.capabilities & required):
            self.show_error(
                "Operation unavailable",
                f"The {volume.normalized_fstype.upper()} backend does not advertise {operation}. "
                "The GUI enables operations from the backend capability table rather than filesystem names.",
            )
            return
        if volume.readonly:
            self.show_error("Read-only volume", f"{volume.path} is marked read-only.")
            return
        if volume.mounted:
            self.show_error(
                "The volume is mounted",
                "Unmount it first. Filesystem mutation engines intentionally refuse mounted volumes.",
            )
            return
        if (operation == "growth-defrag" and self.map_data is not None
                and bool(self.map_data.get("growth_10_satisfied"))):
            self.clear_log()
            self.append_log(
                f"Growth Defrag preflight: cached analysis confirms that {volume.path} "
                "already has contiguous files and at least 10% free growth space after each file."
            )
            self.append_log("No filesystem write or second scan is required.")
            self.progress.set_fraction(1.0)
            self.progress.set_text("Not needed")
            self.status_label.set_text("Growth Defrag not needed · cached 10% layout verified")
            return
        journal = self.journal_path()
        if operation != "recover" and Path(journal).exists():
            self.show_error(
                "Recovery is required",
                f"An unfinished journal exists at:\n{journal}\n\nRun Recover before any other operation.",
            )
            return
        if operation == "recover" and not Path(journal).exists():
            self.show_error("No recovery journal", "There is no unfinished transaction for this volume.")
            return

        descriptions = {
            "defrag": "Rebuild fragmented files as contiguous runs without compacting free space.",
            "compact": "Fill internal free-space gaps without attempting to defragment files.",
            "growth-defrag": (
                "FAT/exFAT: compact allocation, rebuild files contiguously in physical order, "
                "and leave a 10% free expansion gap after each regular file."
            ),
            "recover": "Complete or roll back the interrupted journalled transaction.",
        }
        extra_warning = ""
        if operation == "compact" and volume.normalized_fstype in {"ext4", "xfs"}:
            extra_warning = (
                "\n\nThis Compact pass mounts the otherwise-unmounted volume privately and uses "
                "the filesystem kernel driver to exchange high regular-file extents into low "
                "free ranges. It may increase fragmentation and does not move filesystem metadata."
                + (" XFS range exchange requires Linux 6.10 or newer."
                   if volume.normalized_fstype == "xfs" else "")
            )
        elif operation == "compact" and volume.normalized_fstype == "btrfs":
            extra_warning = (
                "\n\nBtrfs Compact temporarily shrinks the filesystem so the kernel "
                "relocates high physical chunks into lower free chunk ranges, then restores "
                "the exact original size. It does not run file defragmentation."
            )
        elif volume.normalized_fstype == "ntfs":
            if operation == "compact":
                extra_warning = (
                    "\n\nNTFS Compact moves complete physical extents into lower gaps while "
                    "preserving each file's existing fragment count. It does not join, split "
                    "or rebuild fragmented files."
                )
            elif operation == "defrag":
                extra_warning = (
                    "\n\nNTFS Defragment finds supported fragmented ordinary files, rebuilds "
                    "each one as a single contiguous extent, and allocates each rebuilt file "
                    "in the highest suitable free run anywhere on the volume."
                )
            else:
                extra_warning = (
                    "\n\nNTFS Recover completes or rolls back the one journalled native NTFS "
                    "file transaction that was interrupted."
                )
        operation_names = {
            "compact": "Compact",
            "defrag": "Defragment",
            "growth-defrag": "Growth Defrag",
            "recover": "Recover",
        }
        if not self.confirm(
            f"{operation_names[operation]} {volume.path}?",
            f"{descriptions[operation]}{extra_warning}\n\nThe volume must remain connected and unmounted. "
            "A clean Stop request finishes the active transaction before exiting.",
        ):
            return

        operation_engine = (self.exfat_engine if volume.normalized_fstype == "exfat" else
                            self.affs_engine if volume.normalized_fstype == "affs" else
                            self.apple_engine if volume.normalized_fstype in {"hfs", "hfsplus"} else
                            self.ntfs_engine if volume.normalized_fstype == "ntfs" else
                            self.native_compact_engine if (
                                operation == "compact" and volume.normalized_fstype in {"ext4", "btrfs", "xfs"}
                            ) else self.engine)
        args = [operation_engine, operation, volume.path, "--write", "--confirm", volume.path, "--journal", journal]
        if operation_engine == self.native_compact_engine:
            args += ["--filesystem", volume.normalized_fstype]
        live_cells = len(self.map_data.get("cells", [])) if self.map_data else self._desired_map_cells()
        live_cells = max(MIN_MAP_CELLS, min(MAX_MAP_CELLS, int(live_cells)))
        if operation == "defrag":
            if volume.normalized_fstype == "ntfs":
                args += ["--ram-buffer", "auto", "--workers", "auto",
                         "--live-map-cells", str(live_cells)]
            else:
                args += ["--transaction-files", "32", "--ram-buffer", "auto", "--workers", "auto",
                         "--live-map-cells", str(live_cells)]
        elif operation == "growth-defrag":
            args += ["--growth-percent", "10", "--batch-clusters", "4096",
                     "--ram-buffer", "auto", "--workers", "auto",
                     "--live-map-cells", str(live_cells)]
        elif operation == "compact":
            args += ["--ram-buffer", "auto", "--workers", "auto",
                     "--live-map-cells", str(live_cells)]
        else:
            args += ["--ram-buffer", "auto", "--workers", "auto"]

        self.map_cache.pop(volume.path, None)
        self.clear_log()
        self.append_log(f"Starting {operation_names[operation]} on {volume.path}…")
        self._run_command(
            args,
            privileged=not volume.image or not os.access(volume.path, os.R_OK | os.W_OK),
            purpose=operation,
            on_success=lambda _out: self.analyze(clear_log=False),
        )

    def _run_engine_with_permission_retry(
        self, args: list[str], purpose: str, on_success: Callable[[str], None]
    ) -> None:
        volume = self.current_volume
        if not volume:
            return
        privileged = not volume.image or not os.access(volume.path, os.R_OK | os.W_OK)
        self._run_command(
            args, privileged=privileged, purpose=purpose, on_success=on_success, stream_output=False
        )

    def _authenticate_on_launch(self) -> bool:
        """Open the persistent administrator session at application launch."""
        if self.helper_ready or self.helper_starting:
            return False
        self.status_label.set_text("Waiting for administrator authentication…")
        try:
            self._start_privileged_helper()
        except Exception as exc:
            self._helper_start_failed(f"Administrator authentication could not start: {exc}")
        return False

    def _helper_program_and_args(self, args: list[str]) -> tuple[str, list[str]]:
        executable = os.path.realpath(args[0])
        if executable == os.path.realpath(self.engine):
            return "engine", args[1:]
        if executable == os.path.realpath(self.mapper):
            return "mapper", args[1:]
        if executable == os.path.realpath(self.exfat_engine):
            return "exfat-engine", args[1:]
        if executable == os.path.realpath(self.affs_engine):
            return "affs-engine", args[1:]
        if executable == os.path.realpath(self.apple_engine):
            return "apple-engine", args[1:]
        if executable == os.path.realpath(self.ntfs_engine):
            return "ntfs-engine", args[1:]
        if executable == os.path.realpath(self.native_compact_engine):
            return "native-compact-engine", args[1:]
        if os.path.basename(args[0]) == "udisksctl":
            return "udisksctl", args[1:]
        raise RuntimeError(f"The privileged helper does not permit: {args[0]}")

    def _set_operation_started(self, purpose: str, privileged: bool) -> None:
        self.busy = True
        self.process_privileged = privileged
        self.stop_requested = False
        self._update_controls()
        self.progress.set_fraction(0.0)
        display_name = {
            "analysis": "Analysis",
            "compact": "Compact",
            "defrag": "Defragment",
            "growth-defrag": "Growth Defrag",
            "recover": "Recovery",
        }.get(purpose, purpose.capitalize())
        self.progress.set_text(f"{display_name} in progress…")
        self.determinate_progress = False
        self.pulse_id = GLib.timeout_add(120, self._pulse_progress)

    def _start_privileged_helper(self) -> None:
        if self.helper_ready or self.helper_starting:
            return
        if not shutil.which("pkexec"):
            self._helper_start_failed("pkexec is not installed; administrator authentication is unavailable.")
            return
        self.helper_starting = True
        self.append_log("Requesting administrator access for this application session…")

        def launcher() -> None:
            try:
                process = subprocess.Popen(
                    [shutil.which("pkexec") or "pkexec", self.privileged_helper],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                )
                self.helper_process = process
                threading.Thread(target=self._drain_helper_stderr, args=(process,), daemon=True).start()
                assert process.stdout is not None
                for raw in process.stdout:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        GLib.idle_add(self.append_log, f"Privileged helper returned invalid data: {raw.rstrip()}")
                        continue
                    GLib.idle_add(self._handle_helper_message, message)
                returncode = process.wait()
                GLib.idle_add(self._helper_exited, returncode)
            except Exception as exc:
                GLib.idle_add(self._helper_start_failed, str(exc))

        threading.Thread(target=launcher, daemon=True).start()

    def _drain_helper_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            self.helper_stderr_parts.append(line)

    def _helper_send(self, message: dict[str, Any]) -> None:
        process = self.helper_process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("the privileged helper is not running")
        encoded = json.dumps(message, separators=(",", ":")) + "\n"
        with self.helper_write_lock:
            process.stdin.write(encoded)
            process.stdin.flush()

    def _begin_helper_operation(self) -> None:
        pending = self.helper_pending_command
        if pending is None or not self.helper_ready:
            return
        args, purpose, on_success, raw_completion, stream_output = pending
        self.helper_pending_command = None
        try:
            program, helper_args = self._helper_program_and_args(args)
            self.helper_request_id += 1
            request_id = self.helper_request_id
            self.helper_active_id = request_id
            self.helper_output_parts = []
            self._helper_current = (purpose, on_success, raw_completion, stream_output)
            self._helper_send({
                "action": "run",
                "id": request_id,
                "program": program,
                "argv": helper_args,
            })
        except Exception as exc:
            self._command_finished(127, str(exc), purpose, on_success, raw_completion)

    def _handle_engine_stream_line(self, line: str) -> bool:
        range_prefix = "@@LIVE_RANGE "
        if line.startswith(range_prefix):
            try:
                delta = json.loads(line[len(range_prefix):])
                if not self.map_data or not isinstance(self.map_data.get("cells"), list):
                    return True
                cells = self.map_data["cells"]
                unit_size = int(
                    self.map_data.get("unit_size")
                    or self.map_data.get("cluster_size")
                    or 0
                )
                if unit_size <= 0:
                    return True

                def apply_range(start_byte: int, length_byte: int, make_used: bool) -> None:
                    if length_byte <= 0:
                        return
                    start_unit = start_byte // unit_size
                    end_unit = (start_byte + length_byte + unit_size - 1) // unit_size
                    for cell in cells:
                        cell_start = int(cell["start"])
                        cell_end = int(cell["end"]) + 1
                        overlap = max(0, min(end_unit, cell_end) - max(start_unit, cell_start))
                        if overlap <= 0:
                            continue
                        if make_used:
                            moved = min(overlap, int(cell.get("free", 0)))
                            cell["free"] = max(0, int(cell.get("free", 0)) - moved)
                            cell["used"] = int(cell.get("used", 0)) + moved
                        else:
                            old_used = max(1, int(cell.get("used", 0)))
                            moved = min(overlap, int(cell.get("used", 0)))
                            # The live view shows physical allocation movement.  Exact
                            # fragmentation colours are rebuilt by the final analysis.
                            frag = int(round(moved * int(cell.get("fragmented", 0)) / old_used))
                            directory = int(round(moved * int(cell.get("directory", 0)) / old_used))
                            cell["used"] = max(0, int(cell.get("used", 0)) - moved)
                            cell["free"] = int(cell.get("free", 0)) + moved
                            cell["fragmented"] = max(
                                0, min(int(cell.get("fragmented", 0)) - frag, cell["used"])
                            )
                            cell["directory"] = max(
                                0, min(int(cell.get("directory", 0)) - directory, cell["used"])
                            )

                source = int(delta["source_start_byte"])
                destination = int(delta["destination_start_byte"])
                length = int(delta["length_bytes"])
                apply_range(source, length, False)
                apply_range(destination, length, True)
                self.disk_map.set_cells(cells)
                moved_total = int(delta.get("moved_total_bytes", 0))
                pass_number = int(delta.get("pass", 1))
                self.status_label.set_text(
                    f"Live allocation update · Compact pass {pass_number} · "
                    f"{human_bytes(moved_total)} moved · fragmentation recalculated at completion"
                )
            except Exception as exc:
                self.append_log(f"Live allocation update could not be applied: {exc}")
            return True

        prefix = "@@LIVE_MAP "
        if not line.startswith(prefix):
            return False
        try:
            delta = json.loads(line[len(prefix):])
            if not self.map_data or not isinstance(self.map_data.get("cells"), list):
                return True
            cells = self.map_data["cells"]
            for changed in delta.get("cells", []):
                index = int(changed["i"])
                if 0 <= index < len(cells):
                    cells[index] = {
                        "start": int(changed["start"]),
                        "end": int(changed["end"]),
                        "free": int(changed["free"]),
                        "used": int(changed["used"]),
                        "fragmented": int(changed["fragmented"]),
                        "directory": int(changed["directory"]),
                        "bad": int(changed["bad"]),
                    }
            if "fragmented_files" in delta:
                self.map_data["fragmented_files"] = int(delta["fragmented_files"])
            if "fragmented_directories" in delta:
                self.map_data["fragmented_directories"] = int(delta["fragmented_directories"])
            if "free_clusters" in delta:
                self.map_data["free_clusters"] = int(delta["free_clusters"])
            if "free_gaps_below_highest" in delta:
                self.map_data["free_gaps_below_highest"] = int(delta["free_gaps_below_highest"])
            self.disk_map.set_cells(cells)
            if "fragmented_files" in self.map_data and "fragmented_directories" in self.map_data:
                self.fragmented_card.set_value(
                    f"{self.map_data['fragmented_files']:,} files · "
                    f"{self.map_data['fragmented_directories']:,} dirs"
                )
            if "free_clusters" in self.map_data:
                cluster_size = int(
                    self.map_data.get("cluster_size") or self.map_data.get("unit_size") or 0
                )
                free_bytes = int(self.map_data["free_clusters"]) * cluster_size
                capacity = int(
                    self.map_data.get("data_clusters") or self.map_data.get("total_units") or 0
                ) * cluster_size
                percent = (100.0 * free_bytes / capacity) if capacity else 0.0
                self.free_card.set_value(f"{human_bytes(free_bytes)} ({percent:.1f}%)")
            self.status_label.set_text("Live allocation map updated")
        except Exception as exc:
            self.append_log(f"Live map update could not be applied: {exc}")
        return True

    def _handle_helper_message(self, message: dict[str, Any]) -> bool:
        message_type = str(message.get("type", ""))
        if message_type == "ready":
            self.helper_ready = True
            self.helper_starting = False
            self.append_log("Administrator session unlocked at launch. Further operations will reuse it.")
            if not self.busy:
                self.status_label.set_text("Ready · Administrator session active")
            self._begin_helper_operation()
            return False
        if message_type == "progress" and message.get("id") == self.helper_active_id:
            try:
                percent = max(0.0, min(100.0, float(message.get("percent", 0.0))))
            except (TypeError, ValueError):
                percent = 0.0
            current = getattr(self, "_helper_current", None)
            purpose = current[0] if current else "operation"
            display_name = {
                "analysis": "Analysis",
                "compact": "Compact",
                "defrag": "Defragment",
                "growth-defrag": "Growth Defrag",
                "recover": "Recovery",
            }.get(purpose, purpose.capitalize())
            self.determinate_progress = True
            self.progress.set_fraction(percent / 100.0)
            self.progress.set_text(f"{display_name}: {percent:.2f}%")
            self.status_label.set_text(f"{display_name} in progress · {percent:.2f}%")
            return False
        if message_type == "output" and message.get("id") == self.helper_active_id:
            line = str(message.get("line", ""))
            if self._handle_engine_stream_line(line):
                return False
            self.helper_output_parts.append(line + "\n")
            current = getattr(self, "_helper_current", None)
            if current and current[3]:
                self.append_log(line)
            return False
        if message_type == "error":
            text = str(message.get("message", "privileged helper error"))
            if message.get("id") == self.helper_active_id:
                self.helper_output_parts.append(text + "\n")
            else:
                self.append_log(text)
            return False
        if message_type == "stop-result":
            if bool(message.get("delivered")):
                self.append_log("Stop signal delivered; the engine will exit after the active journalled transaction.")
            else:
                self.stop_requested = False
                self._update_controls()
                self.append_log(f"Stop signal was not delivered: {message.get('message', 'unknown reason')}")
            return False
        if message_type == "finished" and message.get("id") == self.helper_active_id:
            current = getattr(self, "_helper_current", None)
            if current is None:
                return False
            purpose, on_success, raw_completion, _stream_output = current
            output = "".join(self.helper_output_parts)
            returncode = int(message.get("returncode", 127))
            self.helper_active_id = None
            self.helper_output_parts = []
            self._helper_current = None
            self._command_finished(returncode, output, purpose, on_success, raw_completion)
            return False
        return False

    def _helper_start_failed(self, message: str) -> bool:
        self.helper_starting = False
        self.helper_ready = False
        pending = self.helper_pending_command
        self.helper_pending_command = None
        if pending is not None:
            _args, purpose, on_success, raw_completion, _stream = pending
            self._command_finished(127, message, purpose, on_success, raw_completion)
        else:
            self.show_error("Administrator access failed", message)
        return False

    def _helper_exited(self, returncode: int) -> bool:
        was_ready = self.helper_ready
        self.helper_ready = False
        self.helper_starting = False
        self.helper_process = None
        stderr = "".join(self.helper_stderr_parts).strip()
        self.helper_stderr_parts = []
        if self.busy and self.process_privileged:
            current = getattr(self, "_helper_current", None)
            pending = self.helper_pending_command
            self.helper_pending_command = None
            if current:
                purpose, on_success, raw_completion, _stream = current
            elif pending:
                _args, purpose, on_success, raw_completion, _stream = pending
            else:
                purpose, on_success, raw_completion = "operation", None, None
            self.helper_active_id = None
            self._helper_current = None
            self._command_finished(
                returncode or 127,
                stderr or "The administrator session ended before the operation completed.",
                purpose,
                on_success,
                raw_completion,
            )
        elif was_ready:
            self.append_log("Administrator session closed.")
        return False

    def _run_privileged_command(
        self,
        args: list[str],
        *,
        purpose: str,
        on_success: Callable[[str], None] | None,
        raw_completion: Callable[[int, str], None] | None,
        stream_output: bool,
    ) -> None:
        self._set_operation_started(purpose, True)
        self.helper_pending_command = (args, purpose, on_success, raw_completion, stream_output)
        if self.helper_ready:
            self._begin_helper_operation()
        else:
            self._start_privileged_helper()

    def _run_command(
        self,
        args: list[str],
        *,
        privileged: bool,
        purpose: str,
        on_success: Callable[[str], None] | None = None,
        raw_completion: Callable[[int, str], None] | None = None,
        stream_output: bool = True,
    ) -> None:
        if self.busy:
            return
        if privileged:
            self._run_privileged_command(
                args,
                purpose=purpose,
                on_success=on_success,
                raw_completion=raw_completion,
                stream_output=stream_output,
            )
            return

        self._set_operation_started(purpose, False)

        def worker() -> None:
            output_parts: list[str] = []
            try:
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                )
                self.process = process
                assert process.stdout is not None
                for line in process.stdout:
                    clean = line.rstrip("\n")
                    if clean.startswith("@@LIVE_MAP "):
                        GLib.idle_add(self._handle_engine_stream_line, clean)
                        continue
                    output_parts.append(line)
                    if stream_output:
                        GLib.idle_add(self.append_log, clean)
                returncode = process.wait()
            except Exception as exc:
                returncode = 127
                output_parts.append(str(exc))
            finally:
                self.process = None
            output = "".join(output_parts)
            GLib.idle_add(
                self._command_finished,
                returncode,
                output,
                purpose,
                on_success,
                raw_completion,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _pulse_progress(self) -> bool:
        if not self.busy:
            return False
        if not self.determinate_progress:
            self.progress.pulse()
        return True

    def _command_finished(
        self,
        returncode: int,
        output: str,
        purpose: str,
        on_success: Callable[[str], None] | None,
        raw_completion: Callable[[int, str], None] | None,
    ) -> bool:
        self.busy = False
        self.process_privileged = False
        self.stop_requested = False
        self.determinate_progress = False
        if self.pulse_id is not None:
            GLib.source_remove(self.pulse_id)
            self.pulse_id = None
        stopped_safely = returncode == 130
        self.progress.set_fraction(1.0 if returncode == 0 or stopped_safely else 0.0)
        self.progress.set_text(
            "Stopped safely" if stopped_safely else ("Complete" if returncode == 0 else "Failed")
        )
        self._update_controls()
        if raw_completion is not None:
            raw_completion(returncode, output)
        elif stopped_safely:
            display_name = {
                "analysis": "Analysis",
                "compact": "Compact",
                "defrag": "Defragment",
                "growth-defrag": "Growth Defrag",
                "recover": "Recovery",
            }.get(purpose, purpose.capitalize())
            self.append_log(
                f"{display_name} stopped safely. The active journalled transaction completed before exit."
            )
            self.status_label.set_text(f"{display_name} stopped safely.")
            self.post_analysis_status = f"{display_name} stopped safely · allocation map refreshed"
            self.post_analysis_progress_text = "Stopped safely"
            if on_success:
                on_success(output)
        elif returncode == 0:
            display_name = {
                "analysis": "Analysis",
                "compact": "Compact",
                "defrag": "Defragment",
                "growth-defrag": "Growth Defrag",
                "recover": "Recovery",
            }.get(purpose, purpose.capitalize())
            no_growth_changes = (
                purpose == "growth-defrag"
                and "Growth Defrag status:          Not needed;" in output
            )
            if no_growth_changes:
                self.progress.set_text("Not needed")
                self.status_label.set_text(
                    "Growth Defrag not needed; the existing layout already satisfies the 10% reserve."
                )
                self.post_analysis_status = (
                    "Growth Defrag not needed · existing 10% growth-space layout verified"
                )
                self.post_analysis_progress_text = "Not needed"
            else:
                self.status_label.set_text(f"{display_name} completed successfully.")
            if on_success:
                on_success(output)
        else:
            display_name = {
                "analysis": "Analysis",
                "compact": "Compact",
                "defrag": "Defragment",
                "growth-defrag": "Growth Defrag",
                "recover": "Recovery",
            }.get(purpose, purpose.capitalize())
            self.show_error(f"{display_name} failed", output.strip() or f"Exit status {returncode}")
        return False

    def _request_stop(self, _button: Gtk.Button) -> None:
        if not self.busy or self.stop_requested:
            return
        self.stop_requested = True
        self._update_controls()
        self.append_log("Stop requested. Waiting for the active journalled transaction to finish…")
        self.progress.set_text("Stopping after current transaction…")
        if self.process_privileged:
            try:
                self._helper_send({"action": "stop", "id": self.helper_request_id + 1})
            except Exception as exc:
                self.stop_requested = False
                self._update_controls()
                self.append_log(f"Unable to send stop request to the administrator session: {exc}")
            return
        process = self.process
        if process is None:
            self.stop_requested = False
            self._update_controls()
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            self.append_log("SIGINT delivered to the engine process group.")
        except ProcessLookupError:
            self.append_log("The engine process has already exited.")
        except PermissionError as exc:
            self.stop_requested = False
            self._update_controls()
            self.append_log(f"Unable to signal process group: {exc}")

    def _shutdown_helper(self, *_args: Any) -> None:
        process = self.helper_process
        if process is None or process.poll() is not None:
            return
        try:
            self._helper_send({"action": "quit"})
        except Exception:
            pass

    def _update_controls(self) -> None:
        volume = self.current_volume
        enabled = volume is not None and not self.busy
        mounted = bool(volume and volume.mounted)
        caps = volume.capabilities if volume else 0
        mutation_backend = bool(caps & (CAP_COMPACT | CAP_DEFRAG | CAP_GROWTH_DEFRAG | CAP_RECOVER))
        journal_exists = bool(mutation_backend and volume and Path(self.journal_path()).exists())
        self.refresh_button.set_sensitive(not self.busy)
        self.image_button.set_sensitive(not self.busy)
        self.device_combo.set_sensitive(not self.busy)
        self.analyze_button.set_sensitive(enabled and bool(caps & CAP_ANALYSE))
        self.unmount_button.set_sensitive(enabled and mounted and not bool(volume and volume.image))
        can_write = enabled and mutation_backend and not mounted and not bool(volume and volume.readonly)
        self.compact_button.set_sensitive(can_write and bool(caps & CAP_COMPACT) and not journal_exists)
        self.defrag_button.set_sensitive(can_write and bool(caps & CAP_DEFRAG) and not journal_exists)
        self.growth_button.set_sensitive(
            can_write and bool(caps & CAP_GROWTH_DEFRAG) and not journal_exists
        )
        self.recover_button.set_sensitive(can_write and bool(caps & CAP_RECOVER) and journal_exists)
        self.stop_button.set_sensitive(self.busy and not self.stop_requested)


class LinuxDefraggerApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=0)
        self.windows: list[MainWindow] = []

    def new_window(self) -> None:
        try:
            window = MainWindow(self)
        except Exception as exc:
            dialog = Gtk.MessageDialog(
                transient_for=None, modal=True, message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE, text="Unable to start Linux Defragger",
            )
            dialog.format_secondary_text(str(exc))
            dialog.run(); dialog.destroy()
            return
        self.windows.append(window)
        window.connect("destroy", lambda w: self.windows.remove(w) if w in self.windows else None)
        window.show_all()
        window.present()

    def do_activate(self) -> None:
        if not self.windows:
            self.new_window()
        else:
            self.windows[-1].present()


def main(argv: list[str] | None = None) -> int:
    app = LinuxDefraggerApplication()
    return app.run(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
