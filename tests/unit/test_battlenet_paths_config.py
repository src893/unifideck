"""Battle.net prefix paths, id map and config.

Three behaviours here are guards against measured incidents rather than
hypotheticals:

  * ``drive_c`` must resolve through both prefix layouts, because umu
    creates ``pfx -> .`` as a self-symlink and a naive
    ``prefix / "drive_c"`` combine already failed once for Ubisoft,
  * a per-game prefix path is **recorded, never reconstructed** — a rebuilt
    path once stamped a marker into a directory no launch opened, causing a
    permanent reset loop,
  * prefix ownership comes from an in-directory marker, never from the
    path, because appid inference nearly deleted a gigabyte of user
    prefixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.stores.battlenet import config as cfg
from unifideck.stores.battlenet import paths
from unifideck.stores.battlenet.id_map import BattlenetIdMap, GameRecord


#: A real build number, as observed on-device beside ``Battle.net.17554``.
_BUILD_DIR = "Battle.net.17651"


def _make_prefix(
    root: Path,
    *,
    layout: str = "modern",
    client: bool = True,
    payload: bool = True,
) -> Path:
    """Build a prefix tree in either layout umu can produce.

    ``payload=False`` reproduces the shape an *interrupted* client install
    leaves: the shim executables present, the versioned client they load
    missing. That prefix used to pass every "is the client here" check.

    The ``payload=True`` shape is the REAL one, measured on this Deck at
    build 17651 — the client is ``battle.net.dll``, and the payload dir
    contains no ``Battle.net.exe`` at all. The fixture used to fabricate one
    there, which is why it could not catch the inverse bug: keying the check
    on that exe reported every real client as incomplete. See
    :func:`test_a_real_payload_has_no_exe_only_the_client_dll`.
    """
    prefix = root / "pfx-under-test"
    drive_c = prefix / ("pfx/drive_c" if layout == "modern" else "drive_c")
    drive_c.mkdir(parents=True)
    if client:
        client_dir = drive_c / paths.CLIENT_DIR
        client_dir.mkdir(parents=True)
        (client_dir / paths.CLIENT_EXE).write_bytes(b"MZ")
        (client_dir / paths.LAUNCHER_EXE).write_bytes(b"MZ")
        if payload:
            _write_payload(client_dir / _BUILD_DIR)
    return prefix


def _write_payload(build: Path) -> Path:
    """A complete payload dir, in the shape Blizzard actually ships.

    The auxiliary exes are included deliberately: they are what a payload
    dir really holds, they land early, and a check keyed on "some exe is
    here" must not accept them as the client.
    """
    build.mkdir(parents=True, exist_ok=True)
    (build / paths.CLIENT_DLL).write_bytes(b"MZ")       # the client
    (build / "libcef.dll").write_bytes(b"MZ")
    (build / "Battle.net.mpq").write_bytes(b"MPQ")
    (build / "BlizzardError.exe").write_bytes(b"MZ")    # auxiliary
    (build / "GameSessionMonitor.exe").write_bytes(b"MZ")
    return build


# --------------------------------------------------------------------------
# prefix layout
# --------------------------------------------------------------------------


@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_drive_c_resolves_in_both_layouts(tmp_path: Path, layout: str) -> None:
    prefix = _make_prefix(tmp_path, layout=layout)
    assert paths.drive_c(prefix) is not None


def test_pfx_self_symlink_layout_resolves(tmp_path: Path) -> None:
    """umu creates 'pfx -> .', so both spellings are the same directory."""
    prefix = _make_prefix(tmp_path, layout="legacy")
    (prefix / "pfx").symlink_to(".")
    assert paths.drive_c(prefix) is not None
    assert paths.client_exe(prefix) is not None


def test_client_binaries_are_found(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    assert paths.client_exe(prefix).name == "Battle.net.exe"
    assert paths.launcher_exe(prefix).name == "Battle.net Launcher.exe"
    assert paths.client_installed(prefix) is True


def test_missing_client_is_reported_not_guessed(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path, client=False)
    assert paths.client_exe(prefix) is None
    assert paths.client_installed(prefix) is False


def test_prefix_without_drive_c_is_not_a_prefix(tmp_path: Path) -> None:
    assert paths.drive_c(tmp_path / "nope") is None
    assert paths.client_installed(tmp_path / "nope") is False


# --------------------------------------------------------------------------
# client completeness — the shim is not the client
# --------------------------------------------------------------------------


def test_a_shim_without_its_payload_is_not_an_installed_client(tmp_path: Path) -> None:
    """The exact prefix an interrupted client install leaves behind.

    ``Battle.net.exe`` next to the launcher is a ~1 MB shim written early;
    the client it loads lands later in ``Battle.net.<build>/``. Reported
    from the field on a ROG Ally X: a sign-in stopped mid-install left this
    shape, ``client_installed`` said yes, the template was derived from it,
    and every game prefix cloned from that started a launcher with nothing
    to hand off to — 300 s of spinner per install, forever, with no user
    action that repaired it.
    """
    prefix = _make_prefix(tmp_path, payload=False)
    assert paths.client_exe(prefix) is not None
    assert paths.launcher_exe(prefix) is not None
    assert paths.client_payload_dir(prefix) is None
    assert paths.client_installed(prefix) is False


def test_a_payload_directory_without_its_exe_does_not_count(tmp_path: Path) -> None:
    """A half-written payload directory is what an interrupted install makes."""
    prefix = _make_prefix(tmp_path, payload=False)
    (paths.client_dir(prefix) / _BUILD_DIR).mkdir()
    assert paths.client_payload_dir(prefix) is None
    assert paths.client_installed(prefix) is False


def test_the_newest_payload_wins(tmp_path: Path) -> None:
    """The client self-updates into a new sibling; the newest is the live one."""
    prefix = _make_prefix(tmp_path)
    _write_payload(paths.client_dir(prefix) / "Battle.net.17999")
    assert paths.client_payload_dir(prefix).name == "Battle.net.17999"


def test_a_real_payload_has_no_exe_only_the_client_dll(tmp_path: Path) -> None:
    """The regression that blocked every Battle.net install on 0.7.4.

    The completeness check was keyed on ``<build>/Battle.net.exe`` — a file
    Blizzard never writes. The payload dir holds the client as
    ``battle.net.dll`` beside ``libcef.dll`` and ``Battle.net.mpq``; its only
    exes are auxiliary tools. So ``client_installed`` was False for every
    correctly installed client: installs were refused with "client files are
    incomplete" and the error's own advice ("sign in again") could never fix
    it. Measured on-device at build 17651, where the client had installed
    fine and signed the user in.
    """
    prefix = _make_prefix(tmp_path)
    payload = paths.client_payload_dir(prefix)

    assert payload is not None
    assert not (payload / paths.CLIENT_EXE).exists()   # the whole bug
    assert (payload / paths.CLIENT_DLL).is_file()
    assert paths.client_installed(prefix) is True


def test_auxiliary_exes_alone_are_not_the_client(tmp_path: Path) -> None:
    """A payload with only its small early exes is still incomplete.

    Guards the direction the DLL keying must not lose: those two exes land
    before the 28 MB client DLL, so accepting "an exe is present" would
    reinstate the original unrecoverable bug.
    """
    prefix = _make_prefix(tmp_path, payload=False)
    build = paths.client_dir(prefix) / _BUILD_DIR
    build.mkdir()
    (build / "BlizzardError.exe").write_bytes(b"MZ")
    (build / "GameSessionMonitor.exe").write_bytes(b"MZ")

    assert paths.client_payload_dir(prefix) is None
    assert paths.client_installed(prefix) is False


def test_client_dll_match_is_case_insensitive(tmp_path: Path) -> None:
    """One Blizzard capitalisation change must not break every client."""
    prefix = _make_prefix(tmp_path, payload=False)
    build = paths.client_dir(prefix) / _BUILD_DIR
    build.mkdir()
    (build / "Battle.Net.DLL").write_bytes(b"MZ")

    assert paths.client_payload_dir(prefix) is not None
    assert paths.client_installed(prefix) is True


@pytest.mark.parametrize("payload", [True, False])
@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_backend_and_launcher_agree_on_completeness(
    tmp_path: Path, layout: str, payload: bool,
) -> None:
    """The rule is written twice and must never drift.

    The launcher copy runs out-of-process under the SYSTEM python and
    cannot import the backend, so the two implementations are independent
    code. This is the only thing holding them together.
    """
    from unifideck.launcher.proton.handlers import battlenet_client as launcher_side

    prefix = _make_prefix(tmp_path, layout=layout, payload=payload)
    assert launcher_side.client_installed(prefix) is paths.client_installed(prefix)
    assert (launcher_side.find_payload_dir(prefix) is None) is (
        paths.client_payload_dir(prefix) is None
    )


def test_ownership_requires_the_marker_not_the_path(tmp_path: Path) -> None:
    """Never infer ownership from location — deleting a prefix is final."""
    prefix = _make_prefix(tmp_path)
    assert paths.is_ours(prefix) is False
    (prefix / paths.PREFIX_MARKER).write_text("")
    assert paths.is_ours(prefix) is True


def test_auth_and_template_prefixes_cannot_collide_with_a_game_uid(tmp_path: Path) -> None:
    """Both are dot-prefixed; a uid never is."""
    assert paths.auth_prefix(tmp_path).name.startswith(".")
    assert paths.template_prefix(tmp_path).name.startswith(".")
    assert paths.game_prefix(tmp_path, "wow").name == "wow"


def test_client_version_dirs_sorted_oldest_to_newest(tmp_path: Path) -> None:
    """Self-update writes a new sibling; repair removes the newest."""
    prefix = _make_prefix(tmp_path, payload=False)
    parent = paths.client_dir(prefix)
    for build in ("17554", "17651", "9000"):
        (parent / f"Battle.net.{build}").mkdir()
    names = [p.name for p in paths.client_version_dirs(prefix)]
    assert names == ["Battle.net.9000", "Battle.net.17554", "Battle.net.17651"]


# --------------------------------------------------------------------------
# id map
# --------------------------------------------------------------------------


def test_roundtrip_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    idmap = BattlenetIdMap(path)
    idmap.merge("hs_beta", family="WTCG", prefix_path="/games/hs")
    reloaded = BattlenetIdMap(path)
    record = reloaded.get("hs_beta")
    assert record == GameRecord(uid="hs_beta", family="WTCG", prefix_path="/games/hs")


def test_prefix_path_is_recorded_not_reconstructed(tmp_path: Path) -> None:
    """An SD-card install must resolve to where it really is."""
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.merge("wow", prefix_path="/run/media/sdcard/prefixes/wow")
    assert idmap.resolve_prefix("wow") == Path("/run/media/sdcard/prefixes/wow")
    assert idmap.resolve_prefix("unknown") is None


def test_clear_prefix_forgets_only_the_location(tmp_path: Path) -> None:
    """A reclaimed prefix must not take the family code with it.

    ``merge`` cannot express this — it drops ``None`` so a partial update
    never wipes a field it wasn't given — and ``forget`` would lose the
    family code and launch history that are still true.
    """
    path = tmp_path / "map.json"
    idmap = BattlenetIdMap(path)
    idmap.merge("wow", family="WoW", prefix_path="/p/wow", launch_ok_at=5.0)

    idmap.clear_prefix("wow")

    assert idmap.resolve_prefix("wow") is None
    assert idmap.resolve_family("wow") == "WoW"
    assert BattlenetIdMap(path).get("wow").launch_ok_at == 5.0


def test_clear_prefix_on_an_unknown_game_is_a_no_op(tmp_path: Path) -> None:
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.clear_prefix("never-seen")
    assert idmap.get("never-seen") is None


def test_merge_preserves_unrelated_fields(tmp_path: Path) -> None:
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.merge("wow", family="WoW", prefix_path="/p/wow")
    idmap.merge("wow", install_path="C:/Games/WoW")
    record = idmap.get("wow")
    assert record.family == "WoW"
    assert record.prefix_path == "/p/wow"
    assert record.install_path == "C:/Games/WoW"


def test_launch_proven_only_after_a_real_launch(tmp_path: Path) -> None:
    """An obsolete family fails silently, so a proven one is never re-guessed."""
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.merge("fenris", family="Fen")
    assert idmap.get("fenris").launch_proven is False
    idmap.mark_launch_ok("fenris", "Fen", when=1_700_000_000.0)
    record = idmap.get("fenris")
    assert record.launch_proven is True
    assert record.last_launch_family == "Fen"


def test_all_prefix_paths_skips_records_without_one(tmp_path: Path) -> None:
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.merge("a", prefix_path="/p/a")
    idmap.merge("b", family="B")
    assert idmap.all_prefix_paths() == [Path("/p/a")]


def test_forget_removes_the_record(tmp_path: Path) -> None:
    idmap = BattlenetIdMap(tmp_path / "map.json")
    idmap.merge("a", prefix_path="/p/a")
    idmap.forget("a")
    assert idmap.get("a") is None
    assert BattlenetIdMap(tmp_path / "map.json").get("a") is None


def test_corrupt_map_degrades_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text("{not json")
    idmap = BattlenetIdMap(path)
    assert idmap.all_records() == {}
    idmap.merge("a", family="A")
    assert BattlenetIdMap(path).get("a").family == "A"


def test_unknown_keys_in_a_stored_record_are_ignored(tmp_path: Path) -> None:
    """A newer plugin version's fields must not break an older reader."""
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"a": {"family": "A", "field_from_the_future": 1}}))
    assert BattlenetIdMap(path).get("a").family == "A"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert BattlenetIdMap(tmp_path / "absent.json").all_records() == {}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_defaults_apply_when_nothing_is_configured() -> None:
    conf = cfg.from_mapping(None)
    assert conf.prefixes_dir.endswith("prefixes/battlenet")
    assert conf.client_ready_timeout_seconds == 300
    assert conf.harvest_client_cookies is False


