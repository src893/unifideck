"""Battle.net installs land on the disk the user picked.

The bug this pins was reported as a disk-space error, not a wrong path. The
Battle.net client's installer refused an 83.40 GB download for *The Outer
Worlds 2* with "Insufficient disk space" while the SD card the user had
selected in Unifideck had 164 GB free — because the prefix was still being
created on the 45 GB internal drive, and the game installs *inside* the
prefix, so that is the volume Wine reported as ``C:``.

The dangerous half is the reclaim: the prefix and the game are the same
directory tree, so an over-eager cleanup deletes a download rather than a
scratch folder. Those refusals are the assertions worth keeping.

These drive ``installer.prepare`` rather than ``store.install_game``, because
placement is only the first half of an install now: the second half blocks
until the Battle.net client has actually put the game on disk. That split is
itself a fix — reporting success at placement time is what showed a Play
button on a game that had not downloaded a byte.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from _wine_session import write_registry

from unifideck.stores.battlenet import BattlenetStore
from unifideck.stores.battlenet import paths as bpaths
from unifideck.stores.battlenet.install import PreparedInstall
from unifideck.stores.battlenet.ownership.installed import AGGREGATE_RELATIVE
from unifideck.stores.battlenet.prefix import MARKER_FILENAME, DERIVED_MARKER
from unifideck.stores.battlenet.product_db.reader import PRODUCT_DB_RELATIVE
from unifideck.stores.shared import prefix_clone as pc

FIXTURES = Path(__file__).parent.parent / "fixtures" / "battlenet"
UID = "ark"
FAMILY = "ARK"


@dataclass(frozen=True)
class _Placement:
    """``prepare``'s two return shapes, flattened for assertions."""

    success: bool
    install_path: str | None = None
    error: str | None = None
    error_code: str | None = None


def _place(store: BattlenetStore, install_path: str | None = None) -> _Placement:
    """Run just the placement half of an install."""
    outcome = asyncio.run(store._installer.prepare(UID, install_path))
    if isinstance(outcome, PreparedInstall):
        return _Placement(True, str(outcome.prefix))
    return _Placement(False, None, outcome.error, outcome.error_code)


class _Bus:
    async def emit(self, *_a: Any, **_k: Any) -> None:
        return None


class _Cache:
    def get(self, *_a: Any, **_k: Any) -> None:
        return None

    def clear(self, *_a: Any, **_k: Any) -> None:
        return None


class _Config:
    def __init__(self, data_dir: Path, prefixes_dir: Path) -> None:
        self._values = {
            "data_dir": str(data_dir),
            "prefixes_dir": str(prefixes_dir),
            "installer_cache_dir": str(data_dir / "installer-cache"),
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._values if key == "stores.battlenet" else default


def _install_client(prefix: Path) -> None:
    client = prefix / "drive_c" / bpaths.CLIENT_DIR
    client.mkdir(parents=True, exist_ok=True)
    (client / bpaths.CLIENT_EXE).write_bytes(b"MZ")
    (client / bpaths.LAUNCHER_EXE).write_bytes(b"MZ")
    # The versioned payload the shim loads. Without it the prefix is
    # the shape an interrupted install leaves and no client can start.
    build = client / "Battle.net.17651"
    build.mkdir(exist_ok=True)
    (build / bpaths.CLIENT_DLL).write_bytes(b"MZ")


def _mark(prefix: Path) -> None:
    pc.write_marker(
        prefix, MARKER_FILENAME,
        pc.PrefixMarker(store="battlenet", created_at=1.0, client_build="17651"),
    )


@pytest.fixture
def store(tmp_path: Path) -> BattlenetStore:
    """A signed-in store with a ready auth prefix and template."""
    prefixes = tmp_path / "prefixes"
    prefixes.mkdir(parents=True)
    st = BattlenetStore(
        _Bus(), _Cache(), plugin_dir="/plugin",
        config=_Config(tmp_path, prefixes),
    )
    auth = st.prefixes.auth_prefix
    auth.mkdir(parents=True, exist_ok=True)
    _install_client(auth)
    db = auth / "drive_c/users/steamuser/AppData/Local/Battle.net/CachedData.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE key_value_store (key TEXT, value TEXT)")
    con.execute(
        "INSERT INTO key_value_store VALUES ('features_cached_data_points', ?)",
        (json.dumps({"licenses": [1105059], "account_id": 1}),),
    )
    con.commit()
    con.close()

    template = st.prefixes.template_prefix
    template.mkdir(parents=True, exist_ok=True)
    _install_client(template)
    _mark(template)
    (template / DERIVED_MARKER).write_text("")

    st.id_map.merge(UID, family=FAMILY)
    return st


