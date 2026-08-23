"""Battle.net prefix lifecycle and the shared clone helpers.

For wrapper stores the game's files live *inside* the prefix, so every
destructive operation here can take a user's install with it. The tests
that matter most are therefore the refusals: unmarked prefixes are never
deleted or repaired, repair never uses ``--delete``, and an unwarmed
template is never cloned.

That last one is not hypothetical. On 2026-08-09 a freshly installed client
self-updated within five minutes and then demanded a restart through a modal
that cannot be clicked in Gaming Mode; cloning a stale template would stall
every install behind it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from _wine_session import write_registry

from unifideck.stores.battlenet import paths
from unifideck.stores.battlenet.prefix import (
    MARKER_FILENAME,
    DERIVED_MARKER,
    BattlenetPrefixManager,
    inspect_prefix,
)
from unifideck.stores.shared import prefix_clone as pc


def _install_client(prefix: Path) -> None:
    client = prefix / "drive_c" / paths.CLIENT_DIR
    client.mkdir(parents=True, exist_ok=True)
    (client / paths.CLIENT_EXE).write_bytes(b"MZ")
    (client / paths.LAUNCHER_EXE).write_bytes(b"MZ")
    # The versioned payload the shim loads. Without it the prefix is
    # the shape an interrupted install leaves and no client can start.
    build = client / "Battle.net.17651"
    build.mkdir(exist_ok=True)
    (build / paths.CLIENT_DLL).write_bytes(b"MZ")


def _make_auth(root: Path) -> Path:
    """A signed-in auth prefix — the only legitimate template source."""
    auth = paths.auth_prefix(root)
    auth.mkdir(parents=True, exist_ok=True)
    _install_client(auth)
    return auth


def _make_template(root: Path, *, derived: bool = True, marked: bool = True) -> Path:
    template = paths.template_prefix(root)
    template.mkdir(parents=True, exist_ok=True)
    _install_client(template)
    if marked:
        pc.write_marker(
            template, MARKER_FILENAME,
            pc.PrefixMarker(store="battlenet", created_at=1.0, client_build="17651"),
        )
    if derived:
        (template / DERIVED_MARKER).write_text("")
    return template


# --------------------------------------------------------------------------
# markers — the only proof of ownership
# --------------------------------------------------------------------------


def test_marker_roundtrip(tmp_path: Path) -> None:
    marker = pc.PrefixMarker(store="battlenet", created_at=123.0, client_build="17651")
    assert pc.write_marker(tmp_path, MARKER_FILENAME, marker) is True
    read = pc.read_marker(tmp_path, MARKER_FILENAME)
    assert read.store == "battlenet"
    assert read.client_build == "17651"


def test_unmarked_prefix_is_never_ours(tmp_path: Path) -> None:
    assert pc.read_marker(tmp_path, MARKER_FILENAME) is None
    assert pc.is_owned_by(tmp_path, MARKER_FILENAME, "battlenet") is False


def test_marker_naming_a_different_store_is_not_ours(tmp_path: Path) -> None:
    pc.write_marker(tmp_path, MARKER_FILENAME, pc.PrefixMarker(store="ubisoft", created_at=1.0))
    assert pc.is_owned_by(tmp_path, MARKER_FILENAME, "battlenet") is False


def test_corrupt_marker_still_counts_as_ours(tmp_path: Path) -> None:
    """We wrote it, so we may clean it up — but it names no store."""
    (tmp_path / MARKER_FILENAME).write_text("{not json")
    assert pc.read_marker(tmp_path, MARKER_FILENAME) is not None
    assert pc.is_owned_by(tmp_path, MARKER_FILENAME, "battlenet") is False


# --------------------------------------------------------------------------
# clone / repair
# --------------------------------------------------------------------------


def test_clone_marks_the_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _install_client(src)
    dst = tmp_path / "dst"
    ok = asyncio.run(pc.clone_template(
        src, dst, store="battlenet", marker_filename=MARKER_FILENAME, now=5.0,
    ))
    assert ok is True
    assert pc.is_owned_by(dst, MARKER_FILENAME, "battlenet")
    assert (dst / "drive_c" / paths.CLIENT_DIR / paths.CLIENT_EXE).is_file()


def test_clone_from_a_missing_template_fails_cleanly(tmp_path: Path) -> None:
    ok = asyncio.run(pc.clone_template(
        tmp_path / "absent", tmp_path / "dst",
        store="battlenet", marker_filename=MARKER_FILENAME,
    ))
    assert ok is False


def test_repair_keeps_the_installed_game(tmp_path: Path) -> None:
    """The whole point: repair must not eat the games directory."""
    template = tmp_path / "template"
    _install_client(template)
    (template / "drive_c" / "identity.txt").write_text("fresh")

    game = tmp_path / "game"
    _install_client(game)
    games_dir = game / pc.GAMES_DIR_NAME
    games_dir.mkdir()
    (games_dir / "Hearthstone.bin").write_text("12 GB of game")
    (game / "drive_c" / "identity.txt").write_text("stale")

    assert asyncio.run(pc.repair_from_template(template, game)) is True
    assert (games_dir / "Hearthstone.bin").read_text() == "12 GB of game"
    assert (game / "drive_c" / "identity.txt").read_text() == "fresh"


def test_repair_does_not_delete_files_absent_from_the_template(tmp_path: Path) -> None:
    template = tmp_path / "template"
    _install_client(template)
    game = tmp_path / "game"
    _install_client(game)
    extra = game / "drive_c" / "user_data.txt"
    extra.write_text("keep me")
    assert asyncio.run(pc.repair_from_template(template, game)) is True
    assert extra.is_file()


def test_pfx_selflink_is_restored(tmp_path: Path) -> None:
    """Losing it makes the client unfindable though it is present."""
    prefix = tmp_path / "p"
    prefix.mkdir()
    pc.ensure_pfx_symlink(prefix)
    assert (prefix / "pfx").is_symlink()
    pc.ensure_pfx_symlink(prefix)  # idempotent


# --------------------------------------------------------------------------
# manager
# --------------------------------------------------------------------------


def test_three_tiers_have_distinct_paths(tmp_path: Path) -> None:
    mgr = BattlenetPrefixManager(tmp_path)
    assert len({mgr.auth_prefix, mgr.template_prefix, mgr.game_prefix("wow")}) == 3


def test_a_template_not_derived_from_auth_is_rebuilt(tmp_path: Path) -> None:
    """The defect this whole tier exists to prevent.

    A standalone template carries its own client identity, so the session
    in a clone of it is rejected and the user is asked to sign in for
    every game. Measured on-device: copying the token without a matching
    ``GaClientId`` still produced a password form.
    """
    _make_template(tmp_path, derived=False)
    _make_auth(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    assert mgr.template_ready() is False

    assert asyncio.run(mgr.create_game_prefix("wow")) is not None
    assert mgr.template_ready() is True, "template must be re-derived from auth"


def test_refuses_to_clone_when_there_is_no_signed_in_auth_prefix(tmp_path: Path) -> None:
    """No auth prefix means no session to inherit — refuse, do not guess."""
    _make_template(tmp_path, derived=False)
    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.create_game_prefix("wow")) is None
    assert not mgr.game_prefix("wow").exists()


def test_refuses_to_clone_a_template_with_no_client(tmp_path: Path) -> None:
    template = paths.template_prefix(tmp_path)
    (template / "drive_c").mkdir(parents=True)
    (template / DERIVED_MARKER).write_text("")
    assert BattlenetPrefixManager(tmp_path).template_ready() is False


def test_the_template_is_derived_from_auth_not_installed(tmp_path: Path) -> None:
    """Ubisoft's shared-identity invariant, now held for Battle.net too."""
    auth = _make_auth(tmp_path)
    (auth / "drive_c" / "session-token").write_text("secret")
    mgr = BattlenetPrefixManager(tmp_path)

    assert asyncio.run(mgr.ensure_template()) is True
    assert mgr.template_ready() is True
    # The session came across because the WHOLE prefix did.
    assert (mgr.template_prefix / "drive_c" / "session-token").read_text() == "secret"


