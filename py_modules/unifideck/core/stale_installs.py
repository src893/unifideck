"""core/stale_installs.py — Clear a game's stale install state before install.

WHY THIS EXISTS
---------------
The store CLIs keep their own "what is installed" record, and that record can
outlive the files it points at — a manual delete, a moved SD card, a failed
"Delete all data", a partial uninstall. When it does, the CLI believes the
game is already installed and an install request becomes a **no-op**.

Field report (Amazon, "The Gap"): the install returned in 1.4 s having
downloaded nothing::

    executing: nile install amzn1.adg.product.5d4cab76… --base-path ~/Games
    cannot locate install directory … nile reported success but no
    matching directory found on disk
    failed install for amazon:amzn1.adg.product.5d4cab76…: install_dir_not_found

``~/.config/nile/installed.json`` listed FOUR installed games and not one of
their directories existed. nile exited 0, and the install could never succeed
no matter how many times the user retried.

nile keeps TWO records, and the obvious one is the wrong one
-------------------------------------------------------------
Dropping the ``installed.json`` row — the first version of this module — did
NOT fix it. The retry still no-op'd in 2.9 s and the pruned row came *back*::

    [19:17:08] cleared stale state for amazon:…5d4cab76 — nile installed.json entry
    [19:17:11] cannot locate install directory …

nile's real "do I have this already?" state is a cached protobuf manifest at
``~/.config/nile/manifests/<game_id>.raw``. Reading nile v1.1.2's
``downloading/manager.py``:

* ``load_installed_manifest()`` reads that file **directly — it is not gated
  on installed.json**, so pruning the row changes nothing;
* ``download()`` diffs the freshly-fetched manifest against it and, on an
  empty diff, logs "Game is up to date" and returns **before creating any
  directory**. The comparison is manifest-vs-manifest; nile never stats
  the disk;
* ``finish()`` then rewrites *both* the manifest and the installed.json row
  (with the manifest's total size, not bytes downloaded) — which is why the
  pruned row reappeared.

So the manifest is what actually vetoes the download, and it must be dropped
whenever the row is not backed by files — including when there is **no row at
all**, since nile recreates the row from the manifest on every run.

Dropping it is cache invalidation, never data loss: nile re-fetches it from
Amazon and performs a full download instead of a delta.

``amazon_library.py`` already guards the *display* side of this ("nile's
installed.json can outlive the directory"), so a stale entry does not show a
false PLAY button — but nothing reconciled the records before an install.

SCOPE — deliberately global
---------------------------
Called from the one seam every store install passes through
(``services/download/worker._dispatch_install``) rather than from the Amazon
adapter, because the failure mode is not Amazon's: any store whose CLI keeps
an install record can strand a game the same way. legendary keeps one too.
GOG and Ubisoft have no equivalent CLI record, so for them this is a no-op —
the call site stays unconditional anyway, so a future store that grows one is
covered by adding a pruner here and nothing else.

SAFETY
------
Every rule here exists to make this incapable of destroying a real install:

* A record is pruned ONLY when the path it names is missing from disk. A
  record whose files exist is never touched, so a healthy install is
  untouchable even if this is called by mistake.
* Never run for an UPDATE — an update legitimately expects existing files.
  The caller enforces that.
* **This module never removes directories.** It rewrites CLI record files and
  deletes a re-downloadable manifest cache, nothing else. Directory removal
  belongs to the uninstall and "Delete all data" paths (``marker_sweep``),
  where the user's intent is unambiguous. An earlier revision swept a marked
  directory here before every install; because the sweep was not gated on the
  record being stale, it could ``rmtree`` a perfectly healthy game and then
  leave nile's cached manifest claiming the files were still there — losing
  the install AND wedging it in exactly the loop above.
* Best-effort throughout: cleanup failure logs and returns, it never blocks
  the install. Being unable to tidy up is not a reason to refuse to try.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# store -> (record file, path field). The record's SHAPE differs per CLI and
# is handled in the readers below: nile writes a LIST of entries carrying an
# ``id``; legendary writes a DICT keyed by the game id.
_NILE_RECORD = "~/.config/nile/installed.json"
# nile's second record — the one that actually gates the download. See the
# module docstring.
_NILE_MANIFEST_DIR = "~/.config/nile/manifests"
_LEGENDARY_RECORD = "~/.config/legendary/installed.json"


def _load(path: str) -> Any | None:
    """Parse a CLI record file, or ``None`` if absent/unreadable/corrupt."""
    resolved = Path(path).expanduser()
    try:
        if not resolved.is_file():
            return None
        with resolved.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("[stale_installs] cannot read %s: %s", resolved, e)
        return None


def _write_atomic(path: str, data: Any) -> bool:
    """Replace a CLI record file atomically. True on success.

    Temp file in the same directory + ``os.replace`` so a crash mid-write
    cannot leave the CLI with a truncated state file — losing a store's
    entire install record would be a far worse bug than the one being fixed.
    """
    resolved = Path(path).expanduser()
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(resolved.parent), prefix=resolved.name, suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, resolved)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        logger.warning("[stale_installs] cannot rewrite %s: %s", resolved, e)
        return False
    return True


def _path_is_missing(recorded: str | None) -> bool:
    """Whether a recorded install path is absent from disk.

    An empty/absent path counts as missing: the record claims an install
    with nowhere to point, which is exactly as unusable as a dangling one.
    """
    if not recorded:
        return True
    return not Path(recorded).expanduser().is_dir()


def _reconcile_nile_row(game_id: str) -> tuple[bool, str | None]:
    """Drop a dangling ``installed.json`` row. Returns ``(live, dropped)``.

    ``live`` is True only when a row for *game_id* exists AND the directory
    it names is on disk. A **missing** row is deliberately not live: nile's
    cached manifest outlives the row (``finish()`` rewrites the row from the
    manifest on every run), so "no row" must still invalidate the manifest.
    """
    data = _load(_NILE_RECORD)
    if not isinstance(data, list):
        return False, None
    keep: list[Any] = []
    dangling: str | None = None
    for entry in data:
        if not isinstance(entry, dict) or entry.get("id") != game_id:
            keep.append(entry)
            continue
        recorded = entry.get("path")
        if not _path_is_missing(recorded):
            # Files are there — this is a real install, leave it alone.
            return True, None
        dangling = str(recorded or "<no path>")
    if dangling is None or not _write_atomic(_NILE_RECORD, keep):
        return False, None
    return False, dangling


def _drop_nile_manifest(game_id: str) -> bool:
    """Delete nile's cached manifest for *game_id*. True when one was there.

    This is the record that actually vetoes the download — see the module
    docstring. Removing it costs a re-fetch and a full (rather than delta)
    download; it can never cost the user data.
    """
    target = Path(_NILE_MANIFEST_DIR).expanduser() / f"{game_id}.raw"
    try:
        if not target.is_file():
            return False
        target.unlink()
    except OSError as e:
        logger.warning("[stale_installs] cannot remove %s: %s", target, e)
        return False
    return True


def _prune_nile(game_id: str) -> list[str]:
    """Reconcile both of nile's install records for one game."""
    notes: list[str] = []
    live, dangling = _reconcile_nile_row(game_id)
    if dangling is not None:
        notes.append(f"nile installed.json entry (path was {dangling})")
    if not live and _drop_nile_manifest(game_id):
        notes.append(f"nile manifest cache (manifests/{game_id}.raw)")
    return notes


