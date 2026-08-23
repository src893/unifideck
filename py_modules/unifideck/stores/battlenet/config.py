"""Frozen configuration for the Battle.net store.

py_modules/unifideck/stores/battlenet/config.py

Follows the Ubisoft ``_FIELD_SPECS`` pattern: every key is declared once
with its type and default, so ``from_config_manager`` gets coercion and
per-key fallback for free rather than scattering ``get_cfg`` calls.

Most of these are paths *inside a Wine prefix* that shift when the client
changes layout, so they must be tunable without shipping a release. The
env values are the exception and are deliberately non-optional.

One inherited scar: for Ubisoft the value that actually applies is the one
in ``_FIELD_SPECS``, not the dataclass field default. Keeping the two in
sync is enforced by a unit test rather than by discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# name -> (config key, default). The config key is relative to
# ``stores.battlenet`` in the merged config.
_FIELD_SPECS: dict[str, tuple[str, Any]] = {
    "data_dir": ("data_dir", "~/.local/share/unifideck"),
    "prefixes_dir": ("prefixes_dir", "~/.local/share/unifideck/prefixes/battlenet"),
    "installer_url": (
        "installer_url",
        (
            "https://www.battle.net/download/getInstallerForGame"
            "?os=win&version=LIVE&gameProgram=BATTLENET_APP"
        ),
    ),
    "installer_filename": ("installer_filename", "Battle.net-Setup.exe"),
    "installer_cache_dir": (
        "installer_cache_dir",
        "~/.local/share/unifideck/battlenet_installer_cache",
    ),
    "client_ready_timeout_seconds": ("client_ready_timeout_seconds", 300),
    "game_appear_timeout_seconds": ("game_appear_timeout_seconds", 180),
    "install_poll_interval_seconds": ("install_poll_interval_seconds", 10),
    "install_timeout_seconds": ("install_timeout_seconds", 7200),
    "catalog_max_age_seconds": ("catalog_max_age_seconds", 604800),
    "harvest_client_cookies": ("harvest_client_cookies", False),
    "enable_web_enrichment": ("enable_web_enrichment", True),
}


@dataclass(frozen=True, slots=True)
class BattlenetConfig:
    """Resolved Battle.net settings."""

    data_dir: str = "~/.local/share/unifideck"
    prefixes_dir: str = "~/.local/share/unifideck/prefixes/battlenet"
    installer_url: str = (
        "https://www.battle.net/download/getInstallerForGame"
        "?os=win&version=LIVE&gameProgram=BATTLENET_APP"
    )
    installer_filename: str = "Battle.net-Setup.exe"
    installer_cache_dir: str = "~/.local/share/unifideck/battlenet_installer_cache"
    client_ready_timeout_seconds: int = 300
    game_appear_timeout_seconds: int = 180
    install_poll_interval_seconds: int = 10
    install_timeout_seconds: int = 7200
    catalog_max_age_seconds: int = 604800
    harvest_client_cookies: bool = False
    enable_web_enrichment: bool = True

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def prefixes_dir_path(self) -> Path:
        return Path(self.prefixes_dir).expanduser()

    @property
    def installer_path(self) -> Path:
        """Where the downloaded client installer is cached."""
        return Path(self.installer_cache_dir).expanduser() / self.installer_filename

    @property
    def id_map_path(self) -> Path:
        from .id_map import ID_MAP_FILENAME

        return self.data_dir_path / ID_MAP_FILENAME


def _coerce(value: Any, default: Any) -> Any:
    """Coerce a config value to the default's type, falling back on failure."""
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return str(value) if value != "" else default


def from_mapping(raw: dict[str, Any] | None) -> BattlenetConfig:
    """Build a config from a plain mapping. Unknown keys are ignored."""
    source = raw or {}
    kwargs = {
        name: _coerce(source.get(key), default)
        for name, (key, default) in _FIELD_SPECS.items()
    }
    return BattlenetConfig(**kwargs)


def from_config_manager(config: Any) -> BattlenetConfig:
    """Build a config from the plugin's ConfigManager, tolerating absence."""
    if config is None:
        return BattlenetConfig()
    getter = getattr(config, "get", None)
    if not callable(getter):
        return BattlenetConfig()
    try:
        raw = getter("stores.battlenet", {})
    except Exception:  # config must never break store construction
        return BattlenetConfig()
    return from_mapping(raw if isinstance(raw, dict) else {})


# Guards the scar noted in the module docstring.
FIELD_DEFAULTS: dict[str, Any] = {
    name: default for name, (_key, default) in _FIELD_SPECS.items()
}
DATACLASS_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in fields(BattlenetConfig) if f.default is not field
}
