"""Device classification from DMI.

The Fremont case is the one that matters: it is the only value here
taken from real hardware (a Steam Machine support bundle, 2026-08-03)
rather than from documentation, and getting it wrong mislabels the
library tab on a device we cannot test against.
"""

from __future__ import annotations

import pytest

from unifideck.utils import device
from unifideck.utils.device import DeviceType, detect_device_type


@pytest.fixture
def dmi(tmp_path, monkeypatch):
    """Point the module at a fake ``/sys`` DMI directory."""

    def _write(**fields: str):
        for name, value in fields.items():
            (tmp_path / name).write_text(value, encoding="utf-8")
        monkeypatch.setattr(device, "_DMI", tmp_path)
        return tmp_path

    return _write


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ("Jupiter", DeviceType.DECK),  # Steam Deck LCD
        ("Galileo", DeviceType.DECK),  # Steam Deck OLED
        ("Fremont", DeviceType.MACHINE),  # Steam Machine (measured)
    ],
)
def test_valve_hardware_is_classified(dmi, product, expected):
    dmi(sys_vendor="Valve", product_name=product)
    assert detect_device_type() is expected


def test_dmi_values_are_matched_case_insensitively(dmi):
    """Firmware authors these strings; they are not an API contract."""
    dmi(sys_vendor="VALVE", product_name="JUPITER")
    assert detect_device_type() is DeviceType.DECK


def test_unknown_valve_product_falls_back_rather_than_guessing(dmi):
    """Future Valve hardware must degrade to a true generic label."""
    dmi(sys_vendor="Valve", product_name="SomeUnreleasedThing")
    assert detect_device_type() is DeviceType.OTHER


def test_third_party_vendor_is_other_even_with_a_colliding_product(dmi):
    """A non-Valve board is free to call itself anything at all."""
    dmi(sys_vendor="Acme Corp", product_name="Fremont")
    assert detect_device_type() is DeviceType.OTHER


def test_absent_dmi_is_other_not_an_exception(tmp_path, monkeypatch):
    """Containers, VMs and CI have no DMI. This runs in a UI init path."""
    monkeypatch.setattr(device, "_DMI", tmp_path / "does-not-exist")
    assert detect_device_type() is DeviceType.OTHER


def test_device_type_serialises_as_a_plain_string():
    """The RPC boundary sends ``.value`` with no conversion step."""
    assert DeviceType.MACHINE.value == "machine"
    assert [d.value for d in DeviceType] == ["deck", "machine", "other"]


def test_support_bundle_reports_the_derived_type(dmi):
    """A bundle should state the device class, not just its codename."""
    from unifideck.services.support_bundle import probe_device

    root = dmi(sys_vendor="Valve", product_name="Fremont")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(probe_device, "_DMI", root)
    try:
        assert probe_device.device_block()["device_type"] == "machine"
    finally:
        monkey.undo()