def _prune_legendary(game_id: str) -> list[str]:
    """Drop a stale ``legendary`` entry (dict shape, ``install_path``).

    legendary's metadata cache is deliberately left alone. Unlike nile it
    verifies the installed files on disk rather than trusting a cached
    manifest, so the record row is the only thing that can veto an install.
    """
    data = _load(_LEGENDARY_RECORD)
    if not isinstance(data, dict) or game_id not in data:
        return []
    entry = data[game_id]
    recorded = entry.get("install_path") if isinstance(entry, dict) else None
    if not _path_is_missing(recorded):
        return []
    del data[game_id]
    if not _write_atomic(_LEGENDARY_RECORD, data):
        return []
    return [
        f"legendary installed.json entry (path was {recorded or '<no path>'})",
    ]


# Only these stores keep a CLI-side install record that can veto an install.
_PRUNERS: dict[str, Callable[[str], list[str]]] = {
    "amazon": _prune_nile,
    "epic": _prune_legendary,
}


def _run_pruner(store: str, game_id: str) -> list[str]:
    """Dispatch one ``(store, game_id)`` through :data:`_PRUNERS`.

    Shared by both entry points below. Never raises: a CLI record we cannot
    tidy is a cosmetic problem, and both callers sit in flows (an install,
    a full wipe) that must not fail because of it.
    """
    pruner = _PRUNERS.get(store)
    if pruner is None:
        return []
    try:
        cleaned = pruner(game_id)
    except Exception:
        logger.exception(
            "[stale_installs] pruning %s record for %s failed (non-fatal)",
            store, game_id,
        )
        return []
    if cleaned:
        logger.info(
            "[stale_installs] cleared stale state for %s:%s — %s",
            store, game_id, "; ".join(cleaned),
        )
    return cleaned


