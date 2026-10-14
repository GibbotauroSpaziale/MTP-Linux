#!/usr/bin/env python3
"""MTP Reader — Browse and transfer files from a Sony ZV-E10 via FUSE mount."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QAbstractItemModel,
    QByteArray,
    QMimeData,
    QModelIndex,
    QSettings,
    QTimer,
    Qt,
    QThread,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

CAMERA_MIMETYPE = "application/x-gphoto-explorer"

# Default mount point for the ZV-E10 via gphotofs
DEFAULT_MOUNT = str(Path.home() / "MTP" / "ZV-E10")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CameraEntry:
    name: str
    path: str  # absolute path on the filesystem
    is_dir: bool
    size: int = 0
    mtime: str = ""
    display_name: str = ""  # optional label shown instead of `name`
    oversize: bool = False  # size saturated at the 32-bit MTP limit (>4 GiB)


@dataclass
class _TreeNode:
    """Lightweight tree node used for building the model off the main thread."""
    entry: CameraEntry
    children: list[_TreeNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Camera manager — reads directly from the FUSE mount
# ---------------------------------------------------------------------------

class CameraManager:
    """Browse and copy files from the FUSE-mounted camera."""

    # Sony USB vendor ID (ZV-E10, alpha series, RX series…)
    SONY_VENDOR_ID = "054c"

    # gphotofs/libmtp expose the 32-bit MTP field ObjectCompressedSize, which
    # saturates at this value for files ≥ 4 GiB: such files look like 4.00 GB
    # and copy out truncated. They are fetched with the `mtp-rs` CLI instead,
    # which uses the 64-bit PTP transfer path.
    OVERSIZE_SENTINEL = 0xFFFFFFFF

    # systemd *user* unit that provides the gphotofs mount. `mtp-rs` needs the
    # camera's USB interface, which gphotofs (libusb) holds while mounted, so
    # oversize transfers stop this unit first and start it again afterwards.
    MOUNT_SERVICE = "zve10-gphotofs.service"

    def __init__(self, mount_path: str = DEFAULT_MOUNT) -> None:
        self.mount_path = mount_path

    @staticmethod
    def _service_active() -> bool:
        """True if the gphotofs mount service is currently active."""
        try:
            return subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet",
                 CameraManager.MOUNT_SERVICE],
                timeout=10,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def mount_service_stop() -> bool:
        """Stop the gphotofs mount so `mtp-rs` can claim the camera.

        Returns True when the service was active (and therefore must be
        started again by the caller once the PTP transfers are done).
        """
        if not CameraManager._service_active():
            return False
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", CameraManager.MOUNT_SERVICE],
                timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return True

    @staticmethod
    def mount_service_start() -> None:
        """Start the gphotofs mount service again (best effort)."""
        try:
            subprocess.run(
                ["systemctl", "--user", "start", CameraManager.MOUNT_SERVICE],
                timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def usb_camera_present() -> bool:
        """True if any Sony camera is present on the USB bus (via sysfs).

        We read sysfs directly so we don't depend on lsusb being installed
        and we don't trust the possibly-stale FUSE mount.
        """
        # Find every USB device, check its vendor id, then look inside each
        # interface sub-directory ({port}:{iface}.0) for the interface class.
        # NOTE: the interface files live under the *interface* sub-dirs, so we
        # must glob with `*/bInterfaceClass` — a plain parent/bInterfaceClass
        # glob returns [] and would make every Sony camera appear absent.
        try:
            for ven_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
                try:
                    vid = Path(ven_path).read_text().strip().lower()
                except OSError:
                    continue
                if vid != CameraManager.SONY_VENDOR_ID:
                    continue
                dev_dir = Path(ven_path).parent
                for iface_path in glob.glob(f"{dev_dir}/*/bInterfaceClass"):
                    try:
                        cls = Path(iface_path).read_text().strip()
                    except OSError:
                        continue
                    # c06 = PTP imaging, c08 = MTP mass-storage
                    if cls.startswith("06") or cls.startswith("08"):
                        return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def camera_model(self) -> str:
        """Read the USB product string (e.g. "ZV-E10") from sysfs.

        Falls back to an empty string when the camera is not present or the
        product string can't be read (old kernels / cheap hubs).
        """
        try:
            for ven_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
                try:
                    vid = Path(ven_path).read_text().strip().lower()
                except OSError:
                    continue
                if vid != CameraManager.SONY_VENDOR_ID:
                    continue
                dev_dir = Path(ven_path).parent
                product_file = dev_dir / "product"
                if product_file.exists():
                    prod = product_file.read_text().strip()
                    if prod:
                        return prod
        except Exception:  # noqa: BLE001
            pass
        return ""

    def is_mounted(self) -> bool:
        """True if the FUSE mount directory exists and is an actual mount."""
        try:
            return os.path.isdir(self.mount_path) and os.path.ismount(self.mount_path)
        except Exception:  # noqa: BLE001
            return False

    def mount_healthy(self) -> bool:
        """True if the mount is usable — i.e. we can actually read it.

        gphotofs keeps a *stale* mount after the camera is unplugged: the
        directory still exists and ismount() still returns True, but any access
        raises OSError(Errno 5, I/O error). A healthy mount must pass both
        ismount() AND a real read.

        NOTE: listdir alone can be served by the FUSE page cache while statvfs
        still talks to the PTP daemon — a semi-stale session passes a bare
        listdir. So we require BOTH to work.
        """
        if not self.is_mounted():
            return False
        try:
            os.listdir(self.mount_path)
        except OSError:
            return False
        try:
            os.statvfs(self.mount_path)
            return True
        except OSError:
            return False

    def is_connected(self) -> bool:
        """True if a camera is physically present AND the mount is usable.

        A mounted-but-stale gphotofs session (camera unplugged) is NOT
        considered connected: ismounted alone can't tell, so we probe the
        mount with a real read.
        """
        if not self.usb_camera_present():
            return False
        return self.mount_healthy()

    @staticmethod
    def restart_mount_service() -> None:
        """Force a clean remount by restarting the gphotofs user service.

        This kills the stale PTP session (lazy unmount may have failed) so the
        next poll sees fresh contents instead of the old cache.
        """
        import subprocess

        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "zve10-gphotofs.service"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def stop_mount_service() -> None:
        """Try to unmount / stop the stale gphotofs session entirely."""
        import subprocess

        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "zve10-gphotofs.service"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass

    def connect(self) -> None:
        """Verify the mount is accessible. Raises RuntimeError if not."""
        if not os.path.isdir(self.mount_path):
            raise RuntimeError(
                f"Mount point {self.mount_path} does not exist.\n"
                "Connect your camera and ensure gphotofs has mounted it."
            )
        if not os.path.ismount(self.mount_path):
            raise RuntimeError(
                f"{self.mount_path} exists but is not a mount point.\n"
                "Ensure gphotofs has mounted the camera."
            )
        # Actually try to read the mount to verify it's usable
        try:
            os.listdir(self.mount_path)
        except OSError:
            raise RuntimeError(
                f"Mount point {self.mount_path} exists but is not accessible.\n"
                "The camera may have been disconnected."
            )

    def disconnect(self) -> None:
        """Nothing to disconnect — we just read files."""
        pass

    def list_folder_contents(self, path: str | None = None) -> list[CameraEntry]:
        """List immediate children (folders + files) of *path*."""
        if path is None:
            path = self.mount_path
        result: list[CameraEntry] = []
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return result
        for name in names:
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            size = 0
            mtime = ""
            try:
                st = os.stat(full)
                size = st.st_size
                mtime = str(int(st.st_mtime))
            except OSError:
                pass
            oversize = size >= CameraManager.OVERSIZE_SENTINEL
            result.append(
                CameraEntry(
                    name=name,
                    path=full,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime,
                    oversize=oversize,
                )
            )
        return result

    def build_tree(self, path: str | None = None, parent: _TreeNode | None = None) -> _TreeNode:
        """Recursively build a tree of _TreeNode from the mounted filesystem.

        Folder entries get `size` = total size of their contents (recursive),
        so columns and info panels can show how heavy a folder is.
        """
        if path is None:
            path = self.mount_path
        if parent is None:
            parent = _TreeNode(entry=CameraEntry(name=Path(path).name, path=path, is_dir=True))

        # If this is the top level of the mount, label the SD-card folder with
        # the camera model (e.g. "ZV-E10") instead of the raw "store_00010001".
        model = self.camera_model()
        is_top_level = Path(path) == Path(self.mount_path)

        entries = self.list_folder_contents(path)
        folders = [e for e in entries if e.is_dir]
        files = [e for e in entries if not e.is_dir]
        files.sort(key=lambda e: e.mtime, reverse=True)

        total_size = 0
        for entry in folders + files:
            if is_top_level and entry.is_dir and model:
                entry.display_name = f"{model}"
            child = _TreeNode(entry=entry)
            parent.children.append(child)
            if entry.is_dir:
                self.build_tree(entry.path, child)
                total_size += child.entry.size
            else:
                total_size += entry.size

        # Aggrega: la size della cartella corrente = somma dei contenuti
        if parent.entry is not None:
            parent.entry.size = total_size

        return parent

    # --- >4 GiB transfers via mtp-rs -------------------------------------

    @staticmethod
    def _which_mtp_rs() -> str | None:
        """Locate the `mtp-rs` CLI, or return None if it isn't installed.

        Looks it up in PATH first, then in the usual user-local location
        (~/.local/bin), which is not always exported to GUI sessions.
        """
        exe = shutil.which("mtp-rs")
        if exe:
            return exe
        fallback = Path.home() / ".local" / "bin" / "mtp-rs"
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
        return None

    def mtp_rs_remote_path(self, src_path: str) -> str | None:
        """Translate a mount path into the MTP path used by `mtp-rs`.

        gphotofs exposes the storage as the first folder under the mount point
        (e.g. `~/MTP/ZV-E10/store_00010001/2026-10-14/C3248.MP4`), while
        `mtp-rs` addresses objects below the default storage
        (`/2026-10-14/C3248.MP4`) — so the storage component is dropped.

        Returns None when *src_path* isn't under the mount or has no object
        part to transfer.
        """
        try:
            rel = os.path.relpath(src_path, self.mount_path)
        except ValueError:  # different drives (Windows) — be safe
            return None
        if rel.startswith(".."):
            return None
        parts = Path(rel).parts
        if len(parts) < 2:  # needs at least <storage>/<object>
            return None
        return "/" + "/".join(parts[1:])

    @classmethod
    def _is_oversize(cls, src_path: str) -> bool:
        """True when the file's reported size hit the 32-bit MTP ceiling."""
        try:
            return os.stat(src_path).st_size >= cls.OVERSIZE_SENTINEL
        except OSError:
            return False

    @classmethod
    def _get_file_mtp_rs(
        cls, exe: str, remote: str, dest_path: str, abort_check=None
    ) -> None:
        """Download one remote object with the `mtp-rs` CLI.

        Runs as a subprocess; when *abort_check* is given it is polled while
        the transfer runs so the copy can be cancelled from the UI.

        The caller is responsible for releasing the camera first (see
        `mount_service_stop`): `gphotofs` and `mtp-rs` cannot hold the USB
        interface at the same time.
        """
        proc = subprocess.Popen(
            [exe, "get", "--replace", remote, dest_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ok = False
        try:
            while True:
                try:
                    out, _ = proc.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if abort_check is not None and abort_check():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise RuntimeError("Transfer cancelled")
            if proc.returncode != 0:
                lines = (out or "").strip().splitlines()
                detail = lines[-1] if lines else f"exit code {proc.returncode}"
                raise RuntimeError(f"mtp-rs: {detail}")
            ok = True
        finally:
            if proc.poll() is None:
                proc.kill()
            if not ok:
                # Never leave a truncated file behind on abort/failure.
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except OSError:
                    pass

    def get_file(self, src_path: str, dest_path: str, abort_check=None) -> None:
        """Copy a single file from the camera mount to *dest_path*.

        Files at the 32-bit MTP ceiling (≥ 4 GiB) are fetched with `mtp-rs` so
        they arrive intact; anything else uses the plain FUSE copy.
        """
        if self._is_oversize(src_path):
            exe = self._which_mtp_rs()
            remote = self.mtp_rs_remote_path(src_path)
            if exe is None or remote is None:
                raise RuntimeError(
                    f"File più grande di 4 GiB: serve `mtp-rs` per {Path(src_path).name} "
                    "(installalo in ~/.local/bin/mtp-rs)."
                )
            self._get_file_mtp_rs(exe, remote, dest_path, abort_check=abort_check)
            return
        shutil.copy2(src_path, dest_path)

    def storage_info(self) -> tuple[int | None, int | None, int | None]:
        """Return (free_bytes, used_bytes, total_bytes) via statvfs on the mount.

        Returns (None, None, None) if the mount is not usable.
        """
        try:
            v = os.statvfs(self.mount_path)
            free = v.f_frsize * v.f_bavail
            total = v.f_frsize * v.f_blocks
            used = total - v.f_frsize * v.f_bfree
            return free, used, total
        except Exception:  # noqa: BLE001
            return None, None, None


# ---------------------------------------------------------------------------
# Tree model for camera filesystem (main-thread only)
# ---------------------------------------------------------------------------

class CameraTreeModel(QAbstractItemModel):
    """Read-only tree model built from a flat list of CameraEntry items."""

    COLUMNS = ("Name", "Size")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Each node is a dict: {entry, parent_idx, children_indices}
        self._nodes: list[dict] = []
        self._root_idx = -1  # index in _nodes for the root

    def load_from_tree(self, tree: _TreeNode) -> None:
        """Replace the model contents from a _TreeNode hierarchy (built off-thread)."""
        self.beginResetModel()
        self._nodes.clear()
        self._root_idx = self._build_flat(tree, -1)
        self.endResetModel()

    def _build_flat(self, node: _TreeNode, parent_idx: int) -> int:
        """Flatten _TreeNode into self._nodes list, returning index of created node."""
        idx = len(self._nodes)
        self._nodes.append({
            "entry": node.entry,
            "parent_idx": parent_idx,
            "children": [],
        })
        if parent_idx >= 0:
            self._nodes[parent_idx]["children"].append(idx)
        for child_node in node.children:
            child_idx = self._build_flat(child_node, idx)
        return idx

    def _entry(self, idx: int) -> CameraEntry:
        return self._nodes[idx]["entry"]

    # --- Qt model interface ---

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node_idx = parent.internalId() if parent.isValid() else self._root_idx
        children = self._nodes[parent_node_idx]["children"]
        if 0 <= row < len(children):
            return self.createIndex(row, column, children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node_idx = int(index.internalId())
        parent_idx = self._nodes[node_idx]["parent_idx"]
        if parent_idx < 0 or parent_idx == self._root_idx:
            return QModelIndex()
        # Find our row within parent's children list
        parent_children = self._nodes[parent_idx]["children"]
        try:
            row = parent_children.index(node_idx)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, parent_idx)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        node_idx = parent.internalId() if parent.isValid() else self._root_idx
        if node_idx < 0 or node_idx >= len(self._nodes):
            return 0
        return len(self._nodes[node_idx]["children"])

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        node_idx = int(index.internalId())
        entry = self._nodes[node_idx]["entry"]
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                label = entry.display_name or entry.name
                if entry.is_dir:
                    return "\U0001f4c1 " + label
                return _file_icon(entry.name) + " " + label
            if index.column() == 1:
                if entry.is_dir:
                    return _format_size(entry.size) if entry.size else ""
                if entry.oversize:
                    return "\u2265 4 GiB \u00b7 mtp-rs"
                return _format_size(entry.size)
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.path
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    # --- drag ---

    def mimeTypes(self) -> list[str]:
        mtypes = [CAMERA_MIMETYPE, "text/uri-list"]
        return mtypes

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        from PyQt6.QtCore import QUrl

        # Emette gli items selezionati così come sono (file E cartelle). Le
        # cartelle NON vengono appiattite: chi riceve (DropZone → thread di
        # copia) ricrea la struttura, così "trascino 2026-08-31" copia la
        # cartella con i suoi file, non i file sparsi.
        lines: list[str] = []
        urls: list[QUrl] = []
        seen: set[str] = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            node_idx = int(idx.internalId())
            entry = self._nodes[node_idx]["entry"]
            if entry.path not in seen:
                seen.add(entry.path)
                lines.append(entry.path)
                urls.append(QUrl.fromLocalFile(entry.path))
        mime = QMimeData()
        mime.setData(CAMERA_MIMETYPE, "\n".join(lines).encode("utf-8"))
        mime.setUrls(urls)  # text/uri-list per il drag verso Dolphin/altri FM
        return mime


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

# Theme-aware stylesheet (Breeze/Fusion compatible — uses palette() roles so it
# works on both dark and light Plasma themes; only the accent is fixed).
APP_QSS = """
QToolBar {
    background: transparent;
    border: none;
    border-bottom: 1px solid palette(mid);
    padding: 4px 6px;
    spacing: 4px;
}
QToolBar::separator {
    width: 1px;
    background: palette(mid);
    margin: 4px 8px;
}
QToolButton {
    border-radius: 4px;
    padding: 5px 10px;
    border: 1px solid transparent;
}
QToolButton:hover {
    background: rgba(61,174,233,0.14);
    border: 1px solid rgba(61,174,233,0.35);
}
QToolButton:pressed {
    background: rgba(61,174,233,0.28);
}
QToolButton:focus {
    border: 2px solid #3daee9;
    outline: none;
}
QToolButton:disabled {
    color: palette(disabled-text);
}
QTreeView {
    background: transparent;
    alternate-background-color: rgba(61,174,233,0.05);
    border: none;
    show-decoration-selected: 1;
    selection-background-color: #2b5f7a;
    selection-color: #fcfcfc;
    padding: 2px;
}
QTreeView::item {
    border-radius: 3px;
    padding: 4px 6px;
    min-height: 24px;
}
QTreeView::item:hover {
    background: rgba(61,174,233,0.12);
}
QTreeView::item:selected {
    background: #2b5f7a;
    color: #fcfcfc;
}
QTreeView::branch {
    background: transparent;
}
QHeaderView::section {
    background: palette(button);
    color: palette(text);
    border: none;
    border-bottom: 1px solid palette(mid);
    border-right: 1px solid palette(mid);
    padding: 6px 8px;
    font-weight: 600;
}
QLineEdit {
    padding: 6px 8px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    background: palette(base);
    selection-background-color: #3daee9;
    selection-color: #fcfcfc;
}
QLineEdit:focus {
    border: 2px solid #3daee9;
    padding: 5px 7px;
}
QPushButton {
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid palette(mid);
    background: palette(button);
    color: palette(text);
    min-height: 30px;
}
QPushButton:hover {
    background: rgba(61,174,233,0.12);
}
QPushButton:pressed {
    background: rgba(61,174,233,0.25);
}
QPushButton:focus {
    border: 2px solid #3daee9;
    outline: none;
    padding: 5px 13px;
}
QPushButton:disabled {
    color: palette(disabled-text);
}
QStatusBar {
    background: transparent;
    border-top: 1px solid palette(mid);
    padding: 4px 6px;
    color: palette(text);
}
QStatusBar QLabel {
    color: palette(text);
    padding-right: 8px;
}
QProgressBar {
    background: palette(midlight);
    border: 1px solid palette(mid);
    border-radius: 4px;
    min-height: 14px;
    max-height: 14px;
    text-align: center;
    color: palette(text);
}
QProgressBar::chunk {
    background: #3daee9;
    border-radius: 3px;
}
#dropZone {
    background: rgba(61,174,233,0.04);
    border: 2px dashed rgba(61,174,233,0.5);
    border-radius: 8px;
    margin: 6px;
}
#dropZone QLabel {
    color: palette(text);
}
#dropZone[dragActive="true"] {
    background: rgba(61,174,233,0.16);
    border: 2px solid #3daee9;
}
#storageBox {
    background: rgba(61,174,233,0.06);
    border: 1px solid palette(mid);
    border-radius: 6px;
}
#usedBar {
    background: palette(midlight);
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
}
#usedBar::chunk {
    background: #3daee9;
    border-radius: 4px;
}
"""

def _theme_icon(names: list[str]) -> QIcon:
    """First icon key present in the active theme, with an emoji fallback."""
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QIcon.fromTheme("dialog-information")


def _file_icon(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".mp4", ".mov", ".m4v"):
        return "\U0001f3ac"
    if ext in (".jpg", ".jpeg", ".arw", ".heic", ".heif", ".png", ".raf", ".cr3", ".dng"):
        return "\U0001f4f7"
    return "\U0001f4c4"


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------------

class DetectCameraThread(QThread):
    """Check if the camera mount is available in a background thread."""

    result_ready = pyqtSignal(bool, str)  # success, message

    def __init__(self, mgr: CameraManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mgr = mgr

    def run(self) -> None:
        try:
            self._mgr.connect()
            self.result_ready.emit(True, "Camera connected")
        except RuntimeError as exc:
            self.result_ready.emit(False, str(exc))


class LoadTreeThread(QThread):
    """Load the camera file tree in a background thread, then emit the result."""

    tree_ready = pyqtSignal(object)  # _TreeNode
    error_occurred = pyqtSignal(str)  # error message

    def __init__(self, mgr: CameraManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mgr = mgr

    def run(self) -> None:
        try:
            tree = self._mgr.build_tree()
            self.tree_ready.emit(tree)
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit(str(exc))


class CopyFilesThread(QThread):
    """Copy files from camera to local disk in a background thread."""

    progress = pyqtSignal(int, int, str)  # current, total, filename
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(
        self,
        mgr: CameraManager,
        files: list[str],  # absolute source paths
        dest_dir: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mgr = mgr
        self._files = files
        self._dest_dir = dest_dir
        self._abort = False
        self._ok = 0
        self._errors: list[str] = []

    def abort(self) -> None:
        self._abort = True

    def _prepare_targets(self) -> list[tuple[str, str]]:
        """Expand the selection into (src, dest) pairs, ready to copy.

        Selecting a folder recreates that folder under the destination
        (dropping ``2026-08-31`` yields ``<dest>/2026-08-31/<file>``). Any
        destination that already exists gets a numeric suffix so nothing is
        overwritten, and destination folders are created up front — before the
        mount is taken down for the oversize transfers.
        """
        pairs: list[tuple[str, str]] = []
        for src in self._files:
            if os.path.isdir(src):
                base = Path(src).name
                for root, _dirs, names in os.walk(src):
                    for name in names:
                        full = os.path.join(root, name)
                        rel = os.path.join(base, os.path.relpath(full, src))
                        pairs.append((full, rel))
            else:
                pairs.append((src, Path(src).name))

        targets: list[tuple[str, str]] = []
        for src, rel in pairs:
            dest = os.path.join(self._dest_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                d = os.path.dirname(dest)
                stem = Path(rel).stem
                ext = Path(rel).suffix
                counter = 1
                while os.path.exists(dest):
                    dest = os.path.join(d, f"{stem}_{counter}{ext}")
                    counter += 1
            targets.append((src, dest))
        return targets

    def _copy_one(self, idx: int, total: int, src: str, dest: str) -> bool:
        """Copy one prepared file, recording success/errors on ``self``.

        Returns False only when the transfer was cancelled; a per-file error is
        recorded in ``self._errors`` and returns True so the batch continues.
        """
        self.progress.emit(idx, total, Path(src).name)
        try:
            self._mgr.get_file(src, dest, abort_check=lambda: self._abort)
            self._ok += 1
            return True
        except Exception as exc:  # noqa: BLE001
            if self._abort:
                return False
            self._errors.append(f"{Path(src).name}: {exc}")
            return True

    def run(self) -> None:
        self._ok = 0
        self._errors = []
        targets = self._prepare_targets()
        total = len(targets)

        # Split up front: oversize detection stats the file on the live mount,
        # and oversize transfers need that mount taken down, so normal files
        # are copied first while the FUSE mount is still up.
        plain = [(s, d) for s, d in targets if not self._mgr._is_oversize(s)]
        oversize = [(s, d) for s, d in targets if self._mgr._is_oversize(s)]

        for idx, (src, dest) in enumerate(plain):
            if self._abort or not self._copy_one(idx, total, src, dest):
                self.finished.emit(False, "Transfer cancelled")
                return

        if oversize:
            # gphotofs holds the camera's USB interface; release it so `mtp-rs`
            # (PTP, 64-bit sizes) can fetch the >4 GiB files, then put it back.
            mounted = CameraManager.mount_service_stop()
            try:
                for offset, (src, dest) in enumerate(oversize):
                    idx = len(plain) + offset
                    if self._abort or not self._copy_one(idx, total, src, dest):
                        self.finished.emit(False, "Transfer cancelled")
                        return
            finally:
                if mounted:
                    CameraManager.mount_service_start()

        self.progress.emit(total, total, "")
        if self._errors:
            self.finished.emit(False, f"Copied {self._ok}/{total} files.\nErrors:\n" + "\n".join(self._errors[:10]))
        else:
            self.finished.emit(True, f"Successfully copied {self._ok} file(s).")


# ---------------------------------------------------------------------------
# Drop zone widget
# ---------------------------------------------------------------------------

class DropZone(QWidget):
    """Widget that accepts internal drops and shows the destination folder."""

    files_dropped = pyqtSignal(list)  # list of absolute paths

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._dest: str = str(Path.home() / "Downloads")
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setObjectName("dropZone")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        icon = _theme_icon(["folder-drag-accept", "folder-open", "folder"])
        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if icon.isNull():
            self._icon_label.setText("\U0001f4c2")
            font = self._icon_label.font()
            font.setPointSize(40)
            self._icon_label.setFont(font)
        else:
            self._icon_label.setPixmap(icon.pixmap(48, 48))
        layout.addWidget(self._icon_label)

        self._label = QLabel("Copy files to")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(self._label)

        self._path_edit = QLineEdit(self._dest)
        self._path_edit.setPlaceholderText("Destination folder\u2026")
        self._path_edit.setToolTip("Destination folder for the copied files")
        layout.addWidget(self._path_edit)

        row = QHBoxLayout()
        self._browse_btn = QPushButton(
            _theme_icon(["folder-open", "document-open"]), "Browse\u2026"
        )
        self._browse_btn.setToolTip("Choose a destination folder")
        row.addWidget(self._browse_btn)
        row.addStretch()
        layout.addLayout(row)

        # Spinge lo storage in basso: il box resta sempre visibile.
        layout.addStretch(1)

        # --- Info storage camera + cartella selezionata ---
        self._storage_box = QFrame()
        self._storage_box.setObjectName("storageBox")
        box_layout = QVBoxLayout(self._storage_box)
        box_layout.setContentsMargins(10, 8, 10, 8)
        box_layout.setSpacing(4)

        self._storage_title = QLabel("\U0001f4be Camera storage")
        self._storage_title.setStyleSheet("font-weight: 600;")
        box_layout.addWidget(self._storage_title)

        # Free / Total in una sola riga — il totale è sempre visibile.
        self._free_total_label = QLabel("Free: \u2014 \u00b7 Total: \u2014")
        box_layout.addWidget(self._free_total_label)

        # Barra di occupazione (Used/Total).
        self._used_bar = QProgressBar()
        self._used_bar.setObjectName("usedBar")
        self._used_bar.setRange(0, 100)
        self._used_bar.setValue(0)
        self._used_bar.setTextVisible(False)
        self._used_bar.setFixedHeight(8)
        self._used_bar.setToolTip("Camera storage used")
        box_layout.addWidget(self._used_bar)

        self._used_label = QLabel("Used: \u2014")
        self._used_label.setStyleSheet("color: palette(secondary-text); font-size: 12px;")
        box_layout.addWidget(self._used_label)

        self._selected_label = QLabel("Selected: \u2014")
        self._selected_label.setStyleSheet(
            "border-top: 1px solid palette(mid); padding-top: 6px; margin-top: 4px;"
        )
        box_layout.addWidget(self._selected_label)

        layout.addWidget(self._storage_box)

    def update_storage(
        self,
        free: int | None,
        used: int | None,
        total: int | None,
        selected_size: int | None = None,
        selected_name: str | None = None,
        selected_count: int | None = None,
    ) -> None:
        """Aggiorna il riquadro storage con i valori della camera."""
        if free is None or total is None:
            self._free_total_label.setText("Free: \u2014 \u00b7 Total: \u2014")
            self._used_bar.setValue(0)
            self._used_bar.setToolTip("")
            self._used_label.setText("Used: \u2014")
        else:
            used = used if used is not None else total - free
            self._free_total_label.setText(
                f"Free: {_format_size(free)} \u00b7 Total: {_format_size(total)}"
            )
            pct = round(100 * used / total) if total else 0
            self._used_bar.setValue(pct)
            self._used_bar.setToolTip(
                f"{pct}% of camera storage used "
                f"({_format_size(used)} / {_format_size(total)})"
            )
            self._used_label.setText(f"Used: {_format_size(used)} ({pct}%)")
        if selected_size is not None:
            if selected_count is not None:
                self._selected_label.setText(
                    f"{selected_name or 'Selection'}: {_format_size(selected_size)} "
                    f"\u00b7 {selected_count} file(s)"
                )
            else:
                self._selected_label.setText(
                    f"{selected_name or 'Selection'}: {_format_size(selected_size)}"
                )
        else:
            self._selected_label.setText("Selected: \u2014")

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        # Force a re-polish so the QSS `#dropZone[dragActive="true"]` applies.
        self.style().unpolish(self)
        self.style().polish(self)

    @property
    def destination(self) -> str:
        return self._path_edit.text().strip() or self._dest

    @destination.setter
    def destination(self, path: str) -> None:
        self._dest = path
        self._path_edit.setText(path)

    # --- drag/drop ---

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if mime is not None and (mime.hasFormat(CAMERA_MIMETYPE) or mime.hasUrls()):
            event.acceptProposedAction()
            self._set_drag_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:  # noqa: ANN401
        self._set_drag_active(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        mime = event.mimeData()
        paths: list[str] = []
        if mime is not None and mime.hasFormat(CAMERA_MIMETYPE):
            raw = bytes(mime.data(CAMERA_MIMETYPE)).decode("utf-8")
            for line in raw.strip().splitlines():
                line = line.strip()
                if line:
                    paths.append(line)
        elif mime is not None and mime.hasUrls():
            from PyQt6.QtCore import QUrl

            for url in mime.urls():
                if url.scheme() == "file":
                    paths.append(url.toLocalFile())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MTP Reader")
        self.setMinimumSize(900, 600)

        # Icona personalizzata (cercata prima in locale, poi nel tema)
        for icon_path in (
            Path.home() / ".local/share/icons/hicolor/256x256/apps/mtp-reader.svg",
            Path.home() / "gphoto-explorer/icon.png",
        ):
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
                break
        else:
            icon = QIcon.fromTheme("camera-photo")
            if not icon.isNull():
                self.setWindowIcon(icon)

        self._settings = QSettings("MTPReader", "MTPReader")
        self._camera_mgr = CameraManager()
        self._copy_thread: CopyFilesThread | None = None
        self._detect_retries = 0

        self._setup_ui()
        self._restore_geometry()

        # Monitora lo stato del mount: se si scollega svuota l'albero,
        # se si ricollega ricarica automaticamente.
        self._previous_mount_state: bool | None = None
        self._mount_recover_attempts = 0
        self._is_loading = False
        self._mount_poll = QTimer(self)
        self._mount_poll.setInterval(2000)
        self._mount_poll.timeout.connect(self._check_mount)
        self._mount_poll.start()

        # Auto-detect camera on startup
        self._detect_camera()

    # ------------------------------------------------------------------ mount polling

    def _check_mount(self) -> None:
        try:
            mounted = self._camera_mgr.is_connected()
        except Exception:  # noqa: BLE001
            mounted = False

        # Non interferire se un caricamento o una copia è in corso: restartare
        # adesso causerebbe un loop (mount smontato -> sembra disconnected -> ...).
        if self._is_loading or (self._copy_thread and self._copy_thread.isRunning()):
            if self._previous_mount_state is None:
                self._previous_mount_state = mounted
            return

        if self._previous_mount_state is None:
            self._previous_mount_state = mounted

        if not mounted and self._previous_mount_state is True:
            # Transition: camera disconnected → kill the stale PTP session so
            # the old cache disappears and reconnection gets a fresh mount.
            self._previous_mount_state = False
            self._status_label.setText("Camera disconnected \u2014 cleaning up session\u2026")
            self._camera_mgr.stop_mount_service()
            empty_root = _TreeNode(entry=CameraEntry(name="Camera", path="/", is_dir=True))
            self._model.load_from_tree(empty_root)
            self._empty_state.setText(
                "\U0001f4f7\nCamera disconnected\n\n"
                "Plug your camera in \u2014 MTP Reader will reload automatically"
            )
            self._tree_stack.setCurrentIndex(1)
            self._drop_zone.update_storage(None, None, None)
            return

        if mounted and self._previous_mount_state is False:
            # Transition: camera reconnected → force a clean remount so we read
            # the NEW contents, not the stale gphotofs cache.
            self._previous_mount_state = True
            self._status_label.setText("Camera reconnected \u2014 reloading\u2026")
            self._camera_mgr.restart_mount_service()
            self._load_tree()
            return

        if not mounted and self._previous_mount_state is False:
            # Camera fisicamente presente (o mount assente o zombie/stale):
            # prova a rimontare da solo se il device PTP c'è ma il mount non è
            # utilizzabile. mount_healthy() copre anche la sessione gphotofs
            # stale (mount esiste e ismount=True ma qualsiasi lettura fallisce).
            if self._camera_mgr.usb_camera_present() and not self._camera_mgr.mount_healthy():
                self._mount_recover_attempts += 1
                if self._mount_recover_attempts >= 2:  # ogni ~4s
                    self._mount_recover_attempts = 0
                    self._status_label.setText("Camera found \u2014 mounting\u2026")
                    self._camera_mgr.restart_mount_service()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._refresh_action = toolbar.addAction(
            _theme_icon(["view-refresh", "view-refresh-all"]), "Refresh Camera"
        )
        self._refresh_action.setToolTip(
            "Reload the camera contents (connect your camera first)"
        )
        self._refresh_action.triggered.connect(self._detect_camera)

        toolbar.addSeparator()

        self._copy_selected_action = toolbar.addAction(
            _theme_icon(["document-save-as", "edit-copy", "document-save"]),
            "Copy Selected to\u2026",
        )
        self._copy_selected_action.setToolTip(
            "Copy the selected files/folders to a destination folder"
        )
        self._copy_selected_action.triggered.connect(self._copy_selected_to)

        # Splitter: tree | drop zone
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_layout.addWidget(splitter, stretch=1)

        # Left — tree view
        self._tree = QTreeView()
        self._tree.setHeaderHidden(False)
        self._tree.setDragEnabled(True)
        self._tree.setDragDropMode(QTreeView.DragDropMode.DragOnly)
        self._tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._tree.setAnimated(True)
        self._tree.setExpandsOnDoubleClick(True)
        self._tree.setSortingEnabled(False)
        self._tree.setRootIsDecorated(True)
        self._tree.setAccessibleName("Camera file tree")

        self._model = CameraTreeModel(self)
        self._tree.setModel(self._model)

        # Header: va configurato DOPO setModel(), altrimenti il resize mode
        # viene scartato (l'header senza colonne lo ignora).
        header = self._tree.header()
        header.setStretchLastSection(False)
        # Name (col 0) occupa tutto lo spazio disponibile; Size (col 1) resta
        # stretto ma ridimensionabile dall'utente.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 110)
        header.setMinimumSectionSize(80)

        # Empty-state placeholder over the tree (no camera connected)
        self._empty_state = QLabel(
            "\U0001f4f7\nNo camera connected\n\n"
            "Connect your camera and click Refresh Camera"
        )
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setStyleSheet("font-size: 15px; color: palette(disabled-text);")
        self._empty_state.setWordWrap(True)
        self._empty_state.setVisible(False)

        # Stacked container: [0] = tree, [1] = empty state
        self._tree_stack = QStackedWidget()
        self._tree_stack.addWidget(self._tree)
        self._tree_stack.addWidget(self._empty_state)
        self._tree_stack.setCurrentIndex(0)
        splitter.addWidget(self._tree_stack)

        # Right — drop zone
        self._drop_zone = DropZone(splitter)
        splitter.setSizes([500, 400])

        self._drop_zone._browse_btn.clicked.connect(self._browse_destination)
        self._drop_zone.files_dropped.connect(self._on_files_dropped)
        self._tree.selectionModel().selectionChanged.connect(self._on_selection_changed)
        # Ricorda l'ultima cartella di destinazione e salva le modifiche.
        saved_dest = self._settings.value("dest_folder", "")
        if saved_dest and os.path.isdir(saved_dest):
            self._drop_zone.destination = str(saved_dest)
        self._drop_zone._path_edit.editingFinished.connect(self._save_destination)

        # Status bar with progress
        self._status_label = QLabel("Ready")
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(300)
        self._progress.setVisible(False)
        self.statusBar().addPermanentWidget(self._status_label, stretch=1)
        self.statusBar().addPermanentWidget(self._progress)

    # ------------------------------------------------------------------ geometry

    def _restore_geometry(self) -> None:
        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1000, 650)

    def closeEvent(self, event: Any) -> None:  # noqa: ANN401
        self._settings.setValue("geometry", self.saveGeometry())
        if self._copy_thread and self._copy_thread.isRunning():
            self._copy_thread.abort()
            self._copy_thread.wait(3000)
        self._camera_mgr.disconnect()
        super().closeEvent(event)

    # ------------------------------------------------------------------ camera

    def _detect_camera(self) -> None:
        self._status_label.setText("Detecting camera\u2026")
        self._refresh_action.setEnabled(False)
        thread = DetectCameraThread(self._camera_mgr, self)
        thread.result_ready.connect(self._on_camera_detected)
        # prevent premature GC
        thread.finished.connect(thread.deleteLater)
        self._detect_thread = thread  # type: ignore[attr-defined]
        thread.start()

    @pyqtSlot(bool, str)
    def _on_camera_detected(self, ok: bool, message: str) -> None:
        self._refresh_action.setEnabled(True)
        if not ok:
            # Retry a few times on startup: the gphotofs mount may not be
            # ready yet when the app is launched automatically by udev.
            if self._detect_retries < 6:
                self._detect_retries += 1
                self._status_label.setText(f"Waiting for camera ({self._detect_retries}/6)\u2026")
                QTimer.singleShot(1000, self._detect_camera)
                return
            self._detect_retries = 0
            self._status_label.setText("No camera detected")
            # Clear the tree so stale entries don't linger
            empty_root = _TreeNode(entry=CameraEntry(name="Camera", path="/", is_dir=True))
            self._model.load_from_tree(empty_root)
            self._empty_state.setText(
                "\U0001f4f7\nNo camera connected\n\n"
                "Connect your camera and ensure gphotofs has mounted it.\n"
                "MTP Reader will reload automatically."
            )
            self._tree_stack.setCurrentIndex(1)
            self._drop_zone.update_storage(None, None, None)
            QMessageBox.information(
                self,
                "MTP Reader",
                "No camera detected.\n\n"
                "Connect your camera and ensure gphotofs has mounted it.\n"
                "Then click Refresh.",
            )
            return
        self._detect_retries = 0
        self._status_label.setText("Reloading camera contents\u2026")
        # Remount pulito SOLO se serve: se il mount è già valido non smontare
        # (evita di perdere la camera mentre si ricarica).
        if not self._camera_mgr.is_connected():
            self._camera_mgr.restart_mount_service()
        self._load_tree()

    def _load_tree(self) -> None:
        self._refresh_action.setEnabled(False)
        self._is_loading = True
        thread = LoadTreeThread(self._camera_mgr, self)
        thread.tree_ready.connect(self._on_tree_ready)
        thread.error_occurred.connect(self._on_tree_error)
        thread.finished.connect(thread.deleteLater)
        self._load_thread = thread  # type: ignore[attr-defined]
        thread.start()

    @pyqtSlot(object)
    def _on_tree_ready(self, tree: _TreeNode) -> None:
        self._is_loading = False
        self._refresh_action.setEnabled(True)
        self._previous_mount_state = True
        self._model.load_from_tree(tree)
        self._tree_stack.setCurrentIndex(0)
        top_count = self._model.rowCount()
        self._status_label.setText(f"Camera connected \u2014 {top_count} top-level item(s)")
        self._update_storage()
        # Expand the first level
        for row in range(top_count):
            child = self._model.index(row, 0)
            if child.isValid():
                self._tree.setExpanded(child, True)

    @pyqtSlot(str)
    def _on_tree_error(self, message: str) -> None:
        self._is_loading = False
        self._refresh_action.setEnabled(True)
        self._status_label.setText("Error loading tree")
        QMessageBox.warning(self, "Error", f"Failed to load camera contents:\n{message}")

    # ------------------------------------------------------------------ drag & drop

    @pyqtSlot(list)
    def _on_files_dropped(self, files: list[str]) -> None:
        dest = self._drop_zone.destination
        if not dest:
            QMessageBox.warning(self, "Error", "No destination folder set.")
            return
        os.makedirs(dest, exist_ok=True)
        self._start_copy(files, dest)

    def _copy_selected_to(self) -> None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(self, "MTP Reader", "No files selected in the tree.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Select destination folder", self._drop_zone.destination)
        if not dest:
            return
        self._drop_zone.destination = dest
        self._start_copy(items, dest)

    def _selected_items(self) -> list[str]:
        """Absolute source paths of the selected row(s) — files AND folders.

        Folders are NOT flattened: the copy thread recreates their structure,
        so "Copy 2026-08-31" copies the folder with its files inside.
        """
        result: list[str] = []
        seen: set[str] = set()
        for idx in self._tree.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            node_idx = int(idx.internalId())
            entry = self._model._nodes[node_idx]["entry"]
            if entry.path not in seen:
                seen.add(entry.path)
                result.append(entry.path)
        return result

    def _selected_camera_files(self) -> list[str]:
        """Return list of absolute source paths for selected non-directory items."""
        result: list[str] = []
        seen: set[str] = set()
        for idx in self._tree.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            node_idx = int(idx.internalId())
            entry = self._model._nodes[node_idx]["entry"]
            if entry.is_dir:
                self._collect_files_recursive(node_idx, result, seen)
            else:
                if entry.path not in seen:
                    seen.add(entry.path)
                    result.append(entry.path)
        return result

    def _collect_files_recursive(
        self,
        node_idx: int,
        out: list[str],
        seen: set[str],
    ) -> None:
        entry = self._model._nodes[node_idx]["entry"]
        if not entry.is_dir:
            if entry.path not in seen:
                seen.add(entry.path)
                out.append(entry.path)
            return
        for child_idx in self._model._nodes[node_idx]["children"]:
            self._collect_files_recursive(child_idx, out, seen)

    # ------------------------------------------------------------------ copy

    def _start_copy(self, files: list[str], dest: str) -> None:
        if self._copy_thread and self._copy_thread.isRunning():
            QMessageBox.warning(self, "Busy", "A transfer is already in progress. Please wait.")
            return
        self._progress.setVisible(True)
        self._progress.setMaximum(len(files))
        self._progress.setValue(0)
        self._copy_selected_action.setEnabled(False)
        self._refresh_action.setEnabled(False)

        self._copy_thread = CopyFilesThread(self._camera_mgr, files, dest, self)
        self._copy_thread.progress.connect(self._on_copy_progress)
        self._copy_thread.finished.connect(self._on_copy_finished)
        self._copy_thread.finished.connect(self._copy_thread.deleteLater)
        self._copy_thread.start()

    @pyqtSlot(int, int, str)
    def _on_copy_progress(self, current: int, total: int, filename: str) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        if filename:
            self._status_label.setText(f"Copying: {filename} ({current + 1}/{total})")
        elif current >= total:
            self._status_label.setText("Transfer complete")

    @pyqtSlot(bool, str)
    def _on_copy_finished(self, ok: bool, message: str) -> None:
        self._progress.setVisible(False)
        self._copy_selected_action.setEnabled(True)
        self._refresh_action.setEnabled(True)
        if ok:
            self._status_label.setText(f"\u2713 {message}")
            self._status_label.setStyleSheet("color: #27ae60;")
        else:
            self._status_label.setText(f"\u2717 {message}")
            self._status_label.setStyleSheet("color: #e35d5d;")
        self._status_label.setToolTip(message)
        # Reset the color after a few seconds
        QTimer.singleShot(
            6000,
            lambda: (self._status_label.setStyleSheet(""), self._status_label.setToolTip("")),
        )
        if ok:
            QMessageBox.information(self, "Transfer", message)
        else:
            QMessageBox.warning(self, "Transfer", message)

    # ------------------------------------------------------------------ browse

    def _browse_destination(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select destination folder", self._drop_zone.destination)
        if d:
            self._drop_zone.destination = d
            self._settings.setValue("dest_folder", d)

    def _save_destination(self) -> None:
        """Persist the current destination folder (manually typed or changed)."""
        dest = self._drop_zone.destination
        if dest:
            self._settings.setValue("dest_folder", dest)

    def _on_selection_changed(self) -> None:
        """Update the storage pane with the selected entries' total size."""
        files = self._selected_camera_files()
        total = 0
        for f in files:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        if files:
            only_name = Path(files[0]).name if len(files) == 1 else f"{len(files)} items"
            self._drop_zone.update_storage(
                *self._camera_mgr.storage_info(),
                selected_size=total,
                selected_name=only_name,
                selected_count=len(files),
            )
        else:
            self._drop_zone.update_storage(*self._camera_mgr.storage_info())

    def _update_storage(self) -> None:
        """Refresh the storage pane after load/disconnect without selection."""
        self._drop_zone.update_storage(*self._camera_mgr.storage_info())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("MTP Reader")
    app.setOrganizationName("MTPReader")
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
