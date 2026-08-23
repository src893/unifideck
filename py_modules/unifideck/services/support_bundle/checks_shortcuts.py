"""support_bundle/checks_shortcuts.py — shortcuts.vdf integrity checks.

Split out of ``checks.py`` (which sits near its 550-line cap) to hold the
checks that answer one recurring, historically unanswerable question:
*did something remove the user's own non-Steam shortcuts?*

Answering it needs two things a bundle did not previously carry — how the
file splits between our entries and theirs, and whether a pre-write
snapshot exists to compare against or restore from.
"""
from __future__ import annotations

from typing import Any

from .check_kit import View as _View
from .check_kit import fail as _fail
from .check_kit import na as _na
from .check_kit import ok as _ok
from .check_kit import warn as _warn
from .spec import CheckResult


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def check_shortcut_ownership_census(view: _View) -> CheckResult:
    """Report how shortcuts.vdf splits between Unifideck and the user.

    A bare total cannot distinguish "the user has no non-Steam games" from
    "ours are all that is left". The split can, and it is the first number
    to look at on any "my games disappeared" report.
    """
    name = "shortcut_ownership_census"
    block = view.block("shortcuts_census")
    if not block.get("resolved"):
        return _na(name, "shortcuts.vdf not resolved or not readable")

    total = _int_or_none(block.get("total"))
    ours = _int_or_none(block.get("ours"))
    foreign = _int_or_none(block.get("foreign"))
    if total is None or ours is None or foreign is None:
        return _na(name, f"incomplete census: {block}")

    detail = f"{total} entries: {ours} Unifideck, {foreign} the user's own"
    if total and ours == total:
        return _warn(
            name,
            f"{detail} - every entry is ours. Normal if the user only ever "
            "had Unifideck shortcuts; the signature of a wipe if not, so "
            "compare against the backups under shortcuts_backups/",
        )
    return _ok(name, detail)


def check_shortcut_backups(view: _View) -> CheckResult:
    """Confirm a pre-write snapshot of shortcuts.vdf exists.

    Without one there is nothing to diff a suspected loss against and
    nothing to restore, which is exactly the position every prior report
    of this kind left us in.
    """
    name = "shortcut_backups_present"
    block = view.block("shortcuts_census")
    if not block.get("resolved"):
        return _na(name, "data dir not resolved")

    count = _int_or_none(block.get("backup_count"))
    if count is None:
        return _na(name, "backup directory not scanned")
    if count == 0:
        return _fail(
            name,
            "no shortcuts.vdf backups - the plugin has not completed a "
            "guarded write yet, so a loss would be unrecoverable",
        )
    newest = block.get("backup_newest") or "unknown"
    return _ok(name, f"{count} backup(s), newest {newest}")