def _make_prefix(path: Path, *, marked: bool = True) -> Path:
    """An existing per-game prefix, client installed."""
    path.mkdir(parents=True, exist_ok=True)
    _install_client(path)
    if marked:
        _mark(path)
    return path


def _install_a_game(prefix: Path) -> None:
    """Give a prefix the client state that proves a real install lives here."""
    drive_c = prefix / "drive_c"
    for relative, source in (
        (AGGREGATE_RELATIVE, "aggregate_installed.json"),
        (PRODUCT_DB_RELATIVE, "product_db_installed.bin"),
    ):
        target = drive_c / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FIXTURES / source).read_bytes())


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------


def test_install_lands_on_the_picked_storage(store: BattlenetStore, tmp_path: Path) -> None:
    """The whole bug: the pick has to reach the prefix, or nothing moves."""
    base = tmp_path / "sd" / "Games"

    result = _place(store, str(base))

    assert result.success, result.error
    expected = base / "prefixes" / "battlenet" / UID
    assert Path(result.install_path) == expected
    assert expected.is_dir()
    # Recorded, never reconstructed — the launcher reads this back.
    assert store.id_map.resolve_prefix(UID) == expected


def test_install_without_a_pick_uses_the_internal_default(
    store: BattlenetStore,
) -> None:
    result = _place(store)

    assert result.success, result.error
    assert Path(result.install_path) == store.prefixes.game_prefix(UID)


