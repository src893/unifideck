"""config/defaults_path.py — locate the bundled config.json.

Companion to :mod:`config.user_config_path`: that one answers "where do
the user's overrides live", this one answers "where did the defaults
ship".

Two install layouts are valid in production, and which one you get
depends on how the plugin was built:

1. ``<plugin>/defaults/config.json`` — local builds
   (``build-plugin.sh`` without the Decky CLI) and dev syncs, which
   preserve the source directory layout.
2. ``<plugin>/config.json`` — Decky CLI builds. Since CLI 0.0.8 the
   contents of ``defaults/`` are flattened to the install root on first
   install, so users can edit them and the file survives plugin
   updates.

The plugin backend has always handled both (``bootstrap.boot``, which
now delegates here). The launcher process is a separate entry point and
hardcoded layout 1, so on a CLI-built install — the normal case for
anyone who did not build locally — its ``ConfigManager`` found no
defaults at all.
"""
from __future__ import annotations

from pathlib import Path


def resolve_defaults_config_path(plugin_dir: str | Path) -> str:
    """Return the bundled config.json path for either install layout.

    Prefers the unflattened layout when both exist, being the more
    explicit of the two. Returns the unflattened path when neither
    exists, so the caller still has something to log or hand to
    ``ConfigManager``, which treats a missing defaults file as degraded
    mode rather than an error.
    """
    nested = Path(plugin_dir) / "defaults" / "config.json"
    if nested.is_file():
        return str(nested)
    flattened = Path(plugin_dir) / "config.json"
    if flattened.is_file():
        return str(flattened)
    return str(nested)
