"""support_bundle/probe_storage.py — Every disk, and who can see it.

"My SD card / external drive is not detected" has more than one root
cause, which is exactly why this module exists — nothing else records
what the kernel could see versus what the plugin could see. The three
seen in the wild so far:

1. a space in the filesystem label. udisks2 mounts the drive at
   ``/run/media/<user>/External SSD`` and the kernel escapes that in
   ``/proc/mounts`` as ``External\\040SSD``; the scanner did not decode
   it and dropped the mount as a nonexistent path.
2. FUSE filesystems mounted with ``allow_other`` off, invisible to any
   process whose effective uid differs from the mounting user's.
3. a mount root the desktop user cannot write to — visible to the
   scanner, but refused by the install picker's writability gate. Hence
   ``offered_in_picker``: visible and selectable are different answers.

That comparison is the point of this module. Every device is listed
twice: once as the kernel reports it, once as the plugin's own mount
scanner reports it. **The delta between those two views is the
diagnosis.**

``lsblk --json`` is the primary probe because it reports the
filesystem type of *unmounted* partitions without needing root, which
neither ``/proc/mounts`` nor unprivileged ``blkid`` can do. Unmounted
is exactly the interesting case. Serial numbers are deliberately not
requested.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from unifideck.utils.mount_naming import unescape_mount_field

logger = logging.getLogger(__name__)

_LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,PARTLABEL,UUID,"
    "MOUNTPOINTS,RM,RO,HOTPLUG,TRAN,MODEL,VENDOR,STATE"
)
_VIRTUAL_PREFIXES = ("loop", "zram", "ram", "dm-", "sr")
_NETWORK_FSTYPES = frozenset({"nfs", "nfs4", "cifs", "smb3", "sshfs"})
_FUSE_FSTYPES = frozenset({
    "fuseblk", "exfat-fuse", "ntfs-3g", "fuse.sshfs", "fuse",
})
# Filesystems that cannot host a Wine prefix reliably: no POSIX
# permissions, no symlinks, case-insensitive lookups.
RISKY_FSTYPES = frozenset({"exfat", "fuseblk", "ntfs", "ntfs3", "vfat", "msdos"})
_AUTOMOUNT_ROOTS = ("/run/media", "/media")


def _run_lsblk() -> list[dict[str, Any]] | None:
    """Return lsblk's device tree, or None when unavailable."""
    binary = shutil.which("lsblk")
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [binary, "--json", "-b", "-o", _LSBLK_COLUMNS],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if done.returncode != 0:
            logger.debug("[support_bundle] lsblk rc=%s", done.returncode)
            return None
        parsed = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as err:
        logger.debug("[support_bundle] lsblk failed: %s", err)
        return None
    devices = parsed.get("blockdevices")
    return devices if isinstance(devices, list) else None


def _flatten(nodes: list[dict[str, Any]], parent: str = "") -> list[dict[str, Any]]:
    """Flatten lsblk's nested device tree into one list."""
    out: list[dict[str, Any]] = []
    for node in nodes:
        record = dict(node)
        record["parent"] = parent
        children = record.pop("children", None)
        out.append(record)
        if isinstance(children, list):
            out.extend(_flatten(children, str(record.get("name", ""))))
    return out


def _mountpoints(node: dict[str, Any]) -> list[str]:
    """Every mount point lsblk reports for a device.

    A device can be mounted many times: SteamOS mounts the home
    partition at ``/home``, ``/var/tmp`` and several other paths. Taking
    only the first one made a healthy internal disk look invisible to
    the plugin, so all of them are kept and any match counts.
    """
    points = node.get("mountpoints") or []
    found = [str(p) for p in points if p] if isinstance(points, list) else []
    single = node.get("mountpoint")
    if single and str(single) not in found:
        found.append(str(single))
    return found


def _primary_mountpoint(points: list[str]) -> str:
    """Pick the most meaningful mount point for display.

    Prefers an automount location, then the user's home, then whatever
    is left — so an SD card shows as ``/run/media/deck/SD`` rather than
    an incidental bind mount.
    """
    for point in points:
        if point.startswith(_AUTOMOUNT_ROOTS):
            return point
    for point in points:
        if point in ("/home", str(Path.home())):
            return point
    return points[0] if points else ""