def test_a_game_prefix_inherits_the_session(tmp_path: Path) -> None:
    auth = _make_auth(tmp_path)
    (auth / "drive_c" / "session-token").write_text("secret")
    mgr = BattlenetPrefixManager(tmp_path)

    created = asyncio.run(mgr.create_game_prefix("hs_beta"))

    assert created is not None
    assert (created / "drive_c" / "session-token").read_text() == "secret"


def test_creates_a_marked_game_prefix_from_a_derived_template(tmp_path: Path) -> None:
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    created = asyncio.run(mgr.create_game_prefix("hs_beta"))
    assert created is not None
    status = inspect_prefix(created)
    assert status.usable and status.is_ours


def test_clone_records_the_template_client_build(tmp_path: Path) -> None:
    """Lets self-update repair know which build the prefix started from."""
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    created = asyncio.run(mgr.create_game_prefix("hs_beta"))
    assert pc.read_marker(created, MARKER_FILENAME).client_build == "17651"


def test_existing_prefix_is_returned_not_reclobbered(tmp_path: Path) -> None:
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    existing = mgr.game_prefix("hs_beta")
    _install_client(existing)
    (existing / "drive_c" / "precious.txt").write_text("do not lose me")
    assert asyncio.run(mgr.create_game_prefix("hs_beta")) == existing
    assert (existing / "drive_c" / "precious.txt").is_file()


