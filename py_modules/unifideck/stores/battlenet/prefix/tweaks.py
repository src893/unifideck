"""Prefix tweaks the Battle.net client needs before it will behave.

py_modules/unifideck/stores/battlenet/prefix/tweaks.py

Each of these is on-disk state that must exist *before* the client's first
run, so they cannot live in the launch environment:

* ``Battle.net.config`` with hardware acceleration, sound and streaming
  disabled. Without the HW-accel change the login view renders as a
  spinner with no login buttons — the long-standing Wine symptom Lutris
  also works around.
* ``HKCU\\Software\\Wine\\DXVA2`` ``backend=va``, from Lutris' shipped
  install script.

Applied under a **versioned marker** so a future tweak change re-applies
itself on the next launch rather than needing a prefix rebuild — the same
``.v2`` self-heal idiom the GOG registry fix uses.

Deliberately *not* here: ``WINE_SIMULATE_WRITECOPY``, ``PROTON_USE_XALIA``
and ``WINEDLLOVERRIDES``. Those are launch-time environment, and they
belong in the launcher's env builder so they apply to every invocation
including install and auth, not just the ones that happen to run after a
tweak pass.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump when the tweak content changes; the old marker no longer matches and
# the tweaks re-apply on the next launch.
TWEAKS_MARKER = ".unifideck_battlenet_tweaks.v1"

CONFIG_RELATIVE = "users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"

# Client-side settings. HardwareAcceleration is the load-bearing one.
_CLIENT_SETTINGS: dict[str, object] = {
    "HardwareAcceleration": "false",
    "Sound": "false",
    "Streaming": "false",
}

# Written by the caller into the prefix registry; kept here so the whole
# tweak surface is visible in one place.
DXVA2_REGISTRY = (r"HKEY_CURRENT_USER\Software\Wine\DXVA2", "backend", "va")


def tweaks_applied(prefix: Path) -> bool:
    """True when this prefix already has the current tweak generation."""
    return (Path(prefix) / TWEAKS_MARKER).exists()


def _load_config(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_client_config(drive_c: Path) -> bool:
    """Merge our settings into ``Battle.net.config``, preserving the rest.

    Merged rather than overwritten on purpose: the file also carries the
    user's saved account name and auto-login state, and clobbering it would
    silently sign them out of a prefix they had already authenticated.
    """
    path = Path(drive_c) / CONFIG_RELATIVE
    config = _load_config(path)
    client = config.get("Client")  # config-key-ignore: Battle.net.config, not plugin config
    if not isinstance(client, dict):
        client = {}
    client.update(_CLIENT_SETTINGS)
    config["Client"] = client
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    except OSError as exc:
        logger.warning("[Battlenet] cannot write %s: %s", path, exc)
        return False
    return _confirm_written(path)


def _confirm_written(path: Path) -> bool:
    """Read the file back and report what the client will actually see.

    Not ceremony. ``HardwareAcceleration=false`` is the documented cure for
    the client's GPU renderer, it was applied to a failing prefix in the
    same millisecond the client started, and the renderer initialised ANGLE
    and aborted anyway. Distinguishing "we wrote it and the client ignored
    it" from "we never wrote what we think we wrote" took a whole round
    trip with a tester; reading it back costs one stat and settles it in
    the log.
    """
    settings = _load_config(path).get("Client")
    if not isinstance(settings, dict):
        logger.warning(
            "[Battlenet] wrote %s but it reads back with no Client section", path,
        )
        return False
    missing = {
        key for key, value in _CLIENT_SETTINGS.items() if settings.get(key) != value
    }
    if missing:
        logger.warning(
            "[Battlenet] %s did not keep %s", path, sorted(missing),
        )
        return False
    logger.info(
        "[Battlenet] client config in force: %s",
        {key: settings.get(key) for key in _CLIENT_SETTINGS},
    )
    return True


def mark_applied(prefix: Path) -> bool:
    try:
        (Path(prefix) / TWEAKS_MARKER).write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("[Battlenet] cannot mark tweaks applied: %s", exc)
        return False
    return True


def _forget_login_routing(config: dict[str, object]) -> None:
    """Drop the remembered login routing from every install section.

    These keys are **not** under ``Client``. The measured layout keeps them in
    ``<install-hash>.Services``, where the hash covers the client's install
    path, so popping them from ``Client`` (as this once did) was a silent
    no-op. The section name has to be discovered rather than known, hence the
    walk: anything that is not ``Client`` or ``Games`` is an install section.
    """
    for name, section in config.items():
        if name in ("Client", "Games") or not isinstance(section, dict):
            continue
        services = section.get("Services")
        if not isinstance(services, dict):
            continue
        for key in ("LastLoginAddress", "LastLoginRegion", "LastLoginTassadar"):
            services.pop(key, None)


def clear_client_credentials(drive_c: Path) -> bool:
    """Sign the client out by dropping its saved-account keys.

    Used by "sign out of the Battle.net client", which is deliberately
    separate from ``logout()``: the plugin's logout must never touch a
    prefix, because for this store the prefix holds the game.
    """
    path = Path(drive_c) / CONFIG_RELATIVE
    config = _load_config(path)
    if not config:
        return True
    client = config.get("Client")  # config-key-ignore: Battle.net.config, not plugin config
    if isinstance(client, dict):
        for key in ("SavedAccountNames", "AutoLogin"):
            client.pop(key, None)
    _forget_login_routing(config)
    try:
        path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    except OSError as exc:
        logger.warning("[Battlenet] cannot clear credentials in %s: %s", path, exc)
        return False
    return True