def is_user_storage(record: dict[str, Any]) -> bool:
    """True when a device is one the user would install games to.

    Scopes the visibility check to removable and network media plus
    anything under an automount root. Internal system mounts are
    deliberately *not* expected to appear in the plugin's install-target
    scanner, and treating them as missing produced a failing verdict on
    every healthy device.
    """
    if record.get("class") in ("sdcard", "usb", "network"):
        return True
    return any(
        str(point).startswith(_AUTOMOUNT_ROOTS)
        for point in record.get("all_mountpoints") or []
    )


def classify(node: dict[str, Any]) -> str:
    """Bucket a device as internal / sdcard / usb / network / virtual."""
    name = str(node.get("name") or "")
    parent = str(node.get("parent") or "")
    fstype = str(node.get("fstype") or "")
    transport = str(node.get("tran") or "")
    if fstype in _NETWORK_FSTYPES:
        return "network"
    if name.startswith(_VIRTUAL_PREFIXES):
        return "virtual"
    if name.startswith("mmcblk") or parent.startswith("mmcblk"):
        return "sdcard"
    if transport == "usb" or node.get("hotplug") is True:
        return "usb"
    return "internal"


def _sys_block_fallback() -> list[dict[str, Any]]:
    """Enumerate devices from ``/sys/block`` when lsblk is missing.

    Filesystem types of unmounted partitions are simply unknown here
    and reported as such. Guessing would be worse than admitting it.
    """
    out: list[dict[str, Any]] = []
    root = Path("/sys/block")
    if not root.is_dir():
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        size = _read_int(entry / "size")
        out.append({
            "name": entry.name,
            "path": f"/dev/{entry.name}",
            "type": "disk",
            "size": (size or 0) * 512,
            "fstype": "unknown",
            "rm": bool(_read_int(entry / "removable")),
            "model": _read_str(entry / "device" / "model"),
            "vendor": _read_str(entry / "device" / "vendor"),
            "mountpoints": [],
        })
    return out


def _read_int(path: Path) -> int | None:
    """Read a single integer from a ``/sys`` file."""
    raw = _read_str(path)
    try:
        return int(raw)
    except ValueError:
        return None