def test_a_prefix_is_created_at_the_requested_destination(tmp_path: Path) -> None:
    """Placement is how a game reaches the SD card — the install lives inside."""
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    elsewhere = tmp_path / "sd" / "Games" / "prefixes" / "battlenet" / "hs_beta"

    created = asyncio.run(mgr.create_game_prefix("hs_beta", elsewhere))

    assert created == elsewhere
    assert inspect_prefix(elsewhere).usable
    assert not mgr.game_prefix("hs_beta").exists()


def test_a_half_written_prefix_is_completed_not_returned(tmp_path: Path) -> None:
    """An interrupted clone to removable media leaves no client behind.

    Returning it as-is fails later at launch on a missing exe; the clone is
    additive (no ``--delete``), so falling through finishes the job.
    """
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    partial = mgr.game_prefix("hs_beta")
    (partial / "drive_c").mkdir(parents=True)

    created = asyncio.run(mgr.create_game_prefix("hs_beta"))

    assert created == partial
    assert inspect_prefix(partial).usable


def test_never_deletes_the_auth_or_template_prefix(tmp_path: Path) -> None:
    """The template carries our marker too, so the marker alone cannot save it.

    Harmless while every caller read its path back from the id map; prefix
    placement now *computes* paths, so the shared tiers are named explicitly.
    """
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)

    assert mgr.remove_game_prefix(mgr.template_prefix) is False
    assert mgr.remove_game_prefix(mgr.auth_prefix) is False
    assert inspect_prefix(mgr.template_prefix).usable
    assert inspect_prefix(mgr.auth_prefix).usable


def test_never_deletes_an_unmarked_prefix(tmp_path: Path) -> None:
    """A prefix under our directory is not proof we made it."""
    mgr = BattlenetPrefixManager(tmp_path)
    stranger = mgr.game_prefix("not_ours")
    _install_client(stranger)
    assert mgr.remove_game_prefix(stranger) is False
    assert stranger.is_dir()


def test_deletes_a_prefix_we_marked(tmp_path: Path) -> None:
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    created = asyncio.run(mgr.create_game_prefix("hs_beta"))
    assert mgr.remove_game_prefix(created) is True
    assert not created.exists()


def test_removing_an_absent_prefix_is_a_no_op(tmp_path: Path) -> None:
    mgr = BattlenetPrefixManager(tmp_path)
    assert mgr.remove_game_prefix(tmp_path / "gone") is True


def test_never_repairs_an_unmarked_prefix(tmp_path: Path) -> None:
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    stranger = mgr.game_prefix("not_ours")
    _install_client(stranger)
    assert asyncio.run(mgr.repair_game_prefix(stranger)) is False


def test_an_already_derived_template_is_not_rebuilt(tmp_path: Path) -> None:
    """Re-deriving on every install would copy 1.2 GB each time."""
    _make_auth(tmp_path)
    _make_auth(tmp_path)
    _make_template(tmp_path)
    mgr = BattlenetPrefixManager(tmp_path)
    (mgr.template_prefix / "drive_c" / "sentinel").write_text("kept")

    assert asyncio.run(mgr.ensure_template()) is True
    assert (mgr.template_prefix / "drive_c" / "sentinel").read_text() == "kept"


