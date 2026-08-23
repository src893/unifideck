"""Last line of defence before we overwrite Steam's ``shortcuts.vdf``.

``shortcuts.vdf`` is a shared file. Ours are the entries whose ``Exe`` is
the ``unifideck-launcher``; everything else belongs to the user — games
they added by hand, EmuDeck / Steam ROM Manager ROM entries,
NonSteamLaunchers, Heroic. Losing those is unrecoverable and has bitten
this project twice already (UD-006, UD-043).

The fixes for both of those hardened individual call sites. That
approach does not compose: roughly a dozen code paths mutate the entry
dict, several key off nothing but an ``appid``, and each new store adds
more. One missed gate silently destroys a library, and the reconcile
tally reports ``removed=0`` because it never knew it dropped anything.

So this module asserts the invariant at the single point every byte
funnels through instead — a net under the per-site gates, not a
replacement for them:

    **A write may not drop a foreign entry the caller did not declare.**

Callers that legitimately remove a foreign-looking entry (the Ubisoft
auth prunes target rows with an empty ``Exe``, which the ownership test
correctly calls foreign) pass its appid in ``allow_foreign_drops``. Every
other caller declares nothing, which is the truth for every other caller.

A refusal leaves ``shortcuts.vdf`` byte-identical. That is deliberate: a
stale shortcut list costs the user one sync, a destroyed one costs them
their library.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .orphan_scan import _is_launcher_exe
from .vdf_read import VdfRead, VdfStatus, entries_of

logger = logging.getLogger(__name__)

#: Identity of one vdf entry. ``("appid", <signed int>)`` whenever the
#: entry carries an int appid — the form Steam itself keys on and the
#: form ``allow_foreign_drops`` speaks. Entries without one fall back to
#: ``("named", AppName, Exe)`` so a malformed row is still tracked
#: rather than silently exempted from the invariant.
EntryKey = tuple[str, ...]


@dataclass(frozen=True)
class Census:
    """How a set of vdf entries splits between us and the user."""

    total: int
    ours: int
    foreign: int

    def __str__(self) -> str:
        return f"total={self.total} ours={self.ours} foreign={self.foreign}"


@dataclass(frozen=True)
class Verdict:
    """Whether a write may proceed, and what would have been lost."""

    allowed: bool
    reason: str = ""
    #: Display names of the foreign entries the write would have dropped.
    dropped: list[str] = field(default_factory=list)


def _entry_key(entry: dict[str, Any]) -> EntryKey:
    """Stable identity for one vdf entry. See :data:`EntryKey`."""
    appid = entry.get("appid")
    if isinstance(appid, int):
        return ("appid", str(appid))
    name = entry.get("AppName") or entry.get("appname") or ""
    exe = entry.get("Exe") or entry.get("exe") or ""
    return ("named", str(name), str(exe))


def _entry_label(entry: dict[str, Any]) -> str:
    """Human-readable identifier for a log line."""
    name = entry.get("AppName") or entry.get("appname") or "<unnamed>"
    appid = entry.get("appid")
    return f"{name!r} (appid={appid})" if appid is not None else f"{name!r}"


def is_ours(entry: Any, launcher_path: str) -> bool:
    """True when *entry* is a Unifideck-managed shortcut.

    The single ownership question in this subsystem, asked the one way
    that a foreign tool cannot forge: the ``Exe`` target. LaunchOptions
    tokens and our ``tags`` marker both survive on shortcuts we do not
    own (a user copies a tile, a scanner clones one), which is exactly
    how UD-006 deleted people's own games.
    """
    if not isinstance(entry, dict):
        return False
    exe_raw = entry.get("Exe") or entry.get("exe") or ""
    exe = exe_raw.strip().strip('"') if isinstance(exe_raw, str) else ""
    return _is_launcher_exe(exe, launcher_path)


def foreign_index(
    entries: dict[str, Any], launcher_path: str,
) -> dict[EntryKey, dict[str, Any]]:
    """Map every *foreign* entry in *entries* to its identity key."""
    index: dict[EntryKey, dict[str, Any]] = {}
    for entry in entries.values():
        if not isinstance(entry, dict) or is_ours(entry, launcher_path):
            continue
        index[_entry_key(entry)] = entry
    return index


def census(entries: dict[str, Any], launcher_path: str) -> Census:
    """Split *entries* into ours and the user's, for the write log line."""
    total = ours = 0
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        total += 1
        if is_ours(entry, launcher_path):
            ours += 1
    return Census(total=total, ours=ours, foreign=total - ours)


def check_write(
    memory: dict[str, Any],
    disk: VdfRead,
    launcher_path: str,
    allow_foreign_drops: frozenset[int] = frozenset(),
) -> Verdict:
    """Decide whether *memory* may be written over the file *disk* came from.

    Refuses when the on-disk state could not be read (we cannot prove
    what a write would destroy), and when the write would drop a foreign
    entry that the caller did not name in ``allow_foreign_drops``.
    """
    if disk.status is VdfStatus.UNREADABLE:
        return Verdict(
            allowed=False,
            reason=f"on-disk shortcuts.vdf is unreadable ({disk.reason})",
        )
    if disk.status is VdfStatus.MISSING:
        return Verdict(allowed=True)

    on_disk = foreign_index(entries_of(disk.data), launcher_path)
    if not on_disk:
        return Verdict(allowed=True)

    in_memory = foreign_index(entries_of(memory), launcher_path)
    declared = {("appid", str(appid)) for appid in allow_foreign_drops}
    lost = [
        entry for key, entry in on_disk.items()
        if key not in in_memory and key not in declared
    ]
    if not lost:
        return Verdict(allowed=True)

    return Verdict(
        allowed=False,
        reason=(
            f"write would drop {len(lost)} shortcut(s) belonging to the "
            f"user that no caller declared"
        ),
        dropped=[_entry_label(entry) for entry in lost],
    )


def log_refusal(verdict: Verdict, disk: VdfRead, launcher_path: str) -> None:
    """Log a refused write loudly enough to be found in a support bundle."""
    logger.error(
        "[ShortcutGuard] REFUSED to write shortcuts.vdf — %s. "
        "The file on disk is untouched (%s).",
        verdict.reason, census(entries_of(disk.data), launcher_path),
    )
    for label in verdict.dropped:
        logger.error("[ShortcutGuard]   would have dropped: %s", label)
