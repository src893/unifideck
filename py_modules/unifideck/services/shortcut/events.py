"""services/shortcut/events.py — Event handlers for shortcut lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events, Game
from unifideck.event_bus.event_bus_devex import subscribe

logger = logging.getLogger(__name__)


def _find_icon_for_appid(grid_dir: str, appid: int) -> str:
    """Return the absolute icon path Steam's grid dir holds for ``appid``.

    Tries the two extensions SteamGridDB exports use, in order.
    Returns ``""`` when no file is found — caller treats empty
    as "no update needed".
    """
    unsigned = (appid & 0xFFFFFFFF) | 0x80000000
    for ext in (".jpg", ".png"):
        candidate = Path(grid_dir) / f"{unsigned}_icon{ext}"
        if candidate.exists():
            return str(candidate)
    return ""


def _apply_icon_updates(
    shortcuts: dict[str, Any], grid_dir: str, launcher_path: str = "",
) -> int:
    """Walk *our* shortcuts and update each entry's ``icon`` field in place.

    Returns the count of entries actually mutated (skips entries
    whose ``icon`` field already points at the on-disk file).

    Foreign entries are skipped. ``grid/`` is shared with every other
    non-Steam tool on the device, so a file named for a foreign
    shortcut's appid is *their* artwork — pointing that entry's ``icon``
    at our resolved path rewrites a tile we do not own.
    """
    from .write_guard import is_ours

    updated = 0
    for entry in shortcuts.values():
        if not isinstance(entry, dict) or not is_ours(entry, launcher_path):
            continue
        appid = entry.get("appid")
        if not isinstance(appid, int):
            continue
        icon_path = _find_icon_for_appid(grid_dir, appid)
        if not icon_path or entry.get("icon", "") == icon_path:
            continue
        entry["icon"] = icon_path
        updated += 1
    return updated


def _data_dir_of(svc: Any) -> str:
    """Plugin data dir for *svc*, or ``""`` when it cannot be derived.

    ``games.map`` sits at the data-dir root, so its parent is the dir.
    Returning ``""`` disables backups rather than raising — this runs on
    a background artwork handler, and a stub service without the
    attribute must not break the icon refresh.
    """
    games_map = getattr(svc, "_games_map_path", "") or ""
    return str(Path(games_map).parent) if games_map else ""


async def _update_icons_from_grid(svc: Any) -> int:
    """Scan grid dir for icon files and update shortcuts.vdf.

    Called after the artwork phase completes so any newly-
    downloaded icon files are picked up. Returns the number of
    shortcuts whose icon field was updated.
    """
    shortcuts_path = getattr(svc, "_shortcuts_path", None)
    if not shortcuts_path:
        logger.warning("[ShortcutService] no shortcuts_path — cannot update icons")
        return 0

    grid_dir = str(Path(shortcuts_path).parent / "grid")
    try:
        from unifideck.services.shortcut.persistence import (
            read_vdf,
            vdf_write_lock,
            write_vdf,
        )
    except Exception:
        logger.exception("[ShortcutService] failed to import VDF deps")
        return 0

    # The second read-modify-write of shortcuts.vdf in the package, and the
    # reason the lock is module-level rather than owned by the service: this
    # runs off the artwork phase, concurrently with a reconcile doing its own
    # read-edit-write, and unsynchronised they both start from the same
    # snapshot so one silently discards the other's entries. See
    # ``ShortcutService._save_all``.
    async with vdf_write_lock():
        data = await read_vdf(shortcuts_path)
        shortcuts = data.get("shortcuts")
        if not isinstance(shortcuts, dict):
            return 0

        launcher = getattr(svc, "_launcher_path", "") or ""
        updated = _apply_icon_updates(shortcuts, grid_dir, launcher)
        if updated > 0:
            await write_vdf(shortcuts_path, data, _data_dir_of(svc))
            logger.info(
                "[ShortcutService] updated icons for %d shortcuts in shortcuts.vdf",
                updated,
            )
    return updated

if TYPE_CHECKING:
    # This is a mixin; `self` will be the ShortcutService facade
    # at runtime. The facade provides ``mark_installed``,
    # ``mark_uninstalled``, ``remove_game`` and ``reconcile`` via
    # ``_GamesMapMixin``. Mypy doesn't see the multiple-inheritance
    # composition here (this file is imported standalone), so we
    # declare the protocol of methods we rely on as
    # TYPE_CHECKING-only forward refs.
    from collections.abc import Sequence


class EventsMixin:
    """Event subscriptions for ShortcutService.

    Expects the composing class to provide the methods listed
    in the ``if TYPE_CHECKING`` block below. Each handler reads
    its payload from the bus and delegates to the facade.
    """

    if TYPE_CHECKING:
        # Type-only declarations — implementations come from
        # ``_GamesMapMixin`` at runtime through the MRO. These
        # stubs exist purely so mypy knows the methods exist on
        # ``self`` when this module is type-checked in isolation.
        #
        # Signatures match ``_GamesMapMixin`` exactly: ``async def
        # foo(...) -> T`` (not ``def foo(...) -> Awaitable[T]``).
        # Lot 12d fix: previously the stubs returned ``Awaitable[T]``
        # which is semantically equivalent but mypy strict considered
        # them incompatible with the ``async def`` definitions in
        # ``_GamesMapMixin`` — surfaced as 3× ``[misc]`` "incompatible
        # definition in base class" errors on the facade class
        # body in service.py.
        async def remove_game(self, app_id: int) -> bool: ...
        async def mark_installed(
            self, store: str, store_game_id: str,
            exe_path: str, install_path: str,
        ) -> int | None: ...
        async def mark_uninstalled(
            self, store: str, store_game_id: str,
        ) -> int | None: ...
        async def reconcile(
            self, games: Sequence[Game], *, force: bool = ...,
            valid_stores: set[str] | None = ...,
        ) -> dict[str, int]: ...

    @subscribe(Events.DOWNLOAD_COMPLETE)
    async def _on_download_complete(self, **kwargs: Any) -> None:
        """Flip the existing shortcut's install state to installed.

        The shortcut was created at sync time by reconcile with
        ``tags["2"] = "Not Installed"`` — we only flip the tag and
        write the games.map row. The shortcut's appid is preserved
        so Steam playtime / artwork / categories survive the
        transition.
        """
        game = kwargs.get("game")
        if isinstance(game, Game):
            await self.mark_installed(
                game.store, game.store_game_id,
                game.exe_path or "", game.install_path or "",
            )

    @subscribe(Events.GAME_UNINSTALLED)
    async def _on_game_uninstalled(self, **kwargs: Any) -> None:
        """Flip the existing shortcut's install state to not-installed.

        Symmetric with ``_on_download_complete``: the user still
        owns the game, they just removed the bytes. Keep the
        shortcut and its appid so the frontend cache and detail-page
        UI continue to recognise it. The emitters in
        ``stores/{epic,amazon}/install.py`` pass ``store`` + ``game_id``
        (not ``app_id``), so we look the shortcut up by those.
        """
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if isinstance(store, str) and isinstance(game_id, str):
            await self.mark_uninstalled(store, game_id)

    @subscribe(Events.SYNC_COMPLETE)
    async def _on_sync_complete(self, **kwargs: Any) -> None:
        """Reconcile shortcuts against the new library state.

        After reconciling, emit ``SHORTCUT_RECONCILE_COMPLETE``
        with the per-batch counters so the frontend can prompt
        the user for a Steam restart when any shortcuts were
        added or removed (Steam holds shortcuts.vdf in memory and
        overwrites our writes on its next shutdown otherwise).
        """
        games = kwargs.get("games", [])
        if not games:
            return
        is_force = bool(kwargs.get("is_force", False))
        # Widen the stale-sweep to every registered store (not just the
        # ones that returned games) so shortcuts for a logged-out or
        # empty store — phantom Ubisoft entries, the legacy
        # ``microsoft:ms-auth`` row — get dropped on this sync instead of
        # lingering forever.
        registered = kwargs.get("registered_stores")
        valid_stores = set(registered) if registered else None
        logger.info(
            "[ShortcutService] SYNC_COMPLETE → reconciling %d games "
            "(force=%s, valid_stores=%s)",
            len(games), is_force, sorted(valid_stores) if valid_stores else None,
        )
        result = await self.reconcile(
            games, force=is_force, valid_stores=valid_stores,
        )
        added = result.get("added", 0)
        removed = result.get("removed", 0)
        kept = result.get("kept", 0)
        # ``self._bus`` is provided by the host (ShortcutService
        # facade); silently skip the emit if for some reason it's
        # unavailable so a missing bus never breaks reconcile.
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        await bus.emit(
            Events.SHORTCUT_RECONCILE_COMPLETE,
            added=added, removed=removed, kept=kept,
            total=len(games),
        )

    @subscribe(Events.POST_SYNC_PHASE_CHANGED)
    async def _on_artwork_phase_done(self, **kwargs: Any) -> None:
        """After artwork download completes, update icon paths in
        shortcuts.vdf so Steam displays the shortcut icon in the
        library list and desktop mode."""
        if kwargs.get("phase") != "artwork":
            return
        if kwargs.get("active") is not False:
            return
        logger.info("[ShortcutService] artwork phase done — updating icons")
        await _update_icons_from_grid(self)
