"""Persistent auth shortcuts for wrapper stores.

py_modules/unifideck/stores/shared/auth_shortcut.py

Wrapper stores sign in by running their vendor client. In Desktop Mode the
client can be spawned directly, but **in Gaming Mode it must come from a
Steam shortcut** — a bare subprocess gets no gamescope session, so its
window never appears. That is why signing in works on the desktop and
silently fails on the deck without one of these.

Generic over the store: everything that differs is in
``AuthShortcutSpec``, so EA App is a spec rather than another module.

Two Steam behaviours drive the shape here:

* **Steam reads ``shortcuts.vdf`` only at startup.** A shortcut written
  this session is absent from Steam's in-memory app store, and ``RunGame``
  on its appid fails with "Game configuration unavailable". The frontend
  handles that with a temporary shortcut; this module just has to return a
  ``launcher_path`` so it can.
* **The appid must be derived, not invented** — ``generate_app_id`` is a
  CRC of launcher plus identity, and the same inputs must always give the
  same appid or the shortcut is orphaned on the next run.

Ubisoft keeps its own richer implementation for now (it also prunes legacy
template shortcuts and integrates with its registry). Migrating it onto
this is a follow-up that wants device testing, since it is a shipped and
working auth path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthShortcutSpec:
    """Everything store-specific about one wrapper store's auth shortcut."""

    store: str
    #: ``store:id`` written into LaunchOptions, e.g. ``battlenet:bnet-auth``.
    store_game_id: str
    #: Shortcut name in Steam, e.g. ``Battle.net``.
    display_name: str
    #: Env token telling the launcher this run is a sign-in.
    action_env: str
    #: Env token naming the prefix directory, e.g.
    #: ``UNIFIDECK_BATTLENET_PREFIX_NAME``. Required whenever the auth
    #: prefix is not named after the id in ``store_game_id`` — the launcher
    #: otherwise derives the prefix from ``ctx.game_id`` and signs the user
    #: in to an empty directory. Set both this and :attr:`prefix_name`.
    prefix_env: str | None = None
    #: Directory name of the auth prefix, e.g. ``.bnet-auth``.
    prefix_name: str | None = None
    #: Milliseconds the frontend should wait for Steam to register it.
    launch_wait_ms: int = 3000

    def launch_options(self, launcher_path: str) -> str:
        """LaunchOptions for the shortcut. Must be byte-stable.

        :func:`ensure_auth_shortcut` compares against this exactly, so any
        change here orphans existing shortcuts — which is why it repairs a
        row whose id matches but whose options differ, rather than leaving
        the user with a tile that launches the wrong thing.
        """
        del launcher_path
        options = f"{self.store_game_id} {self.action_env}=auth"
        if self.prefix_env and self.prefix_name:
            options += f" {self.prefix_env}={self.prefix_name}"
        return options


def launcher_path_for(plugin_dir: str | None) -> str:
    """Absolute path to the shortcut launcher binary."""
    base = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parents[3]
    return str(base / "bin" / "unifideck-launcher")


def _entry_matches(entry: Any, spec: AuthShortcutSpec) -> bool:
    """True when *entry*'s launch id is this store's auth shortcut.

    Identity only — deliberately no ownership check, because the one
    caller that merely *reads* (:func:`find_in_vdf`) must not miss a
    row: a false negative there makes ``ensure_auth_shortcut`` add a
    second auth tile rather than reuse the existing one. Callers that
    go on to *write* pair this with :func:`_is_repairable`.

    Tightened from the original ``spec.store_game_id in options``
    substring test to a canonical ``store:id`` compare, so a row that
    merely contains the token no longer matches. The compare stays on
    the canonical head only, never the whole string —
    :func:`repair_launch_options` exists precisely to fix rows whose
    tail has drifted, and matching the tail would make it unable to
    find its own repair targets.
    """
    from unifideck.services.shortcut.launch_options import get_full_id

    if not isinstance(entry, dict):
        return False
    options = str(entry.get("LaunchOptions") or "")
    return get_full_id(options) == spec.store_game_id


def _is_repairable(entry: dict[str, Any], launcher_path: str) -> bool:
    """True when it is safe to rewrite *entry*'s fields.

    Ours by the ``Exe`` gate, or a row with no ``Exe`` at all. The
    second case is the one this repair exists for — a bare row launches
    nothing, so it cannot be a working shortcut of the user's, and
    giving it our launcher is what makes it functional again.
    """
    from unifideck.services.shortcut.write_guard import is_ours

    if is_ours(entry, launcher_path):
        return True
    exe = entry.get("Exe") or entry.get("exe") or ""
    return not (exe.strip().strip('"') if isinstance(exe, str) else exe)


def find_in_vdf(shortcuts: dict[str, Any], spec: AuthShortcutSpec) -> int | None:
    """Existing appid for this auth shortcut, or None."""
    for entry in shortcuts.values():
        if _entry_matches(entry, spec):
            appid = entry.get("appid") if isinstance(entry, dict) else None
            if isinstance(appid, int):
                return appid
    return None


