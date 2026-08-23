"""Two concurrent writers of shortcuts.vdf must not destroy each other.

Measured during a logout on 2026-08-22: 260 failures in a twelve second
window, and the file left holding nothing at all::

    [ShortcutPersistence] post-write validation FAILED: wrote 398 entries
                          but the file holds 0
    FileNotFoundError: '.../shortcuts.vdf.tmp' -> '.../shortcuts.vdf'
    [ShortcutBackup] could not snapshot shortcuts.vdf: No such file or directory

Two independent defects, both of which these pin:

* every writer used the same fixed ``<path>.tmp``, so one write consumed the
  other's temp file mid-rename;
* there was no lock anywhere in the package, and both call sites do a full
  read-modify-write, so the second silently discarded the first's entries.

The snapshot that ``_rollback_if_lossy`` relies on failed too, because the
target was briefly absent between the two renames. The guard against a lossy
write was disabled by the same race it was meant to catch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import vdf

from unifideck.services.shortcut import persistence


def _entries(n: int, tag: str) -> dict[str, Any]:
    return {
        "shortcuts": {
            str(i): {"appid": i, "AppName": f"{tag}-{i}", "Exe": "/launcher"}
            for i in range(n)
        },
    }


def _count(path: Path) -> int:
    with path.open("rb") as fh:
        return len(vdf.binary_load(fh).get("shortcuts", {}))


def test_concurrent_writes_never_lose_the_file(tmp_path: Path) -> None:
    """The headline failure: the file ended up with zero entries."""
    target = tmp_path / "shortcuts.vdf"

    async def _hammer() -> None:
        await asyncio.gather(*(
            persistence.write_vdf(str(target), _entries(20, f"w{i}"), "")
            for i in range(12)
        ))

    asyncio.run(_hammer())

    assert target.exists(), "the file was destroyed by concurrent writers"
    assert _count(target) == 20


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    """A consumed temp file used to surface as FileNotFoundError on rename."""
    target = tmp_path / "shortcuts.vdf"

    async def _hammer() -> None:
        await asyncio.gather(*(
            persistence.write_vdf(str(target), _entries(5, f"w{i}"), "")
            for i in range(8)
        ))

    asyncio.run(_hammer())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["shortcuts.vdf"]


def test_each_writer_gets_its_own_temp_path(tmp_path: Path) -> None:
    """Uniqueness is the backstop for writers that cannot share the lock.

    A second plugin process holds no lock of ours, so the worst case there
    must be a lost write rather than a destroyed file.
    """
    target = str(tmp_path / "shortcuts.vdf")

    paths = {str(persistence._unique_tmp(target)) for _ in range(50)}

    assert len(paths) == 50
    assert all(p.endswith(".tmp") for p in paths)
    assert f"{target}.tmp" not in paths


def test_games_map_writes_are_also_unique(tmp_path: Path) -> None:
    """games.map had the identical fixed-temp bug and the same call pattern."""
    target = tmp_path / "games.map"

    async def _hammer() -> None:
        await asyncio.gather(*(
            persistence.write_games_map(str(target), {}) for _ in range(8)
        ))

    asyncio.run(_hammer())

    assert target.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["games.map"]


def test_the_lock_serialises_read_modify_write(tmp_path: Path) -> None:
    """Atomic writes alone do not help: the read must be inside the lock.

    Two callers that each read, edit and write concurrently both start from
    the same snapshot, and whichever writes second discards the other's work.
    Only holding the lock across the whole cycle prevents that.
    """
    target = tmp_path / "shortcuts.vdf"
    asyncio.run(persistence.write_vdf(str(target), _entries(0, "seed"), ""))

    async def _add_one(index: int) -> None:
        async with persistence.vdf_write_lock():
            data = await persistence.read_vdf(str(target))
            data.setdefault("shortcuts", {})[str(index)] = {
                "appid": index, "AppName": f"g{index}", "Exe": "/launcher",
            }
            await asyncio.sleep(0)  # force interleaving if the lock is absent
            await persistence.write_vdf(str(target), data, "")

    async def _all() -> None:
        await asyncio.gather(*(_add_one(i) for i in range(10)))

    asyncio.run(_all())

    assert _count(target) == 10, "a concurrent writer's entries were discarded"


def test_the_real_save_path_holds_the_lock(tmp_path: Path) -> None:
    """The production writer, not just the primitive.

    Verifying the lock works is not the same as verifying anything acquires
    it, and the bug was precisely that nothing did. This drives
    ``ShortcutService._save_all`` and asserts the lock was held while it ran.
    """
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.services.shortcut.service import ShortcutService

    held: list[bool] = []
    real = persistence.vdf_write_lock()

    class _Observed:
        async def __aenter__(self) -> None:
            held.append(real.locked())
            await real.acquire()

        async def __aexit__(self, *_exc: object) -> None:
            real.release()

    svc = ShortcutService(
        EventBus(),
        str(tmp_path / "shortcuts.vdf"),
        str(tmp_path / "games.map"),
        launcher_path="/launcher",
    )
    svc._shortcuts = _entries(3, "ours")
    svc._shortcuts_loaded = True

    async def _drive() -> None:
        import unifideck.services.shortcut.service as service_mod

        service_mod.vdf_write_lock = lambda: _Observed()  # type: ignore[assignment]
        try:
            await svc._save_all()
        finally:
            service_mod.vdf_write_lock = persistence.vdf_write_lock  # type: ignore[assignment]

    asyncio.run(_drive())

    assert held == [False], "_save_all did not take the shortcuts write lock"
