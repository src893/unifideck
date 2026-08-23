"""A write that would lose the user's shortcuts is refused, not made.

The failure this guards against is the one that leaves no evidence: a
``shortcuts.vdf`` we could not parse used to read as an *empty* library,
so reconcile rebuilt our entries from nothing, the merge that protects
foreign entries re-read with the same parser and found nothing to merge,
and the write replaced the user's whole non-Steam library with only our
own. The reconcile tally said ``removed=0`` throughout, because from
reconcile's point of view nothing was removed.

Two independent properties are pinned here:

* **Fail closed.** An unreadable file is untouchable — the bytes on disk
  must be identical afterwards.
* **Never shrink the foreign set.** A write that drops a shortcut we do
  not own is refused unless the caller declared that drop by appid.

A refused write costs the user a stale shortcut list until the next
sync. That is the trade being made deliberately.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import vdf

from unifideck.event_bus.event_bus import EventBus
from unifideck.services.shortcut.persistence import write_vdf
from unifideck.services.shortcut.service import ShortcutService
from unifideck.services.shortcut.vdf_backup import KEEP_BACKUPS, backup_dir
from unifideck.services.shortcut.vdf_read import VdfStatus, read_vdf_sync

_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


def _ours(appid: int, launch: str) -> dict[str, Any]:
    return {
        "appid": appid, "AppName": f"Ours {appid}",
        "Exe": f'"{_LAUNCHER}"', "LaunchOptions": launch,
    }


def _theirs(appid: int, name: str = "Their Game") -> dict[str, Any]:
    return {
        "appid": appid, "AppName": name,
        "Exe": '"/home/deck/Games/thing.exe"', "LaunchOptions": "",
    }


def _write_file(path: str, entries: dict[str, Any]) -> None:
    Path(path).write_bytes(vdf.binary_dumps({"shortcuts": entries}))


def _service(tmp_path: Path) -> ShortcutService:
    return ShortcutService(
        EventBus(),
        str(tmp_path / "shortcuts.vdf"),
        str(tmp_path / "games.map"),
        launcher_path=_LAUNCHER,
    )


# ── the tri-state read ─────────────────────────────────────────────

def test_missing_file_is_not_unreadable(tmp_path: Path) -> None:
    """No file is a legitimately empty library, not a failure."""
    result = read_vdf_sync(str(tmp_path / "nope.vdf"))
    assert result.status is VdfStatus.MISSING
    assert result.trustworthy


def test_garbage_bytes_are_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(b"\x00\x01not a vdf at all\xff")

    result = read_vdf_sync(str(path))

    assert result.status is VdfStatus.UNREADABLE
    assert not result.trustworthy


def test_trailing_bytes_make_the_whole_parse_unreadable(tmp_path: Path) -> None:
    """The real-world shape: a third-party writer appends to the file.

    Our vendored ``vdf`` uses ``raise_on_remaining=True``, so trailing
    bytes fail the entire parse rather than truncating it — which is how
    a file full of shortcuts came to look empty.
    """
    path = tmp_path / "shortcuts.vdf"
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _theirs(-2)})
    path.write_bytes(path.read_bytes() + b"\x08\x08trailing")

    result = read_vdf_sync(str(path))

    assert result.status is VdfStatus.UNREADABLE
    assert result.raw_count == 2, "the byte scan still sees both entries"


def test_a_healthy_file_parses_with_matching_counts(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.vdf"
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _theirs(-2)})

    result = read_vdf_sync(str(path))

    assert result.status is VdfStatus.PARSED
    assert result.parsed_count == result.raw_count == 2


# ── fail closed ────────────────────────────────────────────────────

def test_unreadable_file_is_left_byte_identical(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    path = Path(svc._shortcuts_path)
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _theirs(-2)})
    path.write_bytes(path.read_bytes() + b"\x08\x08corrupt")
    before = path.read_bytes()

    asyncio.run(svc._load_shortcuts())
    # Reconcile would rebuild from here; simulate that it produced only ours.
    svc._shortcuts = {"shortcuts": {"0": _ours(-1, "epic:1")}}
    svc._shortcuts_loaded = True
    asyncio.run(svc._save_all())

    assert path.read_bytes() == before


def test_unreadable_read_is_not_cached_as_an_empty_library(tmp_path: Path) -> None:
    """``_shortcuts_loaded`` must stay False so the read is retried."""
    svc = _service(tmp_path)
    path = Path(svc._shortcuts_path)
    _write_file(str(path), {"0": _theirs(-2)})
    path.write_bytes(path.read_bytes() + b"\x08\x08corrupt")

    asyncio.run(svc._load_shortcuts())

    assert svc._shortcuts_loaded is False
    assert svc._shortcuts_unreadable is True


# ── never shrink the foreign set ───────────────────────────────────

def test_undeclared_foreign_drop_is_refused(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    path = Path(svc._shortcuts_path)
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _theirs(-2)})
    asyncio.run(svc._load_shortcuts())

    # Drop the user's entry from memory, as an ungated code path would.
    del svc._shortcuts["shortcuts"]["1"]
    asyncio.run(svc._save_all())

    on_disk = read_vdf_sync(str(path))
    appids = sorted(e["appid"] for e in on_disk.data["shortcuts"].values())
    assert appids == [-2, -1], "the user's shortcut must still be there"


def test_declared_foreign_drop_proceeds(tmp_path: Path) -> None:
    """The Ubisoft auth prunes legitimately remove bare foreign rows."""
    svc = _service(tmp_path)
    path = Path(svc._shortcuts_path)
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _theirs(-2)})
    asyncio.run(svc._load_shortcuts())

    del svc._shortcuts["shortcuts"]["1"]
    asyncio.run(svc._save_all(allow_foreign_drops=frozenset({-2})))

    on_disk = read_vdf_sync(str(path))
    appids = [e["appid"] for e in on_disk.data["shortcuts"].values()]
    assert appids == [-1]


def test_dropping_our_own_entry_is_always_allowed(tmp_path: Path) -> None:
    """The guard constrains foreign entries only — our deletes still apply."""
    svc = _service(tmp_path)
    path = Path(svc._shortcuts_path)
    _write_file(str(path), {"0": _ours(-1, "epic:1"), "1": _ours(-3, "epic:2")})
    asyncio.run(svc._load_shortcuts())

    del svc._shortcuts["shortcuts"]["1"]
    asyncio.run(svc._save_all())

    on_disk = read_vdf_sync(str(path))
    assert [e["appid"] for e in on_disk.data["shortcuts"].values()] == [-1]


# ── backups ────────────────────────────────────────────────────────

def test_write_snapshots_the_previous_contents(tmp_path: Path) -> None:
    sc = tmp_path / "shortcuts.vdf"
    data_dir = tmp_path / "data"
    _write_file(str(sc), {"0": _theirs(-2, "Original")})
    before = sc.read_bytes()

    asyncio.run(write_vdf(str(sc), {"shortcuts": {"0": _ours(-1, "epic:1")}},
                          str(data_dir)))

    newest = backup_dir(str(data_dir)) / "shortcuts.vdf.bak.1"
    assert newest.read_bytes() == before


def test_backups_rotate_and_are_capped(tmp_path: Path) -> None:
    sc = tmp_path / "shortcuts.vdf"
    data_dir = tmp_path / "data"
    for i in range(KEEP_BACKUPS + 3):
        _write_file(str(sc), {"0": _theirs(-100 - i)})
        asyncio.run(write_vdf(
            str(sc), {"shortcuts": {"0": _theirs(-100 - i)}}, str(data_dir),
        ))

    kept = sorted(backup_dir(str(data_dir)).glob("shortcuts.vdf.bak.*"))
    assert len(kept) == KEEP_BACKUPS


def test_write_leaves_the_file_executable(tmp_path: Path) -> None:
    """NSL's scanner wipes a non-executable shortcuts.vdf — unchanged rule."""
    import os

    sc = tmp_path / "shortcuts.vdf"
    asyncio.run(write_vdf(str(sc), {"shortcuts": {"0": _ours(-1, "epic:1")}}))

    assert os.access(sc, os.X_OK)