def test_field_specs_and_dataclass_defaults_agree() -> None:
    """The Ubisoft scar: the applied value is the _FIELD_SPECS one."""
    for name, expected in cfg.FIELD_DEFAULTS.items():
        assert cfg.DATACLASS_DEFAULTS[name] == expected, name


@pytest.mark.parametrize(
    ("raw", "attr", "expected"),
    [
        pytest.param({"client_ready_timeout_seconds": "45"}, "client_ready_timeout_seconds", 45, id="int-from-str"),
        pytest.param({"client_ready_timeout_seconds": "abc"}, "client_ready_timeout_seconds", 300, id="bad-int-falls-back"),
        pytest.param({"harvest_client_cookies": "true"}, "harvest_client_cookies", True, id="bool-from-str"),
        pytest.param({"harvest_client_cookies": None}, "harvest_client_cookies", False, id="null-falls-back"),
        pytest.param({"data_dir": ""}, "data_dir", "~/.local/share/unifideck", id="empty-str-falls-back"),
        pytest.param({"unknown_key": 1}, "data_dir", "~/.local/share/unifideck", id="unknown-key-ignored"),
    ],
)
def test_coercion(raw: dict, attr: str, expected: object) -> None:
    assert getattr(cfg.from_mapping(raw), attr) == expected


