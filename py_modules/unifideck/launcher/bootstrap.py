from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
def build_launcher_service(config: Any | None = None) -> Any:
    """Build launcher service.

    The function works in two modes:

    * **Embedded** (default in Decky): the caller passes a fully
      initialized ``ConfigManager`` via ``config``.
    * **Standalone** (CLI / tests): ``config=None`` causes a
      transient ``ConfigManager`` to be built from disk via
      :func:`_load_standalone_config`.

    In both modes the rest of the wiring (event bus, service
    subset, edge browser, ``LauncherService``) is identical, so
    the body lives at function scope rather than under the
    ``if config is None`` branch.
    """
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.event_bus import EventBus
    from unifideck.launcher.frontend_bridge import install_bus_forwarder
    from unifideck.services.bootstrap import build_service_subset
    from unifideck.services.launcher import LauncherService
    if config is None:
        config = _load_standalone_config()
    bus = EventBus()
    # The launcher runs as its own process, so its LAUNCHER_STAGE toast
    # events can't reach the plugin's replay buffer on their own. Mirror
    # them into the shared bridge file the plugin drains (frontend_bridge).
    install_bus_forwarder(bus)
    # Drift fix (lot 11g): the previous call was
    # ``build_service_subset(bus, config, paths, attrs={...})``
    # — but the real signature is
    # ``(bus, config, services: Iterable[str])``. ``paths`` is
    # derived inside the function and ``attrs=`` was renamed to
    # the positional ``services``. The return type is
    # ``ServiceContainer`` (a dataclass), not a dict — use
    # attribute access, not ``.get()``.
    services = build_service_subset(
        bus, config,
        services={"shortcut", "proton", "cloudsave", "launch_history"},
    )
    shortcut_svc = services.shortcut
    proton_svc = services.proton
    cloud_svc = services.cloudsave
    assert shortcut_svc is not None, "bootstrap: shortcut service missing"
    assert proton_svc is not None, "bootstrap: proton service missing"
    # cloud_svc is intentionally optional. Cloud-save is a non-essential
    # feature and must NEVER block a game launch: if it failed to
    # instantiate (e.g. a missing native dep under the launcher's Python),
    # we log and launch without save-sync rather than aborting the whole
    # launcher at bootstrap. LauncherService tolerates a None cloud_svc and
    # the cloud-sync phases no-op (see helpers.cloud_sync_phase).
    if cloud_svc is None:
        logger.warning(
            "bootstrap: cloudsave service unavailable — "
            "launching without cloud-save sync",
        )
    edge_browser = EdgeBrowser(
        cdp_port=config.get_int("edge.cdp_port", 9222),
        locale_fn=lambda: config.get_str("ui.locale", "en-US"),
    )
    return LauncherService(
        bus=bus,
        shortcut_svc=shortcut_svc,
        proton_svc=proton_svc,
        cloud_svc=cloud_svc,
        edge_browser=edge_browser,
        config=config,
    )
def _load_standalone_config() -> Any:
    """Load standalone config.

    ``resolve_defaults_config_path`` rather than a hardcoded
    ``defaults/config.json``: the Decky CLI build flattens that directory into
    the install root, so the nested path finds nothing on a shipped install and
    the merged view is the hardcoded fallback alone. PR #422 fixed exactly this
    for Epic, Ubisoft and Amazon; this entry point and ``dispatcher`` were not
    in its file list, which left every ``i18n``-derived answer in the launcher
    running on defaults that were never loaded.
    """
    from unifideck.config.config_manager import ConfigManager
    from unifideck.config.defaults_path import resolve_defaults_config_path
    plugin_dir = _resolve_plugin_dir()
    defaults_path = resolve_defaults_config_path(plugin_dir)
    user_path = _user_config_path()
    return ConfigManager(
        defaults_path=defaults_path,
        user_path=user_path,
    )
def _resolve_plugin_dir() -> str:
    """Resolve plugin dir."""
    from unifideck.core.paths import resolve_plugin_dir
    return str(resolve_plugin_dir(start=Path(__file__)))

def _user_config_path() -> str | None:

    """User config path."""
    override = os.environ.get("UNIFIDECK_USER_CONFIG")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(
        Path.home() / ".config",
    )
    return str(Path(xdg) / "unifideck" / "config.json")
