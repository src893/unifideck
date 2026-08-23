"""Tests for utils/mount_naming.py — escaping, identity and display.

Three hazards, each with its own field report behind it: a drive
labelled with a space vanished from the install picker because
``/proc/mounts`` escaping was never decoded; an id derived from the
label broke when the drive was relabelled; and a label reaches the UI
verbatim, so invisible characters in it reach the UI too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from unifideck.utils import mount_naming


def _fake_uuid_index(tmp_path: Path, mapping: dict[str, str]) -> Path:
    """A stand-in ``/dev/disk/by-uuid`` — uuid symlink -> device node."""
    root = tmp_path / "by-uuid"
    root.mkdir()
    for uuid, device in mapping.items():
        (root / uuid).symlink_to(device)
    return root


def test_legacy_mount_id_still_derives_the_old_scheme() -> None:
    """The migration in _remap_legacy_default depends on this exactly."""
    assert mount_naming.legacy_mount_id("/run/media/deck/External SSD") == (
        "ext:External_SSD"
    )
    assert mount_naming.legacy_mount_id("/run/media/deck/LX1TB") == "ext:LX1TB"


# ─── /proc/mounts octal escaping ───────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    (r"/run/media/deck/External\040SSD", "/run/media/deck/External SSD"),
    (r"/run/media/deck/tab\011here", "/run/media/deck/tab\there"),
    (r"/run/media/deck/nl\012here", "/run/media/deck/nl\nhere"),
    (r"/run/media/deck/back\134slash", "/run/media/deck/back\\slash"),
    ("/run/media/deck/PLAIN", "/run/media/deck/PLAIN"),
    ("", ""),
])
def test_unescape_mount_field_decodes_kernel_escapes(raw: str, expected: str) -> None:
    assert mount_naming.unescape_mount_field(raw) == expected


def test_unescape_mount_field_never_double_decodes() -> None:
    """A literal backslash in a label must not turn into a space.

    The kernel escapes the backslash of a label containing ``\\040``
    as ``\\134040``, so decoding twice would read it as an escaped
    space and rename the drive.
    """
    assert mount_naming.unescape_mount_field(r"a\134040b") == r"a\040b"



def test_uuid_by_device_reads_the_udev_index(tmp_path: Path) -> None:
    node = tmp_path / "sda1"
    node.touch()
    root = _fake_uuid_index(tmp_path, {"b430ddca-dece-4f36-b839-ab71e1b4efed": str(node)})
    assert mount_naming.uuid_by_device(root) == {
        str(node): "b430ddca-dece-4f36-b839-ab71e1b4efed",
    }


def test_uuid_by_device_missing_index_degrades_quietly(tmp_path: Path) -> None:
    assert mount_naming.uuid_by_device(tmp_path / "absent") == {}



# ─── display names ─────────────────────────────────────────────


# Built from codepoints so this file stays readable ASCII — the whole
# point of the sanitiser is characters you cannot see in a diff.
_BIDI_OVERRIDE = chr(0x202E)   # RIGHT-TO-LEFT OVERRIDE
_ZERO_WIDTH = chr(0x200B)      # ZERO WIDTH SPACE
_NBSP = chr(0x00A0)            # NO-BREAK SPACE — legitimate text, keep it


@pytest.mark.parametrize(("raw", "expected"), [
    ("External SSD", "External SSD"),                       # untouched
    (f"GAMES{_BIDI_OVERRIDE}exe", "GAMESexe"),               # bidi override
    (f"zero{_ZERO_WIDTH}width", "zerowidth"),                # zero-width space
    ("bell\x07here", "bellhere"),                            # C0 control
    (f"nbsp{_NBSP}ok", f"nbsp{_NBSP}ok"),                    # NBSP survives
])
def test_display_name_strips_invisible_characters(raw: str, expected: str) -> None:
    assert mount_naming.display_name("/run/media/deck/" + raw) == expected


def test_display_name_drops_surrogates_from_a_non_utf8_label() -> None:
    """Surrogates would break any ensure_ascii=False JSON writer."""
    name = mount_naming.display_name(os.fsdecode(b"/run/media/deck/Disque\xa0Dur"))
    assert name == "DisqueDur"
    json.dumps(name, ensure_ascii=False).encode("utf-8")  # must not raise