def test_the_prefix_is_recorded_before_the_clone_runs(
    store: BattlenetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted rsync must leave a reachable prefix, not an orphan.

    Rebuilding the path from the uid is what wedged a Ubisoft prefix in a
    permanent reset loop, so the id map has to know the location before
    anything can fail.
    """
    base = tmp_path / "sd" / "Games"
    seen: list[Path | None] = []

    async def _fail(uid: str, destination: Path | None = None) -> None:
        seen.append(store.id_map.resolve_prefix(uid))
        return None

    monkeypatch.setattr(store.prefixes, "create_game_prefix", _fail)

    result = _place(store, str(base))

    assert result.success is False
    assert result.error_code == "prefix_clone_failed"
    assert seen == [base / "prefixes" / "battlenet" / UID]


# --------------------------------------------------------------------------
# reinstall — always fresh, but never through a real install
# --------------------------------------------------------------------------


def test_reinstalling_elsewhere_reclaims_the_old_prefix(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    old = _make_prefix(store.prefixes.game_prefix(UID))
    store.id_map.merge(UID, prefix_path=str(old))
    base = tmp_path / "sd" / "Games"

    result = _place(store, str(base))

    assert result.success, result.error
    assert not old.exists(), "the abandoned internal prefix should be reclaimed"
    assert Path(result.install_path) == base / "prefixes" / "battlenet" / UID


def test_an_unmarked_prefix_is_never_deleted(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    """No marker, no proof we made it — so it is not ours to remove."""
    old = _make_prefix(tmp_path / "somewhere" / "else", marked=False)
    (old / "user-data.txt").write_text("precious")
    store.id_map.merge(UID, prefix_path=str(old))

    result = _place(store, str(tmp_path / "sd" / "Games"))

    assert result.success, result.error
    assert (old / "user-data.txt").read_text() == "precious"


# --------------------------------------------------------------------------
# the shared tiers are not game prefixes
# --------------------------------------------------------------------------


def test_the_auth_and_template_prefixes_survive_an_install(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    """The template carries our marker too, so the marker alone cannot save it."""
    store.id_map.merge(UID, prefix_path=str(store.prefixes.template_prefix))

    result = _place(store, str(tmp_path / "sd" / "Games"))

    assert result.success, result.error
    assert store.prefixes.template_prefix.is_dir()
    assert bpaths.client_installed(store.prefixes.template_prefix)
    assert bpaths.client_installed(store.prefixes.auth_prefix)


def test_remove_game_prefix_refuses_the_shared_tiers(
    store: BattlenetStore,
) -> None:
    assert store.prefixes.remove_game_prefix(store.prefixes.auth_prefix) is False
    assert store.prefixes.remove_game_prefix(store.prefixes.template_prefix) is False
    assert store.prefixes.auth_prefix.is_dir()
    assert store.prefixes.template_prefix.is_dir()


# --------------------------------------------------------------------------
# abandoned cleanup
# --------------------------------------------------------------------------


def test_a_failed_install_reclaims_its_partial_prefix(
    store: BattlenetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-written clone must not squat on the disk the user picked."""
    base = tmp_path / "sd" / "Games"
    target = base / "prefixes" / "battlenet" / UID

    async def _half_write(uid: str, destination: Path | None = None) -> None:
        _make_prefix(Path(destination or target))
        return None

    monkeypatch.setattr(store.prefixes, "create_game_prefix", _half_write)

    result = _place(store, str(base))

    assert result.success is False
    assert not target.exists()
    assert store.id_map.resolve_prefix(UID) is None
    # The family code survives — only the location is forgotten.
    assert store.id_map.resolve_family(UID) == FAMILY


def test_a_failed_install_keeps_a_prefix_that_holds_a_game(
    store: BattlenetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix IS the install, so cleanup here would delete a real game."""
    base = tmp_path / "sd" / "Games"
    target = base / "prefixes" / "battlenet" / UID

    async def _leave_a_game(uid: str, destination: Path | None = None) -> None:
        _install_a_game(_make_prefix(Path(destination or target)))
        return None

    monkeypatch.setattr(store.prefixes, "create_game_prefix", _leave_a_game)

    result = _place(store, str(base))

    assert result.success is False
    assert target.is_dir(), "a prefix holding a game must never be reclaimed"
    assert store.id_map.resolve_prefix(UID) == target


# --------------------------------------------------------------------------
# across the process boundary
# --------------------------------------------------------------------------


def test_the_launcher_resolves_the_relocated_prefix(
    store: BattlenetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher is a separate process reading the id map off disk."""
    from unifideck.launcher.proton.handlers import battlenet_client as client

    base = tmp_path / "sd" / "Games"
    _place(store, str(base))
    monkeypatch.setattr(client, "id_map_path", lambda p=store.id_map.path: p)

    assert client.resolve_prefix(UID) == base / "prefixes" / "battlenet" / UID


# --------------------------------------------------------------------------
# the session survives the rebuild
# --------------------------------------------------------------------------


def _put_session(
    prefix: Path, *, mtime: float, vault: bytes, token: str = "tok",
) -> Path:
    write_registry(prefix, stamp=int(mtime), token=token)
    vault_path = (
        prefix
        / "drive_c/users/steamuser/AppData/Local/Battle.net/Account/1/account.db"
    )
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(vault)
    os.utime(vault_path, (mtime, mtime))
    config = (
        prefix
        / "drive_c/users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"Client": {"GaClientId": "GUID-A"}}))
    return vault_path


def test_install_captures_the_session_before_wiping_the_old_prefix(
    store: BattlenetStore,
) -> None:
    """Install rebuilds the prefix, so the reset is a session-loss window.

    The prefix being deleted has had a client running in it, so it holds a
    newer token than the auth prefix. Deleting it uncaptured is exactly what
    made the *next* install open signed-out.
    """
    auth_vault = _put_session(store.prefixes.auth_prefix, mtime=1000.0, vault=b"stale")
    old = _make_prefix(store.prefixes.game_prefix(UID))
    _put_session(old, mtime=2000.0, vault=b"rotated")
    store.id_map.merge(UID, prefix_path=str(old))

    _place(store)

    assert auth_vault.read_bytes() == b"rotated"


def test_a_relocated_install_captures_from_the_old_disk_first(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    """Picking a new disk abandons the old prefix — capture on the way out."""
    auth_vault = _put_session(store.prefixes.auth_prefix, mtime=1000.0, vault=b"stale")
    old = _make_prefix(store.prefixes.game_prefix(UID))
    _put_session(old, mtime=2000.0, vault=b"rotated-on-internal")
    store.id_map.merge(UID, prefix_path=str(old))

    sdcard = tmp_path / "sdcard"
    sdcard.mkdir()
    _place(store, str(sdcard))

    assert auth_vault.read_bytes() == b"rotated-on-internal"
    assert not old.is_dir()


# --------------------------------------------------------------------------
# an incomplete client is named as such, not as "you are not signed in"
# --------------------------------------------------------------------------


def test_an_incomplete_client_is_not_reported_as_signed_out(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    """The tester's state: signed in fine, client half-installed.

    Both reach the same gate. "Sign in first" to a user who signed in an
    hour ago reads as a bug in the sign-in, which is how one report came
    back a second time — the fix is the same action, but only if the
    message says which thing is broken.
    """
    payload = bpaths.client_payload_dir(store.prefixes.auth_prefix)
    for leftover in payload.iterdir():
        leftover.unlink()
    payload.rmdir()

    result = _place(store, str(tmp_path / "sd" / "Games"))

    assert result.success is False
    assert result.error_code == "client_incomplete"
    assert "reinstalls the client" in result.error


def test_a_genuinely_absent_client_still_says_sign_in(
    store: BattlenetStore, tmp_path: Path,
) -> None:
    """The other half of the same gate keeps its original words."""
    import shutil

    shutil.rmtree(bpaths.client_dir(store.prefixes.auth_prefix))

    result = _place(store, str(tmp_path / "sd" / "Games"))

    assert result.success is False
    assert result.error_code == "not_signed_in"