def _read_str(path: Path) -> str:
    """Read and strip a ``/sys`` file, "" on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _plugin_view() -> tuple[dict[str, Any], str]:
    """Mount points the plugin's own scanner can see.

    This is the second half of the comparison, and it records BOTH
    scanner variants. The lenient one (``require_writable=False``)
    answers "can we see the device at all"; the strict one is what
    ``get_storage_locations``/``get_browseable_devices`` actually call,
    so ``in_picker`` is the only field that answers the user's real
    question — whether the drive is selectable. Reporting the lenient
    view alone once let a bundle read as healthy for a drive the picker
    was silently dropping as unwritable.
    """
    try:
        from unifideck.utils.mounts import scan_mounts, stat_dev

        home_dev = stat_dev(str(Path.home()))
        mounts = scan_mounts(home_dev, require_writable=False)
        in_picker = {
            mount.mount_point
            for mount in scan_mounts(home_dev, require_writable=True)
        }
    except Exception as err:
        logger.debug("[support_bundle] scan_mounts failed: %s", err)
        return {}, f"scan failed: {err!r}"
    view = {
        mount.mount_point: {
            "device": mount.device,
            "fstype": mount.fstype,
            "writable": mount.writable,
            "in_picker": mount.mount_point in in_picker,
            "effective_uid": mount.effective_uid,
            "options": mount.options,
        }
        for mount in mounts
    }
    return view, "ok"


def _free_bytes(mountpoint: str) -> int | None:
    """Free space at ``mountpoint``, or None if unreadable."""
    if not mountpoint:
        return None
    try:
        return shutil.disk_usage(mountpoint).free
    except OSError:
        return None


def _device_record(
    node: dict[str, Any], view: dict[str, Any],
) -> dict[str, Any]:
    """Build one inventory row, including the visibility verdict."""
    points = _mountpoints(node)
    mountpoint = _primary_mountpoint(points)
    fstype = str(node.get("fstype") or "")
    visible = any(point in view for point in points)
    selectable = any(
        bool(view.get(point, {}).get("in_picker")) for point in points
    )
    return {
        "all_mountpoints": points,
        "name": node.get("name", ""),
        "path": node.get("path", ""),
        "type": node.get("type", ""),
        "class": classify(node),
        "size_bytes": node.get("size"),
        "fstype": fstype or "unknown",
        "label": node.get("label") or node.get("partlabel") or "",
        "uuid": node.get("uuid") or "",
        "removable": bool(node.get("rm")),
        "read_only": bool(node.get("ro")),
        "transport": node.get("tran") or "",
        "model": (node.get("model") or "").strip(),
        "mounted_at": mountpoint,
        "free_bytes": _free_bytes(mountpoint),
        "visible_to_plugin": visible,
        "offered_in_picker": selectable,
        "visibility_note": _visibility_note(
            mountpoint, fstype, visible, selectable,
        ),
    }


def _visibility_note(
    mountpoint: str, fstype: str, visible: bool, selectable: bool,
) -> str:
    """Explain a visibility mismatch in one phrase."""
    if visible and selectable:
        return ""
    if not mountpoint:
        return "not mounted (never automounted, or no filesystem driver)"
    if visible:
        # Seen by the scanner but refused by the picker's writability
        # gate — a different failure with a different fix (ownership of
        # the mount root), so never let it read as "not detected".
        return (
            "visible to the plugin but NOT writable, so the install "
            "picker hides it: check ownership of the mount root"
        )
    if fstype in _FUSE_FSTYPES:
        return "FUSE mount: check allow_other / user_allow_other"
    return "mounted but not reported by the plugin's mount scanner"


def _fuse_conf() -> dict[str, Any]:
    """Whether ``/etc/fuse.conf`` enables ``user_allow_other``.

    This single line is the switch behind the whole class of
    "external drive not detected" reports.
    """
    path = Path("/etc/fuse.conf")
    if not path.is_file():
        return {"present": False, "user_allow_other": False}
    text = _read_str(path)
    enabled = any(
        line.strip() == "user_allow_other"
        for line in text.splitlines()
        if not line.strip().startswith("#")
    )
    return {"present": True, "user_allow_other": enabled}


def _automount_roots() -> list[dict[str, Any]]:
    """List the automount roots the plugin probes, and their contents."""
    found: list[dict[str, Any]] = []
    for raw in _AUTOMOUNT_ROOTS:
        root = Path(raw)
        if not root.is_dir():
            found.append({"path": raw, "exists": False, "children": []})
            continue
        found.append({
            "path": raw,
            "exists": True,
            "children": _child_names(root),
        })
    return found


def _child_names(root: Path) -> list[str]:
    """Names one level under ``root``, tolerating permission errors."""
    try:
        return sorted(child.name for child in root.iterdir())
    except OSError as err:
        return [f"<unreadable: {err.strerror}>"]


def _install_locations(config: Any) -> list[dict[str, Any]]:
    """Configured game directories, with the filesystem behind each."""
    try:
        from unifideck.utils.paths import get_all_game_directories

        directories = get_all_game_directories(config)
    except Exception as err:
        logger.debug("[support_bundle] install dirs failed: %s", err)
        return []
    return [_location_record(raw) for raw in directories]


def _location_record(raw: str) -> dict[str, Any]:
    """Existence, free space and fstype for one install location."""
    path = Path(raw)
    fstype = _fstype_for(raw)
    return {
        "path": raw,
        "exists": path.is_dir(),
        "free_bytes": _free_bytes(raw) if path.is_dir() else None,
        "fstype": fstype,
        "risky_fstype": fstype in RISKY_FSTYPES,
    }


def _fstype_for(target: str) -> str:
    """Find the filesystem type of the mount containing ``target``."""
    best = ""
    best_len = -1
    for line in _read_str(Path("/proc/mounts")).splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        # Shared decoder — this used to open-code a `\040`-only
        # replace, the codebase's one partial fix for the escaping the
        # real scanner ignored entirely.
        point = unescape_mount_field(fields[1])
        if target.startswith(point) and len(point) > best_len:
            best, best_len = fields[2], len(point)
    return best


def storage_block(config: Any) -> dict[str, Any]:
    """Assemble the full storage inventory."""
    nodes = _run_lsblk()
    source = "lsblk"
    if nodes is None:
        nodes = _sys_block_fallback()
        source = "sys_block_fallback"
    else:
        nodes = _flatten(nodes)
    view, view_status = _plugin_view()
    devices = [_device_record(node, view) for node in nodes]
    return {
        "source": source,
        "devices": devices,
        "plugin_view_status": view_status,
        "plugin_view_mount_points": sorted(view),
        # The subset the install picker actually offers. Compare the two
        # lists first: equal means any missing drive died in the
        # scanner, shorter means the writability gate ate it.
        "plugin_view_picker_mount_points": sorted(
            point for point, data in view.items() if data.get("in_picker")
        ),
        "fuse_conf": _fuse_conf(),
        "automount_roots": _automount_roots(),
        "install_locations": _install_locations(config),
    }
