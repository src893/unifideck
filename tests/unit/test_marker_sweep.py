"""Tests for the marker-based install-dir sweep.

Live validation showed game folders surviving uninstall: GOG games installed
outside the store's default ``download_dir`` silently no-op on uninstall
(``get_installed_game_info`` can't find them) and Amazon leaves a stub dir
holding only our manifest. Both carry a Unifideck ownership marker, so the
sweep finds and deletes them — while never touching a folder we didn't mark.

Covers root derivation from install records, per-game lookup/deletion
(the per-game uninstall fallback), and the destructive ``sweep_all`` that
removes orphans + out-of-root + custom-location installs but preserves
non-Unifideck games.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.core import marker_sweep


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _gog_marker(d: Path, game_id: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / ".unifideck-id").write_text(
        json.dumps({"game_id": game_id, "gameId": game_id}),
    )


def _manifest(d: Path, store: str, store_id: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / ".unifideck_manifest.json").write_text(
        json.dumps({"store": store, "store_id": store_id, "title": "x"}),
    )


def _ubisoft_marker(d: Path, space_id: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / ".unifideck_ubisoft").write_text(
        json.dumps({
            "space_id": space_id,
            "install_path": str(d),
            "game_title": "x",
            "executable": "x.exe",
        }),
    )


def _record_roots(home: Path, *, legendary=None, nile=None, games_map=None):
    (home / ".config/legendary").mkdir(parents=True, exist_ok=True)
    (home / ".config/legendary/installed.json").write_text(
        json.dumps(legendary or {}),
    )
    (home / ".config/nile").mkdir(parents=True, exist_ok=True)
    (home / ".config/nile/installed.json").write_text(json.dumps(nile or []))
    data = home / ".local/share/unifideck"
    data.mkdir(parents=True, exist_ok=True)
    (data / "games.map").write_text(games_map or "# header\n")


def test_collect_roots_from_records_filters_shallow_and_prefixes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _home(monkeypatch, tmp_path)
    sd = home / "microSD/Games"
    (sd / "GameA").mkdir(parents=True)
    # A prefix-internal path must be excluded from roots.
    pfx = home / ".local/share/unifideck/prefixes/ubisoft/109/drive_c/game"
    pfx.mkdir(parents=True)
    _record_roots(
        home,
        nile=[{"id": "x", "path": str(sd / "GameA")}],
        games_map=f"ubisoft:109=/e\t{pfx}\t-1\n",
    )

    roots = marker_sweep.collect_install_roots()

    assert sd in roots
    assert not any("/prefixes/" in str(r) for r in roots)


def test_find_for_game_matches_store_and_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _home(monkeypatch, tmp_path)
    root = home / "Games"
    _gog_marker(root / "Brigador", "1356485086")
    _manifest(root / "Afterimage", "amazon", "amzn1.x")
    _record_roots(home, nile=[{"id": "amzn1.x", "path": str(root / "Afterimage")}])
    roots = {root}

    assert marker_sweep.find_for_game(roots, "gog", "1356485086") == (
        root / "Brigador"
    )
    assert marker_sweep.find_for_game(roots, "amazon", "amzn1.x") == (
        root / "Afterimage"
    )
    assert marker_sweep.find_for_game(roots, "gog", "nope") is None


def test_sweep_game_deletes_out_of_root_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The Brigador case: GOG game in a non-default root, full files."""
    home = _home(monkeypatch, tmp_path)
    sd = home / "microSD/Games"
    game = sd / "Brigador"
    _gog_marker(game, "1356485086")
    (game / "game").mkdir()
    (game / "game" / "brigador").write_text("bin")
    # Root is discoverable via games.map even though GOG's own scan can't.
    _record_roots(home, games_map=f"gog:1356485086=/e\t{game}\t-1\n")

    assert marker_sweep.sweep_game("gog", "1356485086") is True
    assert not game.exists()


def test_sweep_game_noop_when_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _home(monkeypatch, tmp_path)
    _record_roots(home)
    # Store already deleted the dir → nothing marked → success, no error.
    assert marker_sweep.sweep_game("gog", "whatever") is True


def test_sweep_all_removes_marked_keeps_unmarked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _home(monkeypatch, tmp_path)
    sd = home / "microSD/Games"
    _gog_marker(sd / "Brigador", "1356485086")
    _manifest(sd / "Afterimage", "amazon", "amzn1.x")          # stub
    _manifest(home / "Games/Overcooked2", "epic", "Potoo")
    # Non-Unifideck game: no marker — must survive.
    keep = sd / "Ghost of Tsushima"
    keep.mkdir(parents=True)
    (keep / "data.doi").write_text("x")
    _record_roots(
        home,
        legendary={"Potoo": {"install_path": str(home / "Games/Overcooked2")}},
        nile=[{"id": "amzn1.x", "path": str(sd / "Afterimage")}],
        games_map=f"gog:1356485086=/e\t{sd / 'Brigador'}\t-1\n",
    )

    roots = marker_sweep.collect_install_roots()
    removed = marker_sweep.sweep_all(roots)

    assert removed == 3
    assert not (sd / "Brigador").exists()
    assert not (sd / "Afterimage").exists()
    assert not (home / "Games/Overcooked2").exists()
    assert keep.exists()  # untouched — we never marked it


# --------------------------------------------------------------------------
# Ubisoft install marker
# --------------------------------------------------------------------------
def test_ubisoft_marker_is_parsed_and_swept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A standalone Ubisoft install dir is Unifideck-owned and must go.

    Ubisoft games usually live inside their prefix (deleted separately), but
    an install at a standalone path was invisible to the sweep: it knew only
    the GOG and Epic/Amazon marker names.
    """
    home = _home(monkeypatch, tmp_path)
    sd = home / "microSD/Games"
    _ubisoft_marker(sd / "Rayman Origins", "5c4c2f5f4e0165088c2ffff5")
    _gog_marker(sd / "Brigador", "1356485086")
    _record_roots(home, games_map=f"gog:1356485086=/e\t{sd / 'Brigador'}\t-1\n")

    roots = marker_sweep.collect_install_roots()
    assert marker_sweep.find_for_game(
        roots, "ubisoft", "5c4c2f5f4e0165088c2ffff5",
    ) == (sd / "Rayman Origins")

    assert marker_sweep.sweep_all(roots) == 2
    assert not (sd / "Rayman Origins").exists()
    assert not (sd / "Brigador").exists()


def test_ubisoft_marker_without_space_id_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An unparseable marker must not authorise a delete."""
    home = _home(monkeypatch, tmp_path)
    sd = home / "microSD/Games"
    game = sd / "Half Written Install"
    game.mkdir(parents=True)
    (game / ".unifideck_ubisoft").write_text(json.dumps({"game_title": "x"}))
    _gog_marker(sd / "Brigador", "1356485086")
    _record_roots(home, games_map=f"gog:1356485086=/e\t{sd / 'Brigador'}\t-1\n")

    assert marker_sweep.sweep_all(marker_sweep.collect_install_roots()) == 1
    assert game.exists()