def reconcile_for_install(store: str, game_id: str) -> list[str]:
    """Clear stale local state for ``(store, game_id)`` before installing.

    Returns a human-readable list of what was cleaned, empty when there was
    nothing to do (the overwhelmingly common case — this is a couple of
    ``stat`` calls on a healthy system).

    NEVER call this for an update. See the module docstring.
    """
    return _run_pruner(store, game_id)


def _nile_dangling_ids() -> list[str]:
    """Game ids in nile's record whose install dir is gone (list shape)."""
    data = _load(_NILE_RECORD)
    if not isinstance(data, list):
        return []
    return [
        str(entry["id"])
        for entry in data
        if isinstance(entry, dict)
        and entry.get("id")
        and _path_is_missing(entry.get("path"))
    ]


def _legendary_dangling_ids() -> list[str]:
    """Game ids in legendary's record whose install dir is gone (dict shape)."""
    data = _load(_LEGENDARY_RECORD)
    if not isinstance(data, dict):
        return []
    return [
        str(game_id)
        for game_id, entry in data.items()
        if isinstance(entry, dict)
        and _path_is_missing(entry.get("install_path"))
    ]


# Parallel to :data:`_PRUNERS` — adding a store means adding a row to both.
_DANGLING_ID_READERS: dict[str, Callable[[], list[str]]] = {
    "amazon": _nile_dangling_ids,
    "epic": _legendary_dangling_ids,
}


def prune_dangling_records() -> list[str]:
    """Drop every CLI install record whose directory is no longer on disk.

    The bulk counterpart to :func:`reconcile_for_install`, for "Delete all
    Unifideck data": the wipe deletes the game directories but deliberately
    leaves ``installed.json`` alone, because
    :func:`unifideck.core.marker_sweep.collect_install_roots` reads those
    files to find the library roots it sweeps. Once the sweep has run,
    every row it acted on is dangling — and a surviving dangling row makes
    a re-login show phantom installed games.

    Safe to call in either cleanup mode: a row whose directory still exists
    is never touched (:func:`_path_is_missing`), so non-destructive cleanup —
    which keeps the games — finds nothing to prune. Must run *after*
    ``marker_sweep.sweep_all``, or the roots it needs are already gone.

    Each dangling row is removed through the tested single-row pruners, so
    N rows means N small atomic rewrites. At real-world counts (14 on the
    device this was found on) that is cheaper than a new bulk rewriter.
    """
    notes: list[str] = []
    for store, reader in _DANGLING_ID_READERS.items():
        try:
            dangling = reader()
        except Exception:
            logger.exception(
                "[stale_installs] reading %s install record failed", store,
            )
            continue
        for game_id in dangling:
            notes.extend(_run_pruner(store, game_id))
    if notes:
        logger.info(
            "[stale_installs] pruned %d dangling CLI record(s)", len(notes),
        )
    return notes
