"""steam — Steam install and Store integrations.

Public re-exports of the helpers most callers need:

* :func:`find_steam_path` / :func:`search_store` from
  :mod:`unifideck.steam.library`.
* :class:`SteamGridDBClient` and its convenience helpers from
  :mod:`unifideck.steam.steamgriddb`.

Anything else (``steam.owned_games``) stays behind the submodule
import so the public surface here remains small and stable.

``steam.shortcuts`` used to live here too — a shortcuts.vdf writer
with backup, count validation and rollback, and zero callers. Its
guarantees were folded into the one writer that actually runs
(``services/shortcut/persistence.write_vdf``) and the module removed:
a dead module that looks like the safe path is worse than no module,
because the next reader assumes those protections are in force.

The previous version of this file had a stale ``try: import
steam_utils`` branch left over from a refactor — the module
``steam_utils`` doesn't exist anywhere in the repo and nothing
imports the names it claimed to expose, so the branch was dead
code. It has been removed.
"""

from __future__ import annotations

from typing import Any

from .library import find_steam_path, search_store
from .steamgriddb import SteamGridDBClient, fetch_all_kinds, search_artwork

__all__ = [
    "SteamGridDBClient",
    "fetch_all_kinds",
    "find_steam_path",
    "search_artwork",
    "search_store",
]
