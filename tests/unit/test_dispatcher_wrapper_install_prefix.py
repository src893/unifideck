"""A wrapper-store install must reach its client with no games.map row.

Field report, Battle.net, 2026-08-11. Install placed the prefix, the frontend
``RunGame``'d the shortcut, and the launcher died instantly::

    [launcher.dispatcher] request received: battlenet:osi
    GameNotFoundError: game 'battlenet:osi' not found in games.map

The Battle.net client never appeared, so the install sat waiting for a window
that would never open and the user cancelled it.

There is no row *by definition* during an install: the row is written when the
game finishes downloading. Ubisoft had an escape hatch for exactly this and
Battle.net did not — it had been getting by on a race. The old install reported
success the instant its prefix was cloned, so the (premature) ``DOWNLOAD_COMPLETE``
wrote a games.map row within milliseconds while ``RunGame`` took ~3 s to reach
the launcher. Fixing the premature success removed the race and exposed the
missing hatch.

Every case here is parametrized over ``WRAPPER_STORES``: two hand-written
branches are how these two drifted apart in the first place, and EA App is next.
"""
from __future__ import annotations

import json

import pytest

from unifideck.launcher import dispatcher as d
from unifideck.launcher.wrapper_prefix_probe import _SPECS
from unifideck.launcher.wrapper_stores import WRAPPER_STORES

STORES = sorted(WRAPPER_STORES)


class _NoRow:
    """games.map has nothing — the state every install is launched in."""

    async def get_entry_for_game_key(self, _store: str, _game_id: str) -> None:
        return None


class _RowWithoutExe:
    """An installed title whose row carries no exe.

    Legitimate for both wrapper stores: the vendor client launches the game, so
    neither handler ever reads ``exe_path``.
    """

    def __init__(self, app_id: int = 4242) -> None:
        self.app_id = app_id
        self.exe = ""
        self.work_dir = ""

    async def get_entry_for_game_key(self, _store: str, _game_id: str):
        return self


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    for store in ("EPIC", "GOG", "AMAZON", "MICROSOFT", "UBISOFT", "BATTLENET"):
        monkeypatch.delenv(f"UNIFIDECK_{store}_ACTION", raising=False)
    monkeypatch.setattr(d, "_resolve_exe_from_install", lambda *a, **k: None)
    monkeypatch.setattr(d, "_install_path_from_cache", lambda *a, **k: "")
    monkeypatch.setattr(d, "_resolve_plugin_dir", lambda: tmp_path)


def _data_dir(tmp_path):
    path = tmp_path / ".local" / "share" / "unifideck"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _install_client(root, store: str, *, payload: bool = True) -> None:
    """Write the store's client into ``root``'s ``drive_c``.

    ``payload=False`` writes only the entry executable. For a store whose
    entry point is a *shim* (Battle.net) that is the shape an interrupted
    client install leaves behind, and it must not read as bootstrapped.
    """
    drive_c = root / "pfx" / "drive_c"
    client = drive_c / _SPECS[store].client_rel
    client.parent.mkdir(parents=True, exist_ok=True)
    client.write_bytes(b"MZ")
    glob = _SPECS[store].payload_glob
    if glob is None or not payload:
        return
    # The spec's glob wildcards the build number; pin it to a real one.
    member = drive_c / glob.replace("*", "17651")
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_bytes(b"MZ")


def _bootstrap_prefix(tmp_path, store: str, game_id: str, *, record: bool):
    """A prefix with the store's client installed, as placement leaves it."""
    root = tmp_path / "sd" / "prefixes" / store / game_id
    _install_client(root, store)
    if record:
        (_data_dir(tmp_path) / _SPECS[store].id_map).write_text(
            json.dumps({game_id: {"prefix_path": str(root)}}),
        )
    return root


# ── the reported failure ────────────────────────────────────────


