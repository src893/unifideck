"""One-shot ``LastPlayTime`` migration, lifted out of reconcile.

An early build stamped ``LastPlayTime = now`` into every shortcut it
created, so Steam's ``GetPlaytime`` reported the same fake "last played"
date across a whole library of never-launched games. This clears those
stamps exactly once, guarded by a marker in the data dir; Steam re-stamps
real plays on launch and ``_update_existing_shortcut`` preserves them
from then on.

Split out of ``reconcile_phases.py`` (2026-08-19) to keep that file under
the 550-LOC volumetry cap — a migration that runs once per install has no
business sharing a file with the steady-state reconcile path.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .reconcile_helpers import touch_marker

logger = logging.getLogger(__name__)


def _clear_our_stamps(shortcuts_dict: dict[str, Any], launcher_path: str) -> int:
    """Zero ``LastPlayTime`` on our entries; return how many were cleared.

    Ownership is the ``Exe`` gate, not the LaunchOptions token or our
    ``tags`` marker. Both of those survive on shortcuts we do not own —
    a user duplicating one of our tiles, or a third-party scanner
    cloning it — and this migration writes into whatever it matches, so
    a looser test wipes real play history off the user's own games.
    """
    from .write_guard import is_ours

    cleared = 0
    for entry in shortcuts_dict.values():
        if not isinstance(entry, dict) or not entry.get("LastPlayTime"):
            continue
        if not is_ours(entry, launcher_path):
            continue
        entry["LastPlayTime"] = 0
        cleared += 1
    return cleared


async def reset_lastplaytime_once(svc: Any) -> None:
    """Run the migration against ``svc`` unless its marker already exists.

    ``svc`` is the ``ShortcutService`` facade; this reads
    ``_games_map_path``, ``_shortcuts``, ``_launcher_path`` and calls
    ``_ensure_shortcuts_root`` / ``_save_all`` on it.
    """
    marker = Path(svc._games_map_path).parent / "lastplaytime_reset.done"
    if await asyncio.to_thread(marker.exists):
        return

    svc._shortcuts = svc._ensure_shortcuts_root(svc._shortcuts)
    launcher = getattr(svc, "_launcher_path", "") or ""
    cleared = _clear_our_stamps(svc._shortcuts["shortcuts"], launcher)

    if cleared:
        await svc._save_all()
    # Mark done even when nothing changed, so we don't rescan every
    # sync; a failed marker write just retries next sync (idempotent).
    await asyncio.to_thread(touch_marker, marker)
    logger.info(
        "[ShortcutService] LastPlayTime reset migration: cleared %d shortcut(s)",
        cleared,
    )