def test_paths_expand_the_home_marker() -> None:
    conf = cfg.from_mapping({"data_dir": "~/x"})
    assert not str(conf.data_dir_path).startswith("~")
    assert conf.id_map_path.name == "battlenet_id_map.json"


def test_config_manager_failures_never_break_construction() -> None:
    class Exploding:
        def get(self, *_args: object) -> object:
            raise RuntimeError("config unavailable")

    assert cfg.from_config_manager(Exploding()) == cfg.BattlenetConfig()
    assert cfg.from_config_manager(None) == cfg.BattlenetConfig()
    assert cfg.from_config_manager(object()) == cfg.BattlenetConfig()


def test_schema_and_defaults_declare_the_store() -> None:
    """stores has additionalProperties:false — a missing $defs fails boot."""
    root = Path(__file__).parent.parent.parent
    schema = json.loads((root / "py_modules/unifideck/config/schema.json").read_text())
    defaults = json.loads((root / "defaults/config.json").read_text())
    assert "battlenetStore" in schema["$defs"]
    assert "battlenet" in schema["properties"]["stores"]["properties"]
    assert set(defaults["stores"]["battlenet"]) <= set(
        schema["$defs"]["battlenetStore"]["properties"]
    )
    # Deliberately absent: adding it would reject pre-existing user configs.
    assert "battlenet" not in schema["properties"]["stores"]["required"]
