"""services/shortcut/vdf_shortcuts.py — Escape-hatch read/write helpers.

Provides direct access to the shortcuts list for the UI layer.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _VdfShortcutsMixin:
    """Escape-hatch shortcut read/write."""

    # These are provided by the ShortcutService facade at runtime
    _shortcuts: dict[str, Any]

    # Assume host provides these async load/save primitives
    # async def _load_shortcuts(self) -> None: ...
    # async def _save_all(self) -> None: ...

    async def read_shortcuts(self: Any, *, from_disk: bool = False) -> dict[str, Any]:
        """Return the raw shortcuts dictionary.

        Used by the UI layer to list/view all current shortcuts
        without making modifications.

        ``from_disk`` forces a re-read instead of returning the long-lived
        in-memory cache (``_load_shortcuts`` is idempotent and never
        re-reads). Steam holds ``shortcuts.vdf`` in memory too and flushes
        its own copy over ours, so a row we added this session can be gone
        from disk while our cache still reports it present — measured: an
        auth shortcut written at 01:39 was absent at 01:58, and the cache
        went on answering "already in VDF" so nothing ever re-created it.
        Callers that must not be fooled by that pass ``from_disk=True``.
        """
        if from_disk:
            self._shortcuts_loaded = False
        await self._load_shortcuts()

        # We store internally as {"shortcuts": {"0": {}, "1": {}}}
        # Return a copy to avoid accidental external mutation
        if not isinstance(self._shortcuts, dict):
            return {"shortcuts": {}}

        return dict(self._shortcuts)

    async def write_shortcuts(
        self: Any,
        data: dict[str, Any],
        *,
        allow_foreign_drops: frozenset[int] = frozenset(),
    ) -> None:
        """Overwrite the entire shortcuts dictionary and save.

        Used as an escape hatch for direct modifications. Because this
        replaces the whole dict, a caller that built ``data`` from a
        stale or partial read would drop everything missing from it —
        so the write still goes through ``_save_all``'s guard.
        ``allow_foreign_drops`` names the appids of any non-Unifideck
        rows the caller means to remove.
        """
        self._shortcuts = dict(data)
        await self._save_all(allow_foreign_drops=allow_foreign_drops)
