"""Salvage a vendor client's own logs before its prefix is destroyed.

py_modules/unifideck/stores/shared/prefix_forensics.py

For a wrapper store the prefix *is* the install, so an install that
produced no game gets the prefix deleted — ``prefix_placement`` states
why. That deletion also takes the only first-hand account of what went
wrong: the vendor client writes its own logs inside the prefix, and
nothing else on the machine records why *its* startup failed.

Measured cost of not doing this. A tester's Battle.net install failed
with "the client did not become ready"; by the time the bundle was
collected the prefix had been removed, so ``Battle.net Launcher.log``,
the bootstrapper's ``Setup`` logs and the Agent's logs were all gone.
The failure had to be reconstructed from timing across four other logs,
and the one file that would have named it in a line did not exist any
more.

Deliberately a **flat text file**, not a copied tree. One
``launches/<store>-<id>.vendor.txt`` per abandoned install is something
a triager opens and reads, it caps trivially, and it needs one row in the
support-bundle registry rather than a new collected directory.

Best-effort throughout: this runs on a failure path, immediately before a
cleanup, and must never be the reason the cleanup does not happen.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Globs, relative to the prefix's ``drive_c``, of the logs each vendor
# client writes about its own startup. Ordered roughly by how often they
# hold the answer.
#
# Battle.net's set was read off a real install: the bootstrapper logs under
# ``ProgramData/Battle.net/Setup``, the client next to its own executable,
# and ``Agent`` separately because it survives the client window closing
# and owns every download.
VENDOR_LOG_GLOBS: dict[str, tuple[str, ...]] = {
    # Measured against a real failing prefix on 2026-08-19: of the globs
    # first guessed here, only ``Agent/Logs/*.log`` matched anything. The
    # client's own logs are under ``ProgramData/Battle.net/Logs`` (not
    # Roaming, and not beside the executable), and the bootstrapper writes
    # ``Setup`` logs one level deeper than assumed. Keep the misses: they
    # cost one failed glob each and vendors move these between builds.
    #
    # 2026-08-22: ``Agent/Logs/*.log`` matched, but only a one-line
    # ``Switcher`` log. The Agent's real logs are a directory deeper, under
    # a build-versioned ``Agent.<build>``. A whole investigation into installs
    # stuck at "Queued" ran on files this salvage had not collected: the
    # answer was in ``Operations-*`` (the game's operation sitting behind the
    # Agent's own self-update), with the rate in ``AgentUpdate-*`` and the
    # cause (a region-tag change) in ``AgentNGDP-*``. The Agent owns every
    # download, so its logs go FIRST.
    "battlenet": (
        "ProgramData/Battle.net/Agent/Agent.*/Logs/*.log",
        "ProgramData/Battle.net/Logs/*.log",
        "ProgramData/Battle.net/Agent/Logs/*.log",
        "ProgramData/Battle.net/Agent/Agent.log",
        "ProgramData/Battle.net/Setup/*.log",
        "ProgramData/Battle.net/Setup/*/*.log",
        "ProgramData/Battle.net/*.log",
        "users/steamuser/AppData/Roaming/Battle.net/Logs/*.log",
        "users/steamuser/AppData/Local/Battle.net/Logs/*.log",
        "Program Files (x86)/Battle.net/Logs/*.log",
        "Program Files (x86)/Battle.net/Battle.net Launcher.log",
    ),
    "ubisoft": (
        "ProgramData/Ubisoft/Ubisoft Game Launcher/logs/*.log",
        "users/steamuser/AppData/Local/Ubisoft Game Launcher/logs/*.log",
    ),
}

# Per-file tail, and a ceiling on how many files one salvage may write.
# Blizzard's Agent log can reach tens of MB during a large download and
# the interesting part is always the end.
MAX_BYTES_PER_FILE = 256 * 1024
MAX_FILES = 24

#: Suffix chosen so the existing ``launches/*.log`` collector does not also
#: match these — they get their own registry row.
SUFFIX = ".vendor.txt"


def launches_dir() -> Path:
    """The per-launch log archive, resolved per call.

    Same trap as ``wrapper_session.prefix_index_path``: a module-level
    constant is computed before pytest's fixtures redirect ``HOME``, so it
    keeps pointing at the developer's real data directory for a whole run.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "launches"


def salvage_path(store: str, game_id: str, into: Path | None = None) -> Path:
    """Where this store's salvaged logs for ``game_id`` are written.

    Lands beside the per-launch logs, so a support bundle picks it up next
    to the launch it belongs to and a triager finds it without being told
    where to look.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in game_id)
    root = launches_dir() if into is None else Path(into)
    return root / f"{store}-{safe}{SUFFIX}"


def _tail(path: Path) -> str:
    """The last :data:`MAX_BYTES_PER_FILE` of ``path``, or an error note."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_BYTES_PER_FILE:
                handle.seek(size - MAX_BYTES_PER_FILE)
            return handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _collect(drive_c: Path, globs: tuple[str, ...]) -> list[tuple[Path, str]]:
    """``(path, text)`` for every log the globs match, newest first."""
    found: list[Path] = []
    for pattern in globs:
        try:
            found.extend(p for p in drive_c.glob(pattern) if p.is_file())
        except OSError:
            continue
    try:
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return [(path, _tail(path)) for path in found[:MAX_FILES]]


def _write_salvage(
    store: str, prefix: Path, destination: Path, globs: tuple[str, ...],
) -> int:
    """Do the salvage synchronously. Returns how many files were captured."""
    from unifideck.launcher.proton.infrastructure.prefix_layout import (
        resolve_drive_c,
    )

    drive_c = resolve_drive_c(prefix)
    if drive_c is None:
        return 0
    captured = _collect(drive_c, globs)
    if not captured:
        return 0
    parts = [
        (
            f"# {store} client logs salvaged from {prefix}\n"
            f"# The prefix was removed after this file was written.\n"
        ),
    ]
    for path, text in captured:
        rel = os.path.relpath(path, drive_c)
        parts.append(f"\n===== {rel} =====\n{text}\n")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(parts), encoding="utf-8", errors="replace")
    return len(captured)


async def preserve_vendor_logs(
    store: str, prefix: Path, destination: Path,
) -> int:
    """Copy ``store``'s client logs out of ``prefix``. Never raises.

    Returns the number of files captured — zero when the store has no
    known logs, the prefix is gone, or nothing matched. Callers treat any
    outcome as fine: this exists to make the *next* report diagnosable,
    never to gate the operation it precedes.
    """
    globs = VENDOR_LOG_GLOBS.get(store)
    if not globs:
        return 0
    try:
        count = await asyncio.to_thread(
            _write_salvage, store, Path(prefix), Path(destination), globs,
        )
    except Exception:
        logger.debug(
            "[%s] could not salvage client logs from %s", store, prefix,
            exc_info=True,
        )
        return 0
    if count:
        logger.info(
            "[%s] salvaged %d client log(s) from %s into %s",
            store, count, prefix, destination,
        )
    return count
