"""Tests for rpc/mixins/storage.py's location/device builders.

Covers the two bugs fixed alongside the FUSE-mount regression:
every external mount used to collapse onto the single hardcoded id
``"sdcard"`` (breaking the frontend's `key={loc.id}` selection with
two simultaneous external devices), and a persisted legacy
``default_location == "sdcard"`` would match nothing once ids became
unique. ``mounts.scan_mounts``/``dedupe_by_device`` are monkeypatched
with canned ``MountInfo`` objects — the mount-scanning logic itself
is covered by ``test_mounts.py``.
"""
from __future__ import annotations

import pytest

from unifideck.rpc.mixins import storage as storage_mod
from unifideck.utils import mounts


def _mount(mount_point: str, device: str = "/dev/sda1", **overrides: object) -> mounts.MountInfo:
    base: dict[str, object] = {
        "device": device, "mount_point": mount_point, "fstype": "ext4",
        "st_dev": abs(hash(mount_point)) % 100000 + 1, "options": {}, "writable": True,
    }
    base.update(overrides)
    return mounts.MountInfo(**base)  # type: ignore[arg-type]


def test_build_storage_locations_two_external_mounts_get_distinct_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sd = _mount("/run/media/deck/SDCARD", device="/dev/mmcblk0p1")
    usb = _mount("/run/media/deck/USBDRIVE", device="/dev/sda1")
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [sd, usb])
    monkeypatch.setattr(mounts, "ensure_games_subdir", lambda mp, uid, gid: f"{mp}/Games")

    locations = storage_mod._build_storage_locations(None)
    external = [loc for loc in locations if loc["id"] != "internal"]

    ids = {loc["id"] for loc in external}
    assert len(external) == 2
    assert len(ids) == 2, "two simultaneous external mounts must not collide on one id"


def test_build_storage_locations_same_basename_gets_distinct_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct devices whose mount points share a basename stay unique."""
    a = _mount("/run/media/deck/GAMES", device="/dev/sda1")
    b = _mount("/media/GAMES", device="/dev/sdb1")
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [a, b])
    monkeypatch.setattr(mounts, "ensure_games_subdir", lambda mp, uid, gid: f"{mp}/Games")

    external = [
        loc for loc in storage_mod._build_storage_locations(None)
        if loc["id"] != "internal"
    ]
    ids = [loc["id"] for loc in external]
    assert len(external) == 2
    assert len(set(ids)) == 2, ids
    assert all(str(i).startswith("ext:") for i in ids)


def test_build_storage_locations_labels_mmcblk_source_as_sd_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sd = _mount("/run/media/deck/SDCARD", device="/dev/mmcblk0p1")
    usb = _mount("/run/media/deck/USBDRIVE", device="/dev/sda1")
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [sd, usb])
    monkeypatch.setattr(mounts, "ensure_games_subdir", lambda mp, uid, gid: f"{mp}/Games")

    locations = storage_mod._build_storage_locations(None)
    by_id = {loc["id"]: loc for loc in locations}

    assert by_id[mounts.mount_id(sd)]["label"] == "SD Card"
    assert by_id[mounts.mount_id(usb)]["label"] == "External Drive (USBDRIVE)"


def test_build_storage_locations_includes_custom_path_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [])
    locations = storage_mod._build_storage_locations("/mnt/custom")
    assert [loc["id"] for loc in locations] == ["internal", "custom"]


def test_build_browseable_devices_two_external_mounts_get_distinct_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_a = _mount("/run/media/deck/A")
    mount_b = _mount("/run/media/deck/B")
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [mount_a, mount_b])

    devices = storage_mod._build_browseable_devices()
    ext_ids = {d["id"] for d in devices if d["id"] != "internal"}
    assert len(ext_ids) == 2


def test_remap_legacy_default_passthrough_when_id_still_valid() -> None:
    locations = [{"id": "internal"}, {"id": "custom"}]
    assert storage_mod._remap_legacy_default("custom", locations) == "custom"


def test_remap_legacy_default_sdcard_remaps_to_real_ext_id() -> None:
    locations = [{"id": "internal"}, {"id": "ext:SDCARD"}]
    assert storage_mod._remap_legacy_default("sdcard", locations) == "ext:SDCARD"


def test_remap_legacy_default_sdcard_falls_back_to_internal_when_no_external() -> None:
    locations = [{"id": "internal"}]
    assert storage_mod._remap_legacy_default("sdcard", locations) == "internal"


def test_remap_legacy_default_unknown_value_falls_back_to_internal() -> None:
    locations = [{"id": "internal"}, {"id": "ext:SDCARD"}]
    assert storage_mod._remap_legacy_default("ext:GONE", locations) == "internal"


def test_remap_legacy_default_upgrades_a_name_based_id_to_the_uuid_id() -> None:
    """The user's chosen drive must stay chosen across the id change.

    Configs written before ids moved to the filesystem UUID hold
    ``ext:<name>``, which matches no current id. Re-deriving the old
    id from each location's ``device_path`` recognises it.
    """
    locations = [
        {"id": "internal", "device_path": "/home/deck/Games"},
        {
            "id": "ext:b430ddca-dece-4f36-b839-ab71e1b4efed",
            "device_path": "/run/media/deck/External SSD",
        },
    ]
    assert storage_mod._remap_legacy_default("ext:External_SSD", locations) == (
        "ext:b430ddca-dece-4f36-b839-ab71e1b4efed"
    )


def test_remap_legacy_default_ignores_a_name_id_for_an_absent_drive() -> None:
    locations = [
        {"id": "internal", "device_path": "/home/deck/Games"},
        {"id": "ext:other-uuid", "device_path": "/run/media/deck/LX1TB"},
    ]
    assert storage_mod._remap_legacy_default("ext:External_SSD", locations) == "internal"


def test_external_label_strips_invisible_characters_from_the_drive_name() -> None:
    """A label is attacker-ish input in the sense that we render it raw."""
    m = _mount("/run/media/deck/GAMES" + chr(0x202E) + "exe")
    assert storage_mod._external_label(m) == "External Drive (GAMESexe)"


def test_storage_location_rows_carry_their_device_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usb = _mount("/run/media/deck/External SSD", uuid="b430ddca")
    monkeypatch.setattr(mounts, "scan_mounts", lambda *args, **kw: [usb])
    monkeypatch.setattr(mounts, "ensure_games_subdir", lambda mp, uid, gid: f"{mp}/Games")

    locations = storage_mod._build_storage_locations(None)
    external = next(loc for loc in locations if loc["id"] != "internal")

    assert external["id"] == "ext:b430ddca"
    assert external["device_path"] == "/run/media/deck/External SSD"
    assert external["path"] == "/run/media/deck/External SSD/Games"


def test_set_default_storage_location_accepts_ext_id() -> None:
    class _FakeConfig:
        def set(self, key: str, value: object) -> None:
            self.saved = (key, value)  # type: ignore[attr-defined]

    mixin = storage_mod.StorageRPCMixin()
    mixin.config = _FakeConfig()

    import asyncio

    result = asyncio.run(mixin.set_default_storage_location("ext:SDCARD"))
    assert result["success"] is True


def test_set_default_storage_location_rejects_garbage_id() -> None:
    from unifideck.rpc import RpcError

    mixin = storage_mod.StorageRPCMixin()
    mixin.config = object()

    import asyncio

    with pytest.raises(RpcError):
        asyncio.run(mixin.set_default_storage_location("not-a-real-id"))