@pytest.mark.parametrize("store", STORES)
@pytest.mark.asyncio
async def test_a_bootstrapped_prefix_routes_to_the_install_action(
    store: str, tmp_path, monkeypatch,
) -> None:
    """No row + a prefix holding the client = the install is still running."""
    _bootstrap_prefix(tmp_path, store, "g1", record=True)

    ctx = await d._build_context(["launcher", f"{store}:g1"], _NoRow())

    assert ctx.is_launch_action is False
    assert ctx.action == "install"
    # The handler is picked off auth_store — the wrong value opens the wrong
    # vendor client entirely.
    assert ctx.auth_store == store
    assert ctx.game_id == "g1"


@pytest.mark.parametrize("store", STORES)
@pytest.mark.asyncio
async def test_the_internal_default_location_is_found_too(
    store: str, tmp_path,
) -> None:
    """A prefix in the default dir with nothing recorded still resolves."""
    root = _data_dir(tmp_path) / "prefixes" / store / "g2"
    _install_client(root, store)

    ctx = await d._build_context(["launcher", f"{store}:g2"], _NoRow())

    assert ctx.action == "install"


@pytest.mark.asyncio
async def test_a_battlenet_shim_without_its_payload_is_not_bootstrapped(
    tmp_path,
) -> None:
    """``Battle.net.exe`` alone is a ~1 MB stub, not a client.

    An interrupted client install leaves exactly this, and treating it as a
    bootstrapped prefix is what sent every install into a 300 s wait for a
    client that could never start.
    """
    root = tmp_path / "sd" / "prefixes" / "battlenet" / "g4"
    _install_client(root, "battlenet", payload=False)
    (_data_dir(tmp_path) / _SPECS["battlenet"].id_map).write_text(
        json.dumps({"g4": {"prefix_path": str(root)}}),
    )

    with pytest.raises(d.GameNotFoundError):
        await d._build_context(["launcher", "battlenet:g4"], _NoRow())


@pytest.mark.parametrize("store", STORES)
@pytest.mark.asyncio
async def test_a_genuinely_unknown_game_still_fails(
    store: str, tmp_path,
) -> None:
    """The hatch must not become a catch-all — nothing to open a client into."""
    with pytest.raises(d.GameNotFoundError):
        await d._build_context(["launcher", f"{store}:nope"], _NoRow())


@pytest.mark.parametrize("store", STORES)
@pytest.mark.asyncio
async def test_an_empty_prefix_does_not_count(store: str, tmp_path) -> None:
    """An abandoned install leaves a directory with no client in it."""
    root = tmp_path / "sd" / "prefixes" / store / "g3"
    (root / "pfx" / "drive_c").mkdir(parents=True)
    (_data_dir(tmp_path) / _SPECS[store].id_map).write_text(
        json.dumps({"g3": {"prefix_path": str(root)}}),
    )

    with pytest.raises(d.GameNotFoundError):
        await d._build_context(["launcher", f"{store}:g3"], _NoRow())


# ── the other half of the hatch: installed, but no exe ──────────


@pytest.mark.parametrize("store", STORES)
@pytest.mark.asyncio
async def test_an_installed_title_with_no_exe_plays_the_game(
    store: str, tmp_path,
) -> None:
    """A games.map row is the "installed" signal, whatever the exe says.

    Routing this to the install action instead is the regression where Play
    re-opened the vendor client rather than starting the game.
    """
    _bootstrap_prefix(tmp_path, store, "g4", record=True)

    ctx = await d._build_context(["launcher", f"{store}:g4"], _RowWithoutExe())

    assert ctx.is_launch_action is True
    assert ctx.action != "install"
    assert ctx.game_id == "g4"


# ── the mechanism ───────────────────────────────────────────────


def test_every_wrapper_store_has_a_probe_row() -> None:
    """The gate is ``is_wrapper_store``, so a store missing its row would
    reach the hatch and silently never match."""
    assert set(_SPECS) == set(WRAPPER_STORES)


def test_a_non_wrapper_store_is_never_probed(tmp_path) -> None:
    from unifideck.launcher.wrapper_prefix_probe import wrapper_prefix_is_populated

    assert wrapper_prefix_is_populated("epic", "anything") is False
