"""The shortcut registry survives corruption and does not grow forever.

This file maps ``store:game_id`` to the AppID we first assigned, and
reconcile treats a row here as licence to *reclaim* whichever shortcut
currently carries that AppID. That makes two of its former properties
dangerous rather than merely untidy:

* a corrupt read degraded to ``{}``, and the next save wrote that empty
  dict over the still-recoverable file — losing every mapping, so every
  shortcut became unreclaimable and reconcile re-added each game under a
  fresh id, stranding the user's artwork and playtime on the old ones;
* nothing ever pruned it, so a removed game's AppID stayed on file
  forever as a live reclaim seed.
"""
from __future__ import annotations

import json
from pathlib import Path

from unifideck.services.shortcut.registry import (
    get_registered_appid,
    load_registry,
    register,
    save_registry,
    unregister,
)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "shortcuts_registry.json"


def test_round_trip(tmp_path: Path) -> None:
    p = _path(tmp_path)
    registry: dict = {}
    register(registry, "epic:abc", -123, "A Game")

    assert save_registry(registry, p) is True
    assert get_registered_appid(load_registry(p), "epic:abc") == -123


def test_corrupt_file_falls_back_to_the_backup(tmp_path: Path) -> None:
    p = _path(tmp_path)
    registry: dict = {}
    register(registry, "epic:abc", -123, "A Game")
    save_registry(registry, p)
    # Second save makes the first generation the .bak.
    register(registry, "gog:xyz", -456, "Another")
    save_registry(registry, p)

    p.write_text("{ this is not json", encoding="utf-8")
    recovered = load_registry(p)

    assert get_registered_appid(recovered, "epic:abc") == -123


def test_corrupt_file_without_a_backup_is_empty_not_fatal(tmp_path: Path) -> None:
    p = _path(tmp_path)
    p.write_text("]]not json[[", encoding="utf-8")

    assert load_registry(p) == {}


def test_save_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    p = _path(tmp_path)
    save_registry({"epic:abc": {"appid": -1}}, p)

    assert list(tmp_path.glob("*.tmp")) == []


def test_created_is_preserved_across_re_registration(tmp_path: Path) -> None:
    """``created`` must date the shortcut, not the last sync.

    Reconcile re-registers on nearly every sync, so restamping made every
    row in the file carry one identical timestamp — destroying the only
    signal that could tell when a shortcut was first made.
    """
    registry: dict = {}
    first = register(registry, "epic:abc", -123, "A Game")
    original_created = first["created"]

    second = register(registry, "epic:abc", -123, "A Game Renamed")

    assert second["created"] == original_created
    assert second["last_seen"] >= original_created


def test_unregister_prunes_every_row_for_an_appid(tmp_path: Path) -> None:
    registry: dict = {}
    register(registry, "epic:abc", -123, "A Game")
    register(registry, "gog:xyz", -456, "Another")

    removed = unregister(registry, -123)

    assert removed == ["epic:abc"]
    assert "epic:abc" not in registry
    assert "gog:xyz" in registry


def test_unregister_of_an_unknown_appid_is_a_noop() -> None:
    registry: dict = {"epic:abc": {"appid": -123}}

    assert unregister(registry, -999) == []
    assert registry == {"epic:abc": {"appid": -123}}


def test_saved_file_is_valid_json(tmp_path: Path) -> None:
    """tmp+replace must never leave a half-written file readable."""
    p = _path(tmp_path)
    registry: dict = {}
    for i in range(50):
        register(registry, f"epic:{i}", -1000 - i, f"Game {i}")
    save_registry(registry, p)

    assert len(json.loads(p.read_text(encoding="utf-8"))) == 50
