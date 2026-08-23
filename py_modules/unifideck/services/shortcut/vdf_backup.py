"""Rotating pre-write backups of Steam's ``shortcuts.vdf``.

When a user reports "my non-Steam games disappeared", the only copy of
the file is the one that already lost them — there is nothing to compare
against and nothing to restore from. These snapshots exist so the next
such report is answerable and, more importantly, recoverable.

They live under the plugin data dir rather than beside ``shortcuts.vdf``
in Steam's ``userdata/<id>/config/``: a backup is our state, and Steam's
directory should hold Steam's files. The support bundle collects the
newest generation.

Modelled on ``services/cloud_save/safety.py``'s save-backup rotation.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

#: Generations kept. Three spans a normal sync (reconcile writes, the
#: artwork phase writes icons) plus one older state, so a bad write does
#: not immediately push the last good copy out of the window.
KEEP_BACKUPS = 3

_BACKUP_DIRNAME = "shortcuts_backups"
_BACKUP_STEM = "shortcuts.vdf.bak"


def backup_dir(data_dir: str) -> Path:
    """Directory holding the rotated snapshots."""
    return Path(data_dir).expanduser() / _BACKUP_DIRNAME


def newest_backup(data_dir: str) -> Path:
    """Path of generation 1 — the most recent snapshot (may not exist)."""
    return backup_dir(data_dir) / f"{_BACKUP_STEM}.1"


def _generation(directory: Path, index: int) -> Path:
    return directory / f"{_BACKUP_STEM}.{index}"


def rotate_and_snapshot(shortcuts_path: str, data_dir: str) -> bool:
    """Copy the *current* ``shortcuts.vdf`` in as generation 1.

    Shifts existing generations down (1→2, 2→3) and drops whatever falls
    past :data:`KEEP_BACKUPS`. Returns True when a snapshot was taken.

    Best-effort by design: this runs on the write path, and failing to
    take a backup must never stop the write that follows. A missing
    source file is not a failure — there is simply nothing to snapshot
    yet on a first run.
    """
    source = Path(shortcuts_path)
    if not source.is_file():
        return False

    directory = backup_dir(data_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        oldest = _generation(directory, KEEP_BACKUPS)
        if oldest.exists():
            oldest.unlink()
        for index in range(KEEP_BACKUPS - 1, 0, -1):
            current = _generation(directory, index)
            if current.exists():
                current.replace(_generation(directory, index + 1))
        shutil.copy2(source, _generation(directory, 1))
    except OSError as e:
        logger.warning(
            "[ShortcutBackup] could not snapshot shortcuts.vdf: %s "
            "(continuing — the write itself is still guarded)", e,
        )
        return False
    return True


def restore_newest(shortcuts_path: str, data_dir: str) -> bool:
    """Copy generation 1 back over ``shortcuts.vdf``. True if restored."""
    newest = newest_backup(data_dir)
    if not newest.is_file():
        logger.error(
            "[ShortcutBackup] no backup to restore from at %s", newest,
        )
        return False
    try:
        shutil.copy2(newest, shortcuts_path)
    except OSError:
        logger.exception("[ShortcutBackup] restore failed")
        return False
    logger.warning("[ShortcutBackup] restored shortcuts.vdf from %s", newest)
    return True
