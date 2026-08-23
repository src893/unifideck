"""Reconcile phases mixin — bulk shortcut sync from a library snapshot.

OP-14c-bis | py_modules/unifideck/services/shortcut/reconcile_phases.py

Extracted from ``games_map_mixin.py`` (2026-05-17) to keep the
host file under the 550 LOC volumetry cap. Contains the bulk
reconcile method + its five phase helpers — the set-diff
algorithm that adds missing shortcuts, removes stale ones, and
reclaims orphaned entries by AppID from the persistent registry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .games_map import UNIFIDECK_TAG, GameMapEntry, generate_app_id
from .orphan_scan import _is_launcher_exe
from .reconcile_helpers import (
    build_launch_index,
    dedup_shortcuts,
    log_restart_banner,
)

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)


class _ReconcilePhasesMixin:
    """Bulk shortcut reconciliation for :class:`ShortcutService`.

    Assumes the host provides ``_load_shortcuts``, ``_load_games_map``,
    ``_save_all``, ``_ensure_shortcuts_root``,
    ``_find_existing_shortcut_key``, ``_allocate_new_shortcut_key``,
    ``_launcher_path``, ``_shortcuts``, ``_games_map``.
    """

    @staticmethod
    def _is_stale_managed_shortcut(
        entry: Any,
        valid_app_ids: set[int],
        valid_stores: set[str] | None = None,
        launcher_path: str = "",
    ) -> bool:
        """True if ``entry`` is a Unifideck-managed shortcut no longer needed.

        Ownership is decided on the shortcut's ``Exe`` field: only
        entries whose ``Exe`` points at our ``bin/unifideck-launcher``
        are ever swept (via :func:`orphan_scan._is_launcher_exe`, a
        basename match — the plugin dir differs across installs). A
        foreign shortcut (NonSteamLaunchers', or a manually-added one)
        can carry a ``"<store>:<id>"``-shaped ``LaunchOptions`` token or
        even our stale ``UNIFIDECK_TAG``, so LaunchOptions/tags alone
        cannot distinguish it from ours — matching on them deleted the
        user's own non-Steam shortcuts (UD-006). The launcher ``Exe`` is
        the one marker a foreign scanner cannot forge.

        Beyond the Exe gate, identification of *which* Unifideck games a
        shortcut maps to stays **LaunchOptions-based** (regex on
        ``"<store>:<game_id>"``) rather than tag-based — Steam can strip
        our ``UNIFIDECK_TAG`` on update / by user edit, but it preserves
        ``LaunchOptions`` reliably.

        Auth shortcuts (``ubisoft:upc-auth`` and any
        ``auth-*``-tagged entry) are explicitly preserved —
        their lifecycle is owned by ``services/shortcut/shortcut.py``.

        When ``valid_stores`` is supplied, only sweep shortcuts
        whose store prefix is in that set — this is how staging
        avoided nuking the user's Epic shortcuts after they
        logged out of Epic.
        """
        from .launch_options import get_full_id, get_store_prefix

        if not isinstance(entry, dict):
            return False
        launch = entry.get("LaunchOptions", "") or ""
        full_id = get_full_id(launch) if isinstance(launch, str) else None
        if not _ReconcilePhasesMixin._is_managed_sweepable(entry, full_id):
            return False
        # Ownership gate: never sweep a shortcut we didn't create. A
        # foreign shortcut whose Exe was left intact (or a Unifideck one
        # whose Exe a foreign scanner rewrote) is handed to
        # ``orphan_scan``'s recover path instead of being deleted here.
        exe_raw = entry.get("Exe") or entry.get("exe") or ""
        exe = exe_raw.strip().strip('"') if isinstance(exe_raw, str) else ""
        if not _is_launcher_exe(exe, launcher_path):
            return False
        if valid_stores is not None and full_id is not None:
            store = get_store_prefix(launch)
            if store and store not in valid_stores:
                return False
        return entry.get("appid") not in valid_app_ids

    @staticmethod
    def _is_managed_sweepable(entry: dict[str, Any], full_id: Any) -> bool:
        """True if *entry* is a Unifideck-managed, non-protected shortcut.

        The protected/auth early-exit + managed-by-options-or-tag check,
        extracted from :meth:`_is_stale_managed_shortcut` to keep that
        method under the cognitive-complexity cap. Returns ``False`` for
        protected/auth shortcuts and for entries we never managed;
        behaviour is identical to the inline chain it replaced.
        """
        from .protected import is_protected

        # Centralised protected-set check — replaces the previous
        # hardcoded ``ubisoft:upc-auth`` literal so new stores can
        # register their auth shortcuts in one place.
        if is_protected(full_id):
            return False
        tags = entry.get("tags", {})
        tags_dict = tags if isinstance(tags, dict) else {}
        is_auth_tag = any(
            str(t).startswith("auth-") for t in tags_dict.values()
        )
        if is_auth_tag:
            return False
        is_managed_by_options = full_id is not None
        is_managed_by_tag = any(
            t == UNIFIDECK_TAG for t in tags_dict.values()
        )
        return is_managed_by_options or is_managed_by_tag

    async def reconcile(
        self: Any, games: list[Game], *, force: bool = False,
        valid_stores: set[str] | None = None,
    ) -> dict[str, int]:
        """Bulk-sync all shortcuts from a list of Games.

        Regular sync (``force=False``): **additive** — new
        ``store:game_id`` pairs get a shortcut; already-existing
        entries are left untouched. Mirrors staging's
        ``add_games_batch`` behavior.

        Force sync (``force=True``): **overwriting** — existing
        entries have their ``AppName``, ``exe``, ``tags``, and
        ``icon`` fields updated to match current metadata, while
        preserving their ``appid`` so artwork and playtime survive
        the rewrite. Mirrors staging's ``force_update_games_batch``.

        ``valid_stores`` overrides the set of store prefixes whose
        stale shortcuts may be swept. By default only stores that
        returned games are touched (so logging out of one store left
        its now-orphaned shortcuts behind forever). Passing the full
        set of *registered* stores lets reconcile drop shortcuts for a
        store that returned nothing this sync — e.g. phantom Ubisoft
        entries and the legacy ``microsoft:ms-auth`` row — so affected
        libraries self-heal on the next sync.
        """
        await self._load_shortcuts()
        await self._load_games_map()
        await self._reset_lastplaytime_once()

        from .registry import load_registry, save_registry

        # Explicit path: ``registry``'s default is expanded at import
        # time, so omitting it reads and writes a fixed location
        # regardless of how this service was configured.
        registry_path = self._registry_path
        registry = load_registry(registry_path)
        counts: dict[str, int] = self._apply_reconcile_phases(
            games, registry, force=force, valid_stores=valid_stores,
        )
        if counts["added"] or counts["removed"] or counts["reclaimed"]:
            await self._save_all()
        if counts["added"] or counts["reclaimed"]:
            save_registry(registry, registry_path)
        self._log_reconcile_result(games, counts)
        return counts

    def _apply_reconcile_phases(
        self: Any, games: list[Game], registry: dict[str, Any], *, force: bool,
        valid_stores: set[str] | None = None,
    ) -> dict[str, int]:
        """Prune, sync, drop-stale, then dedup; return the counts dict."""
        launcher = getattr(self, "_launcher_path", "") or ""
        valid_keys = {f"{g.store}:{g.store_game_id}" for g in games}
        valid_app_ids = self._compute_valid_app_ids(games, launcher)
        # Default to stores-with-games; a caller (the post-sync
        # reconcile) can widen this to every registered store so stale
        # shortcuts for a logged-out / empty store also get swept.
        if valid_stores is None:
            valid_stores = {g.store for g in games}
        removed = self._reconcile_phase_prune_map(valid_keys)
        self._shortcuts = self._ensure_shortcuts_root(self._shortcuts)
        shortcuts_dict = self._shortcuts["shortcuts"]
        added, kept, reclaimed = self._reconcile_phase_sync_games(
            games, shortcuts_dict, registry, force=force,
        )
        removed += self._reconcile_phase_drop_stale(
            shortcuts_dict, valid_app_ids, valid_stores, launcher,
        )
        # Dedup AFTER add/drop so reclaimed orphans count toward the
        # winners' scores. Steam occasionally creates duplicate VDF
        # entries with the same launch-options (in-memory desync,
        # crash recovery). Scoped to our own entries by ``launcher``.
        removed += dedup_shortcuts(shortcuts_dict, launcher)
        return {
            "added": added, "removed": removed,
            "kept": kept, "reclaimed": reclaimed,
        }

    @staticmethod
    def _compute_valid_app_ids(games: list[Game], launcher: str) -> set[int]:
        """The set of app_ids the current library should keep."""
        return {
            g.app_id or generate_app_id(launcher, f"{g.store}:{g.store_game_id}")
            for g in games
        }

    def _log_reconcile_result(
        self: Any, games: list[Game], counts: dict[str, int],
    ) -> None:
        """Log the reconcile tally + a Steam-restart banner when changed."""
        logger.info(
            "[ShortcutService] reconcile: %d games → "
            "added=%d kept=%d removed=%d reclaimed=%d",
            len(games), counts["added"], counts["kept"],
            counts["removed"], counts["reclaimed"],
        )
        if counts["added"] > 0 or counts["removed"] > 0:
            log_restart_banner(
                counts["added"], counts["removed"], counts["reclaimed"],
            )

    async def _reset_lastplaytime_once(self: Any) -> None:
        """One-time ``LastPlayTime`` migration — see :mod:`lastplaytime_reset`.

        Body lives in its own module: this file sits against the 550-LOC
        volumetry cap, and a one-shot migration is the natural thing to
        lift out of the steady-state reconcile path.
        """
        from .lastplaytime_reset import reset_lastplaytime_once

        await reset_lastplaytime_once(self)

    # ── Phase helpers ──────────────────────────────────────

    def _reconcile_phase_prune_map(
        self: Any, valid_keys: set[str],
    ) -> int:
        """Phase 1: drop ``_games_map`` keys absent from ``valid_keys``."""
        stale_keys = [k for k in self._games_map if k not in valid_keys]
        for key in stale_keys:
            del self._games_map[key]
        return len(stale_keys)

    def _reconcile_phase_sync_games(
        self: Any,
        games: list[Game],
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        *,
        force: bool = False,
    ) -> tuple[int, int, int]:
        """Phase 2: ensure each game has a map entry and a VDF entry.

        Matching (both staging behaviours):

        * **LaunchOptions** — ``store:game_id`` from the shortcut's
          ``LaunchOptions`` field. Stable across title changes; the
          primary match key. When found, we KNOW this game already
          has a shortcut even if the app_id has drifted.
        * **AppID** — the integer stored in ``shortcut["appid"]``.
          Falls back when LaunchOptions is missing or corrupted.

        Regular sync (``force=False``): matches existing shortcuts
        and **keeps** them — ``added`` only ticks for truly new
        ``store:game_id`` pairs. Moves ``kept`` for existing matches.
        Mirrors staging's ``add_games_batch(skip if exists)``.

        Force sync (``force=True``): matches existing shortcuts and
        **updates** their ``AppName``, ``exe``, ``tags``, and
        ``icon`` fields to match current metadata, while preserving
        the ``appid`` so artwork and playtime carry through the
        rewrite. Mirrors staging's ``force_update_games_batch``.
        """
        # Build a lookup of LaunchOptions → shortcut_key BEFORE
        # iterating games — one O(N) pass across shortcuts, then
        # O(1) per-game. Mirrors staging's approach at
        # shortcuts_manager.py line 1708-1713.
        launcher = getattr(self, "_launcher_path", "") or ""
        launch_to_key = build_launch_index(shortcuts_dict, launcher)
        added = kept = reclaimed = 0
        for game in games:
            outcome = self._sync_one_game(
                game, shortcuts_dict, registry, launch_to_key,
                launcher=launcher, force=force,
            )
            if outcome == "added":
                added += 1
            elif outcome == "reclaimed":
                reclaimed += 1
            else:
                kept += 1
        return added, kept, reclaimed

    def _sync_one_game(
        self: Any,
        game: Game,
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        launch_to_key: dict[str, str],
        *,
        launcher: str,
        force: bool,
    ) -> str:
        """Reconcile a single ``game`` into ``shortcuts_dict``.

        Returns one of ``"added"``, ``"kept"``, ``"reclaimed"`` for
        the caller's tally. Side effects: updates ``self._games_map``,
        ``shortcuts_dict``, ``registry``.
        """
        from .registry import register

        key = f"{game.store}:{game.store_game_id}"
        app_id = game.app_id or generate_app_id(launcher, key)
        self._update_games_map_row(game, key, app_id)

        if self._try_reclaim_orphan(shortcuts_dict, registry, game, key):
            return "reclaimed"

        # ── Match by LaunchOptions (primary — staging behaviour).
        # Only (re)register on a forced update.
        existing_key = launch_to_key.get(key)
        if existing_key is not None:
            if force:
                self._update_existing_shortcut(
                    shortcuts_dict[existing_key], game, app_id, launcher,
                )
                register(registry, key, app_id, game.title)
            return "kept"

        # ── Match by AppID (fallback — LaunchOptions missing).
        existing_key = self._find_existing_shortcut_key(
            shortcuts_dict, app_id, launcher,
        )
        if existing_key is not None:
            if force:
                self._update_existing_shortcut(
                    shortcuts_dict[existing_key], game, app_id, launcher,
                )
            register(registry, key, app_id, game.title)
            return "kept"

        # ── New shortcut
        new_key = self._allocate_new_shortcut_key(shortcuts_dict)
        shortcuts_dict[new_key] = self._build_shortcut_entry(game, app_id)
        register(registry, key, app_id, game.title)
        return "added"

    def _update_games_map_row(
        self: Any, game: Game, key: str, app_id: int,
    ) -> None:
        """Maintain the games.map exe row for *game* (installed-only).

        games.map is the launcher's exe-path lookup, only for games
        with a local executable (xCloud titles need no row). Library-
        sourced sync ``Game`` objects do NOT carry ``exe_path`` (it's
        resolved at install time by the worker via ``mark_installed``),
        so an installed game arriving with an empty ``exe`` must NOT
        wipe its existing entry — only a truly-uninstalled game drops it.
        """
        exe = game.exe_path or ""
        if game.installed and exe:
            self._games_map[key] = GameMapEntry(
                exe=exe, work_dir=game.install_path or "", app_id=app_id,
            )
        elif not (game.installed and key in self._games_map):
            self._games_map.pop(key, None)

    def _try_reclaim_orphan(
        self: Any,
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        game: Game,
        key: str,
    ) -> bool:
        """Reclaim an orphaned shortcut via the registry; True if reclaimed."""
        from .registry import get_registered_appid, register
        registered = get_registered_appid(registry, key)
        if registered is None:
            return False
        launcher = getattr(self, "_launcher_path", "") or ""
        ord_key = self._find_existing_shortcut_key(
            shortcuts_dict, registered, launcher,
        )
        if ord_key is None:
            return False
        self._reclaim_orphan(shortcuts_dict[ord_key], game, registered)
        register(registry, key, registered, game.title)
        return True

    def _update_existing_shortcut(
        self: Any,
        entry: dict[str, Any],
        game: Game,
        app_id: int,
        launcher: str,
    ) -> None:
        """Force-update an existing shortcut in-place — preserves ``appid``.

        Only called during force sync (``force=True``).
        Updates ``AppName``, ``Exe``, ``tags``, and ``LaunchOptions``
        while leaving ``appid`` unchanged so artwork files and Steam's
        playtime tracking survive the rewrite. The ``icon`` field is
        set from ``game.icon_url`` when available (the artwork phase
        populates it from on-disk grid files).
        """
        exe_quoted = f'"{launcher}"' if launcher else '""'
        entry["AppName"] = game.title
        entry["Exe"] = exe_quoted
        entry["LaunchOptions"] = f"{game.store}:{game.store_game_id}"
        if game.icon_url:
            entry["icon"] = game.icon_url
        tags_dict = entry.get("tags", {})
        if isinstance(tags_dict, dict):
            tags_dict["0"] = UNIFIDECK_TAG
            tags_dict["1"] = game.store
            tags_dict["2"] = "" if game.installed else "Not Installed"

    def _reclaim_orphan(
        self: Any, entry: dict[str, Any], game: Game, app_id: int,
    ) -> None:
        """Rewrite ``entry`` in place — restores ownership while keeping AppID."""
        from .launch_options import preserve_user_params

        launcher = getattr(self, "_launcher_path", "") or ""
        current_options = entry.get("LaunchOptions", "")
        target = f"{game.store}:{game.store_game_id}"
        preserved = preserve_user_params(
            current_options if isinstance(current_options, str) else "",
            target,
        )
        entry["appid"] = app_id
        entry["AppName"] = game.title
        if launcher:
            entry["Exe"] = f'"{launcher}"'
        entry["LaunchOptions"] = preserved
        entry["icon"] = game.icon_url or entry.get("icon", "") or ""
        entry["tags"] = {
            "0": UNIFIDECK_TAG,
            "1": game.store,
            "2": "" if game.installed else "Not Installed",
        }

    def _reconcile_phase_drop_stale(
        self: Any,
        shortcuts_dict: dict[str, Any],
        valid_app_ids: set[int],
        valid_stores: set[str] | None = None,
        launcher_path: str = "",
    ) -> int:
        """Phase 3: delete Unifideck-managed shortcuts no longer needed."""
        keys_to_delete = [
            vdf_key for vdf_key, entry in shortcuts_dict.items()
            if self._is_stale_managed_shortcut(
                entry, valid_app_ids, valid_stores, launcher_path,
            )
        ]
        for key in keys_to_delete:
            del shortcuts_dict[key]
        return len(keys_to_delete)

    def _build_shortcut_entry(
        self: Any, game: Game, app_id: int,
    ) -> dict[str, Any]:
        """Construct a shortcuts.vdf entry dict for ``game``.

        Every Unifideck-managed shortcut points its ``Exe`` at the
        plugin's ``bin/unifideck-launcher`` script and stores the
        ``"<store>:<store_game_id>"`` token in ``LaunchOptions``.
        Anchoring on the launcher keeps the AppID stable across
        install / uninstall transitions.
        """
        launcher = getattr(self, "_launcher_path", "") or ""
        exe_quoted = f'"{launcher}"' if launcher else '""'
        start_dir = f'"{game.install_path}"' if game.install_path else '""'
        launch_options = f"{game.store}:{game.store_game_id}"
        cover_icon = game.icon_url or ""
        return {
            "appid": app_id,
            "AppName": game.title,
            "Exe": exe_quoted,
            "StartDir": start_dir,
            "icon": cover_icon,
            "ShortcutPath": "",
            "LaunchOptions": launch_options,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "DevkitOverrideAppID": 0,
            # New shortcuts start with no play history (0 = never played).
            # Steam stamps the real time on first launch, and
            # ``_update_existing_shortcut`` preserves it on later syncs.
            # Hardcoding ``time.time()`` here stamped every game with the
            # same sync timestamp, so the App-Details "Last Played" row
            # showed one identical date across the whole library.
            "LastPlayTime": 0,
            "FlatpakAppID": "",
            "tags": {
                "0": UNIFIDECK_TAG,
                "1": game.store,
                "2": "" if game.installed else "Not Installed",
            },
        }
