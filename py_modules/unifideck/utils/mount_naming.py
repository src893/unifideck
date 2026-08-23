"""utils/mount_naming.py — how a mount is spelled, named and identified.

Split out of ``utils/mounts.py`` (which was over its size cap) along a
real seam rather than an arbitrary one: everything here is a pure
string/path or ``/dev`` index helper that knows nothing about mount
enumeration, so the scanner, the RPC layer and the support-bundle
probes can all share it without importing the scanner. The dependency
runs one way only — ``mounts`` imports this, never the reverse.

Three separate hazards live in a drive's name, all of them proven in
the field:

1. ``/proc/mounts`` is an escaped format. A drive labelled
   "External SSD" appears as ``/run/media/deck/External\\040SSD``, and
   reading that field verbatim yields a path that does not exist — the
   whole "my external drive isn't detected" class of report.
2. The name is not an identity. Deriving an id from it made a saved
   install target break when the user relabelled the drive, and made
   "External SSD" and "External_SSD" collapse onto one id.
3. The name is rendered. Labels can carry bidi overrides that reorder
   the surrounding UI text, and bytes that aren't valid UTF-8, which
   survive as surrogates and break JSON writers downstream.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The kernel escapes exactly four characters in the device and
# mount-point fields of /proc/mounts (fs/proc_namespace.c) — space,
# tab, newline and backslash — as three-digit octal.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")

# udev's UUID index: one symlink per filesystem, pointing at its
# device node. Readable unprivileged, no subprocess, and the UUID it
# names lives in the filesystem superblock — so unlike a label or a
# device number it survives relabelling, replugging and reboots.
BY_UUID_DIR = Path("/dev/disk/by-uuid")

# Characters that must never reach a rendered device name: C0/C1
# controls, zero-width joiners, the bidi overrides (a label holding
# U+202E visually reverses the text after it), and lone surrogates
# from a label that wasn't valid UTF-8 (those also break any
# ensure_ascii=False JSON writer they reach). Declared as codepoint
# ranges rather than literal characters on purpose: pasting the real
# glyphs in would make this file unreadable and one stray edit away
# from a silent behaviour change.
_UNSAFE_DISPLAY_RANGES = (
    (0x00, 0x1F), (0x7F, 0x9F),           # C0 and C1 controls
    (0x200B, 0x200F),                     # zero-width joiners, LRM/RLM
    (0x202A, 0x202E), (0x2066, 0x2069),   # bidi embeddings and overrides
    (0xFEFF, 0xFEFF),                     # zero-width no-break space
    (0xD800, 0xDFFF),                     # lone surrogates (non-UTF-8 label)
)
_UNSAFE_DISPLAY = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _UNSAFE_DISPLAY_RANGES) + "]",
)


def unescape_mount_field(raw: str) -> str:
    """Decode the kernel's octal escapes in one ``/proc/mounts`` field.

    The kernel writes space, tab, newline and backslash as ``\\040``,
    ``\\011``, ``\\012`` and ``\\134``, so a drive labelled
    "External SSD" — which udisks2 automounts at
    ``/run/media/<user>/External SSD`` — arrives as
    ``/run/media/<user>/External\\040SSD``. Left undecoded that names
    no existing path, so the mount failed its ``is_dir()`` probe and
    disappeared from the install picker entirely: the "my external
    drive isn't detected" report, reproducible with nothing but a
    space in the filesystem label.

    Single-pass by construction: ``re.sub`` never rescans its own
    replacements, so a label containing a literal backslash (which the
    kernel escapes as ``\\134040``) decodes to ``\\040`` rather than
    being mistaken for a space. Pure — never raises.
    """
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), raw)


def display_name(mount_point: str) -> str:
    """The mount's user-facing name, stripped of invisible mischief.

    The label reaches the UI verbatim, so a drive named with a bidi
    override could visually reorder the text around it in the picker,
    and one named with bytes that aren't valid UTF-8 carries surrogates
    that break any ``ensure_ascii=False`` JSON writer downstream.
    Display-only: never use this to build a path or an id.
    """
    return _UNSAFE_DISPLAY.sub("", Path(mount_point).name) or _UNSAFE_DISPLAY.sub(
        "", mount_point,
    )


def legacy_mount_id(mount_point: str) -> str:
    """The pre-UUID, name-derived id.

    Still the fallback for mounts udev exposes no UUID for (network
    shares, some FUSE mounts), and still needed to recognise ids saved
    in a user's config before the switch — see ``_remap_legacy_default``
    in ``rpc/mixins/storage.py``. Must keep deriving exactly what the
    old code derived, or that migration stops matching.
    """
    name = Path(mount_point).name.replace(" ", "_")
    return f"ext:{name}" if name else "ext"


def uuid_by_device(root: Path = BY_UUID_DIR) -> dict[str, str]:
    """Map device node → filesystem UUID from udev's ``by-uuid`` index.

    One ``readlink`` per filesystem, no subprocess and no privileges.
    Returns an empty map on any failure — callers fall back to
    name-derived ids, so a missing ``/dev/disk/by-uuid`` (containers,
    exotic distros) degrades rather than breaking.
    """
    found: dict[str, str] = {}
    try:
        links = sorted(root.iterdir())
    except OSError as e:
        logger.debug("[mounts] %s unreadable: %s", root, e)
        return found
    for link in links:
        try:
            found[str(link.resolve())] = link.name
        except OSError as e:
            logger.debug("[mounts] %s unresolvable: %s", link, e)
    return found


def device_uuid(device: str, uuids: dict[str, str]) -> str:
    """UUID for *device*, matching either the literal or resolved node.

    ``/proc/mounts`` usually names the real node (``/dev/sda1``), but a
    unit or fstab entry can mount by ``/dev/disk/by-uuid/...`` or
    through ``/dev/mapper``, in which case only the resolved form is a
    key in the index.
    """
    direct = uuids.get(device)
    if direct:
        return direct
    try:
        return uuids.get(str(Path(device).resolve()), "")
    except OSError:
        return ""
