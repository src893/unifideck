"""Which Valve device is this — Deck, Steam Machine, or neither.

py_modules/unifideck/utils/device.py

The library's compatibility tab is titled after the hardware it is
filtering for, so it has to name the machine the user is actually
holding. Getting it wrong is not cosmetic: telling a Steam Machine owner
their games are "Great on Deck" names a device they do not own.

**DMI is the only signal that discriminates.** Measured against a real
Steam Machine support bundle (2026-08-03), because two more obvious
signals both look right and are both wrong:

* ``/etc/os-release`` ``VARIANT_ID`` is ``steamdeck`` on a Steam Machine
  too, so "is this SteamOS" answers yes for both devices.
* The ``SteamDeck`` environment variable is session-scoped. The same
  bundle recorded it *empty* on the Machine simply because the probe ran
  outside the gamescope session, and the backend runs outside it too.

What does discriminate is the DMI identity that bundle recorded::

    sys_vendor      "Valve"
    product_name    "Fremont"     (board_name "Fremont", family "HawkPoint")

against a Deck's ``Jupiter`` (LCD) or ``Galileo`` (OLED).

Unknown Valve hardware deliberately resolves to :attr:`DeviceType.OTHER`
rather than being guessed into the nearest match. The cost of the
fallback is a generic-but-true label; the cost of a guess is a wrong
device name on hardware that did not exist when this was written. That
is the same trade the 32-bit Vulkan probe got wrong by inferring driver
support from filenames, and it is worth paying once here.

Stdlib only, never raises.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_DMI = Path("/sys/devices/virtual/dmi/id")

#: DMI ``sys_vendor`` on Valve hardware. Compared case-insensitively
#: because it is a firmware-authored string, not an API contract.
_VALVE_VENDOR = "valve"

#: ``product_name`` per device. Jupiter is the LCD Deck, Galileo the
#: OLED refresh, Fremont the Steam Machine.
_DECK_PRODUCTS = frozenset({"jupiter", "galileo"})
_MACHINE_PRODUCTS = frozenset({"fremont"})


class DeviceType(Enum):
    """Device class the UI labels itself after.

    A plain ``Enum`` rather than ``StrEnum``: callers send ``.value``
    over RPC explicitly, and ``StrEnum`` would put a 3.11 floor on a
    module that has no other reason to carry one.
    """

    DECK = "deck"
    MACHINE = "machine"
    OTHER = "other"


def _read_dmi(field: str) -> str:
    """Read one ``/sys`` DMI field, lowercased. "" on any failure.

    Absent DMI is normal, not exceptional: containers, VMs and CI have
    none, and this must return a usable answer there rather than raise
    into a UI init path.
    """
    try:
        return (_DMI / field).read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return ""


def detect_device_type() -> DeviceType:
    """Classify the host as Deck, Steam Machine, or neither.

    Non-Valve hardware short-circuits before ``product_name`` is read:
    a third-party board is free to call itself anything, and matching
    its product name against Valve's would be a collision waiting to
    happen.
    """
    vendor = _read_dmi("sys_vendor")
    if vendor != _VALVE_VENDOR:
        return DeviceType.OTHER
    product = _read_dmi("product_name")
    if product in _DECK_PRODUCTS:
        return DeviceType.DECK
    if product in _MACHINE_PRODUCTS:
        return DeviceType.MACHINE
    logger.info(
        "[device] unrecognised Valve product_name %r — treating as generic",
        product,
    )
    return DeviceType.OTHER