@pytest.mark.parametrize("attr", ["auth_prefix", "template_prefix"])
def test_special_prefixes_are_dot_prefixed(tmp_path: Path, attr: str) -> None:
    """So a game uid can never collide with them."""
    assert getattr(BattlenetPrefixManager(tmp_path), attr).name.startswith(".")


# --------------------------------------------------------------------------
# template freshness
# --------------------------------------------------------------------------
#
# Deriving the template once was only half the fix. Blizzard rotates the token
# on every client run, so a snapshot goes server-stale: measured on-device,
# `.bnet-auth` and `.template` were byte-identical and frozen at 08:57 while a
# game prefix's client had rewritten everything at 21:15. Re-signing-in fixed
# only the auth prefix, and each Install stamped the dead token back over any
# prefix that had since refreshed itself.

VAULT_REL = (
    "drive_c/users/steamuser/AppData/Local/Battle.net/Account/1/account.db"
)
CONFIG_REL = (
    "drive_c/users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"
)


def _put_session(
    prefix: Path, *, mtime: float, vault: bytes, token: str = "tok",
) -> None:
    write_registry(prefix, stamp=int(mtime), token=token)
    vault_path = prefix / VAULT_REL
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(vault)
    os.utime(vault_path, (mtime, mtime))
    config = prefix / CONFIG_REL
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"Client": {"GaClientId": "GUID-A"}}))


def test_a_ready_template_has_its_session_refreshed_from_auth(tmp_path: Path) -> None:
    auth = _make_auth(tmp_path)
    template = _make_template(tmp_path)
    _put_session(auth, mtime=2000.0, vault=b"fresh")
    _put_session(template, mtime=1000.0, vault=b"stale")

    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.ensure_template()) is True
    assert (template / VAULT_REL).read_bytes() == b"fresh"


def test_refreshing_a_template_does_not_rebuild_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh is a few small files, not a 12 s / 1.6 GB recopy.

    It also must not ``rmtree`` the template: an Install running at the same
    time clones from it.
    """
    auth = _make_auth(tmp_path)
    template = _make_template(tmp_path)
    _put_session(auth, mtime=2000.0, vault=b"fresh")
    _put_session(template, mtime=1000.0, vault=b"stale")
    monkeypatch.setattr(
        pc, "rsync_clone",
        lambda *a, **k: pytest.fail("a ready template must not be re-cloned"),
    )

    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.ensure_template()) is True
    assert (template / VAULT_REL).read_bytes() == b"fresh"


def test_a_template_newer_than_auth_is_left_alone(tmp_path: Path) -> None:
    """Never roll a session backwards."""
    auth = _make_auth(tmp_path)
    template = _make_template(tmp_path)
    _put_session(auth, mtime=1000.0, vault=b"older")
    _put_session(template, mtime=2000.0, vault=b"newer")

    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.ensure_template()) is True
    assert (template / VAULT_REL).read_bytes() == b"newer"


def test_a_failed_refresh_still_reports_a_usable_template(tmp_path: Path) -> None:
    """Older is fine; failing the install over it is not."""
    _make_auth(tmp_path)
    template = _make_template(tmp_path)
    _put_session(template, mtime=1000.0, vault=b"stale")
    # Auth has no session at all, so there is nothing to refresh from.

    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.ensure_template()) is True
    assert (template / VAULT_REL).read_bytes() == b"stale"


def test_a_busy_auth_client_blocks_the_refresh_not_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying out from under a live client reads a torn vault."""
    auth = _make_auth(tmp_path)
    template = _make_template(tmp_path)
    _put_session(auth, mtime=2000.0, vault=b"fresh")
    _put_session(template, mtime=1000.0, vault=b"stale")
    monkeypatch.setattr(BattlenetPrefixManager, "_auth_is_busy", lambda _self: True)

    mgr = BattlenetPrefixManager(tmp_path)
    assert asyncio.run(mgr.ensure_template()) is True
    assert (template / VAULT_REL).read_bytes() == b"stale"
