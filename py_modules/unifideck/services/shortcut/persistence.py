"""services/shortcut/persistence.py — Atomic I/O for shortcuts.vdf + games.map.

Pure async helpers extracted from ``ShortcutService`` so the
orchestrator stays focused on the public API while I/O mechanics
(retry-on-corruption, tmpfile+os.replace) stay independently
testable.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
from pathlib import Path
from typing import Any

import vdf

from .games_map import GameMapEntry, format_games_map, parse_games_map
from .orphan_scan import _is_launcher_exe
from .vdf_read import count_entries_in_bytes

logger = logging.getLogger(__name__)

#: Distinguishes concurrent temp files within one process; the pid covers
#: separate ones. See :func:`_unique_tmp`.
_TMP_SEQ = itertools.count()

#: Serialises the whole read-modify-write of ``shortcuts.vdf``, not just the
#: write. Atomic writes alone do not help here: two callers that each read,
#: edit and write concurrently both start from the same snapshot, and the
#: second one silently discards the first one's entries. There are two
#: independent call sites (``service.write_shortcuts`` and the icon pass in
#: ``events``) and before this there was no lock anywhere in the package.
#:
#: Module-level rather than per-service so it still holds when more than one
#: service instance exists, which is what a re-bind after an account switch
#: produces.
_VDF_WRITE_LOCK = asyncio.Lock()


def vdf_write_lock() -> asyncio.Lock:
    """The lock guarding read-modify-write cycles over ``shortcuts.vdf``."""
    return _VDF_WRITE_LOCK

# Games.map read retries — 3 x 100ms worst-case. Cheap enough to
# avoid spurious GameNotFoundError when the launcher reads
# mid-write by a concurrent background sync.
_GAMES_MAP_READ_ATTEMPTS = 3
_GAMES_MAP_RETRY_DELAY_S = 0.1


async def read_vdf(shortcuts_path: str) -> dict[str, Any]:
    """Load shortcuts.vdf into a dict (empty dict if missing).

    Offloaded via ``to_thread`` since the vdf library is sync.
    """
    if not await asyncio.to_thread(lambda: Path(shortcuts_path).is_file()):
        return {"shortcuts": {}}

    def _read_sync() -> dict[str, Any]:
        try:
            with Path(shortcuts_path).open("rb") as f:
                return vdf.binary_loads(f.read())  # type: ignore[no-any-return,no-untyped-call]  # vdf.binary_loads is untyped + returns Any
        except Exception as e:
            logger.warning("[ShortcutPersistence] failed to read shortcuts.vdf: %s", e)
            return {"shortcuts": {}}

    return await asyncio.to_thread(_read_sync)


def _assert_exec_bit(shortcuts_path: str, *, was_exec: bool) -> None:
    """Re-assert ``0o755`` on ``shortcuts_path`` — NSL's init sentinel.

    ``0o755`` is required, not permissive-by-accident. **NonSteamLaunchers
    (NSL)**'s persistent ``nslgamescanner.service`` treats the executable
    bit as its "already-initialised" sentinel: on each scan, if
    ``shortcuts.vdf`` is *not* executable it overwrites the whole file
    with an empty ``{"shortcuts": {}}``, wiping every shortcut, ours
    included. NSL always chmods to ``0o755`` after writing; our
    tmp+``os.replace`` creates the destination inode at the umask default
    instead, which silently disarms the sentinel. A stricter mode
    reintroduces the library wipe.

    ``was_exec`` must be sampled from the **pre-existing** file, before
    the replace. Sampling after it reports the mode of the brand-new tmp
    inode — always non-executable, since ``open("wb")`` creates at base
    ``0o666`` — so the "restored" line fired on literally every write and
    read like a recurring rescue from an external tool.
    """
    try:
        os.chmod(shortcuts_path, 0o755)  # noqa: S103
    except OSError as e:
        logger.warning(
            "[ShortcutPersistence] could not set executable bit on "
            "shortcuts.vdf: %s (NSL, if installed, may reset the file)", e,
        )
        return
    if not was_exec:
        logger.info(
            "[ShortcutPersistence] restored executable bit on shortcuts.vdf "
            "(0o755) — it was missing before this write, which is what makes "
            "NonSteamLaunchers' scanner wipe the library on its next pass",
        )


def _validate_written(shortcuts_path: str, expected: int) -> bool:
    """True when the file on disk holds ``expected`` entries.

    Counted from the raw bytes, not by re-parsing: the point is to catch
    a write that lost content, and a check that shares the writer's view
    of the file cannot see the writer losing part of it.
    """
    try:
        raw = Path(shortcuts_path).read_bytes()
    except OSError:
        logger.exception(
            "[ShortcutPersistence] could not re-read shortcuts.vdf to "
            "validate the write",
        )
        return False
    actual = count_entries_in_bytes(raw)
    if actual == expected:
        return True
    logger.error(
        "[ShortcutPersistence] post-write validation FAILED: wrote %d "
        "entries but the file holds %d", expected, actual,
    )
    return False


async def write_vdf(
    shortcuts_path: str, data: dict[str, Any], data_dir: str = "",
) -> None:
    """Persist shortcuts.vdf atomically, keeping the file executable.

    Uses tmpfile + os.replace so Steam never reads a half-written file.

    Around that, three guarantees this writer lacked while a *dead*
    module (``steam/shortcuts.py``, zero callers) implemented them:

    * the previous contents are snapshotted first, so a bad write is
      recoverable rather than merely regrettable;
    * the written file is re-counted afterwards and rolled back if it
      lost entries;
    * the NSL executable-bit sentinel is re-asserted (see
      :func:`_assert_exec_bit`).

    ``data_dir`` is where snapshots go; passing ``""`` disables them
    (unit tests that only exercise the byte path).
    """
    def _write_sync() -> None:
        from .vdf_backup import rotate_and_snapshot

        was_exec = os.access(shortcuts_path, os.X_OK)
        backed_up = bool(data_dir) and rotate_and_snapshot(
            shortcuts_path, data_dir,
        )
        if not _atomic_write(shortcuts_path, data):
            return
        _assert_exec_bit(shortcuts_path, was_exec=was_exec)
        _rollback_if_lossy(shortcuts_path, data, data_dir, backed_up=backed_up)

    await asyncio.to_thread(_write_sync)


def _unique_tmp(target: str) -> Path:
    """A temp path no other writer can be using.

    The suffix used to be a bare ``.tmp``, shared by every writer of this
    file. Two concurrent writes then destroyed each other: A renamed the temp
    file into place, consuming it, and B's own ``replace`` failed with
    ``FileNotFoundError`` on a source that had just been taken. Measured
    during a logout: 260 failures in twelve seconds, several of them leaving
    ``wrote 398 entries but the file holds 0``.

    Uniqueness is not the real fix, :func:`vdf_write_lock` is. It is the
    backstop for writers that never share a lock, such as a second plugin
    process, where the worst case should be a lost write rather than a
    destroyed file.
    """
    return Path(f"{target}.{os.getpid()}.{next(_TMP_SEQ)}.tmp")


def _atomic_write(shortcuts_path: str, data: dict[str, Any]) -> bool:
    """tmp-file + ``os.replace`` so Steam never reads a partial file."""
    target = Path(shortcuts_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(shortcuts_path)
    try:
        with tmp.open("wb") as f:
            f.write(vdf.binary_dumps(data))  # type: ignore[no-untyped-call]
        tmp.replace(target)
    except Exception:
        logger.exception("[ShortcutPersistence] failed to write shortcuts.vdf")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def _rollback_if_lossy(
    shortcuts_path: str, data: dict[str, Any], data_dir: str, *,
    backed_up: bool,
) -> None:
    """Restore the snapshot when the written file lost entries."""
    from .vdf_backup import restore_newest

    expected = len(_shortcut_entries(data))
    if _validate_written(shortcuts_path, expected) or not backed_up:
        return
    restore_newest(shortcuts_path, data_dir)
    _assert_exec_bit(shortcuts_path, was_exec=True)


def _shortcut_entries(data: dict[str, Any]) -> dict[str, Any]:
    """Return the inner ``shortcuts`` sub-dict of a loaded vdf (or ``{}``).

    ``shortcuts.vdf`` wraps entries under a top-level ``"shortcuts"``
    key; a third party can leave the file in a shape without it.
    """
    inner = data.get("shortcuts") if isinstance(data, dict) else None
    return inner if isinstance(inner, dict) else {}


def _merge_one_foreign(
    entry: Any,
    mem_inner: dict[str, Any],
    known_appids: set[Any],
    launcher_path: str,
    skip_appids: frozenset[int] = frozenset(),
) -> bool:
    """Merge a single disk *entry* back into *mem_inner* if it is a foreign
    shortcut memory lost. Returns ``True`` if it was merged.

    Extracted from :func:`merge_foreign_shortcuts` to keep that function
    under the cognitive-complexity cap; behaviour is identical. Mutates
    *mem_inner* / *known_appids* in place on a merge.
    """
    if not isinstance(entry, dict):
        return False
    exe_raw = entry.get("Exe") or entry.get("exe") or ""
    exe = exe_raw.strip().strip('"') if isinstance(exe_raw, str) else ""
    # Ours: memory is the source of truth (respect our own deletes).
    if _is_launcher_exe(exe, launcher_path):
        return False
    appid = entry.get("appid")
    # A drop the caller declared on purpose (the Ubisoft auth prunes
    # target bare foreign-looking rows). Without this the merge would
    # re-inject the very entry the caller just removed, silently undoing
    # a deliberate deletion — the mirror image of the loss this function
    # exists to prevent.
    if isinstance(appid, int) and appid in skip_appids:
        return False
    # Foreign entry already represented in memory — leave memory's
    # copy (our writes never mutate foreign entries anyway).
    if appid is not None and appid in known_appids:
        return False
    new_key = str(len(mem_inner))
    while new_key in mem_inner:
        new_key = str(int(new_key) + 1)
    mem_inner[new_key] = entry
    if appid is not None:
        known_appids.add(appid)
    return True


def merge_foreign_shortcuts(
    mem: dict[str, Any],
    disk: dict[str, Any],
    launcher_path: str,
    skip_appids: frozenset[int] = frozenset(),
) -> int:
    """Re-inject foreign shortcuts that ``mem`` lost since it was loaded.

    Unifideck holds ``self._shortcuts`` in memory for the lifetime of
    the service and writes the whole dict back on every ``_save_all``.
    A concurrent writer — NonSteamLaunchers' scanner service, Steam's
    own shutdown flush, a manual add — can append entries to the
    on-disk file *after* our snapshot; without this merge the next
    ``_save_all`` overwrites them (a lost update). UD-006's Exe-gate
    stopped reconcile from *deleting* foreign shortcuts, but not this
    stale-snapshot *overwrite*, which is the residual UD-043 data loss.

    Ownership is decided the same way reconcile decides it: an entry
    whose ``Exe`` basename is our ``unifideck-launcher``
    (:func:`orphan_scan._is_launcher_exe`) is *ours* — memory is
    authoritative for it, so an entry we deliberately dropped stays
    dropped. Every other entry is *foreign*: if it is present on disk
    but absent from memory (matched by ``appid``), it is copied back
    into ``mem`` under a fresh non-colliding key. Foreign entries the
    user or Steam removed on disk are honoured too — we only *add*
    what disk has and memory lost, never resurrect our own deletions.

    Mutates ``mem`` in place and returns the number of entries merged
    back (0 in the common no-conflict case, so callers can skip the
    write-back log when nothing changed).
    """
    disk_inner = _shortcut_entries(disk)
    if not disk_inner:
        return 0
    # Ensure the wrapper exists so mutations land on the object the
    # caller will write back (``mem["shortcuts"]``), not a throwaway.
    if not isinstance(mem.get("shortcuts"), dict):
        mem["shortcuts"] = {}
    mem_inner = mem["shortcuts"]

    known_appids = {
        e.get("appid") for e in mem_inner.values()
        if isinstance(e, dict) and e.get("appid") is not None
    }
    merged = 0
    for entry in disk_inner.values():
        if _merge_one_foreign(
            entry, mem_inner, known_appids, launcher_path, skip_appids,
        ):
            merged += 1

    if merged:
        logger.info(
            "[ShortcutPersistence] merged %d foreign shortcut(s) that a "
            "concurrent writer added since load — preventing a lost update",
            merged,
        )
    return merged


async def read_games_map(games_map_path: str) -> dict[str, GameMapEntry]:
    """Load games.map with retry-on-corruption.

    Up to ``_GAMES_MAP_READ_ATTEMPTS`` attempts spaced
    ``_GAMES_MAP_RETRY_DELAY_S`` apart — a concurrent
    ``save_all`` can leave the file briefly partial between
    the truncate and the final flush. Transient errors
    (OSError rename race, UnicodeDecodeError mid-write)
    all retry. Returns ``{}`` on missing file or
    irrecoverable malformation.
    """
    if not await asyncio.to_thread(lambda: Path(games_map_path).is_file()):
        return {}

    for attempt in range(1, _GAMES_MAP_READ_ATTEMPTS + 1):
        try:
            def _read_sync() -> str:
                with Path(games_map_path).open(encoding="utf-8") as f:
                    return f.read()

            content = await asyncio.to_thread(_read_sync)
            return parse_games_map(content)
        except Exception as e:
            if attempt < _GAMES_MAP_READ_ATTEMPTS:
                logger.debug(
                    "[ShortcutPersistence] games.map read failed (attempt %d/%d): %s. Retrying...",
                    attempt, _GAMES_MAP_READ_ATTEMPTS, e,
                )
                await asyncio.sleep(_GAMES_MAP_RETRY_DELAY_S)
            else:
                logger.warning(
                    "[ShortcutPersistence] games.map read failed permanently after %d attempts: %s",
                    _GAMES_MAP_READ_ATTEMPTS, e,
                )

    return {}


async def write_games_map(games_map_path: str, games_map: dict[str, GameMapEntry]) -> None:
    """Persist games.map atomically.

    Uses the POSIX ``tmpfile + os.replace`` pattern: write content to
    ``<path>.tmp``, then rename. Readers mid-read see either
    old or new content, never a half-written file — eliminates
    the race where the launcher dispatcher reads between our
    truncate and the subsequent writes.
    """
    def _write_sync() -> None:
        parent = str(Path(games_map_path).parent)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)

        content = format_games_map(games_map)
        tmp_path = str(_unique_tmp(games_map_path))

        try:
            with Path(tmp_path).open("w", encoding="utf-8") as f:
                f.write(content)
                # Ensure it's fully written to disk before rename
                f.flush()
                os.fsync(f.fileno())

            Path(tmp_path).replace(games_map_path)
        except Exception:
            logger.exception("[ShortcutPersistence] failed to write games.map")
            if Path(tmp_path).exists():
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()

    await asyncio.to_thread(_write_sync)