def repair_launch_options(
    shortcuts: dict[str, Any], spec: AuthShortcutSpec, launcher_path: str,
) -> bool:
    """Rewrite a matching row whose LaunchOptions have gone stale.

    A row is matched on ``store_game_id`` alone, so it survives a change to
    the rest of the string — adding the prefix-name token, for instance.
    Without this the old row keeps winning the ``find_in_vdf`` lookup and
    the shortcut goes on launching with the previous, wrong arguments; the
    user sees a tile that works and a sign-in that silently does nothing.

    Returns True when something was changed and the VDF needs writing.
    """
    expected = spec.launch_options(launcher_path)
    repaired = False
    for entry in shortcuts.values():
        if not _entry_matches(entry, spec):
            continue
        # The rewrite below is what needs ownership, not the lookup.
        if not _is_repairable(entry, launcher_path):
            logger.warning(
                "[%s] a shortcut carries our auth id but is not ours "
                "(Exe=%r) — leaving it alone",
                spec.store, entry.get("Exe"),
            )
            continue
        if str(entry.get("LaunchOptions") or "") == expected:
            continue
        logger.info(
            "[%s] repairing stale auth LaunchOptions: %r -> %r",
            spec.store, entry.get("LaunchOptions"), expected,
        )
        entry["LaunchOptions"] = expected
        repaired = True
    return repaired


def _build_entry(
    spec: AuthShortcutSpec, launcher_path: str, appid: int,
) -> dict[str, Any]:
    return {
        "appid": appid,
        "AppName": spec.display_name,
        "Exe": f'"{launcher_path}"',
        "StartDir": f'"{Path(launcher_path).parent}"',
        "LaunchOptions": spec.launch_options(launcher_path),
        # Hidden: it is an infrastructure tile, not a game the user browses.
        "IsHidden": 1,
        "AllowDesktopConfig": 1,
        "OpenVR": 0,
        "tags": {"0": spec.display_name},
    }


async def _read_shortcuts_from_disk(shortcut_service: Any) -> dict[str, Any]:
    """The VDF as it is on disk, not as the service last cached it.

    ``ShortcutService`` keeps ``shortcuts.vdf`` in memory for the process
    lifetime. Steam keeps its own copy and flushes it over ours, so a row we
    wrote this session can be gone from disk while the cache still reports
    it — measured on-device: an auth shortcut written at 01:39 was absent at
    01:58, and every later check answered "already in VDF", so nothing ever
    re-created it and the tile stayed missing from Steam.

    Falls back to the plain read when the service predates the keyword (test
    doubles, and any third-party shortcut service): a stale answer is worse
    than a fresh one but far better than an exception on the sign-in path.
    """
    try:
        data = await shortcut_service.read_shortcuts(from_disk=True)
    except TypeError:
        data = await shortcut_service.read_shortcuts()
    return dict(data) if isinstance(data, dict) else {"shortcuts": {}}


async def ensure_auth_shortcut(
    shortcut_service: Any,
    spec: AuthShortcutSpec,
    plugin_dir: str | None,
) -> int | None:
    """Create or repair the persistent auth shortcut. Returns its unsigned appid.

    Never raises: a missing shortcut service or an unwritable VDF degrades
    to ``None``, and the frontend falls back to a temporary shortcut.
    """
    if shortcut_service is None:
        logger.debug("[%s] no shortcut service — cannot create auth shortcut", spec.store)
        return None

    launcher_path = launcher_path_for(plugin_dir)
    try:
        appid = shortcut_service.generate_app_id(launcher_path, spec.display_name)
        unsigned = appid if appid >= 0 else appid + 2**32

        data = await _read_shortcuts_from_disk(shortcut_service)
        shortcuts = data.get("shortcuts", {})

        existing = find_in_vdf(shortcuts, spec)
        if existing is not None:
            if repair_launch_options(shortcuts, spec, launcher_path):
                data["shortcuts"] = shortcuts
                await shortcut_service.write_shortcuts(data)
            logger.info("[%s] auth shortcut already in VDF (appid=%s)", spec.store, existing)
            return existing if existing >= 0 else existing + 2**32

        indices = [int(k) for k in shortcuts if str(k).isdigit()]
        shortcuts[str(max(indices, default=-1) + 1)] = _build_entry(
            spec, launcher_path, appid,
        )
        data["shortcuts"] = shortcuts
        await shortcut_service.write_shortcuts(data)
        logger.info("[%s] created auth shortcut in VDF (appid=%d)", spec.store, unsigned)
    except Exception:
        logger.exception("[%s] auth shortcut creation failed", spec.store)
        return None
    return int(unsigned)


async def build_context(
    shortcut_service: Any,
    spec: AuthShortcutSpec,
    plugin_dir: str | None,
) -> dict[str, Any]:
    """The payload the frontend needs to RunGame this store's auth shortcut.

    ``launcher_path`` is always returned, even on failure, so the frontend
    can fall back to a temporary shortcut — which is the only thing that
    works during the first session after the VDF is written.
    """
    launcher_path = launcher_path_for(plugin_dir)
    unsigned = await ensure_auth_shortcut(shortcut_service, spec, plugin_dir)
    if unsigned is None:
        return {
            "success": False,
            "error": "auth_shortcut_not_ready",
            "launcher_path": launcher_path,
        }
    return {
        "success": True,
        "appid_unsigned": unsigned,
        "launcher_path": launcher_path,
        "launch_wait_ms": spec.launch_wait_ms,
    }
