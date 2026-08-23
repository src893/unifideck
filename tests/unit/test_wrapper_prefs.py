"""Carrying a wrapper store's launcher settings between prefixes.

Every game gets its own Wine prefix, so the vendor client's settings file gets
copied along with it and then diverges. The reported bug: the Battle.net
launcher's language "reverts to the default every time you launch a game". Not a
clobber - nothing was overwriting the choice, nothing was carrying it either.

The asymmetry these tests guard is the same one ``test_wrapper_session`` names,
one layer down. Failing to carry a setting costs the user a dropdown they have
to set again. Carrying *too much* writes one prefix's install path or one game's
``LastPlayed`` into every other prefix, and the file this all lives in mixes
both kinds of key freely. So the interesting assertions here are the negative
ones.

The fixture below is the real file, measured on this Deck (client build 17651)
rather than invented - including the install-hash section name, which is where
the language actually lives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher import wrapper_locale as wl
from unifideck.launcher import wrapper_prefs as wp
from unifideck.launcher import wrapper_session as ws

from _wine_session import CONFIG, make_session, write_file

SPEC = ws.SPECS["battlenet"]


@pytest.fixture(autouse=True)
def _no_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the language seed for every test in this file.

    ``inject`` seeds the client's UI language before it merges, so a test that
    left the resolver alone would take the *test machine's* language and write
    it over the fixture — which is how ``test_inject_carries_settings_even_
    when_the_session_is_skipped`` started failing on a German Deck and passing
    everywhere else. The seed has its own file; here it must not participate.
    """
    monkeypatch.setattr(wl, "_RESOLVE_ATTEMPTED", True)
    monkeypatch.setattr(wl, "_RESOLVED_LOCALE", None)

# The client keys its per-installation settings by a hash of the install path.
# Identical in every one of our prefixes because ``INSTALLER_ARGS`` pins that
# path, which is what makes a merge across prefixes meaningful at all.
HASH = "5a61123b37cafce1"


def _config(**overrides: Any) -> dict[str, Any]:
    """The measured ``Battle.net.config``, with overrides merged in shallowly."""
    config: dict[str, Any] = {
        "Client": {
            "Version": {"FirstRun": "false", "LastBuildVersion": "17651"},
            "Toasts": {"ScreenPosition": "BottomRight", "Monitor": "0"},
            "GaClientId": "GUID-A",
            "AutoStartMinimized": "true",
            "AutoLogin": "true",
            "SavedAccountNames": "player@example.com",
        },
        HASH: {
            "Client": {"Language": "enUS", "LoginSettings": {"AllowedRegions": ""}},
            "Path": "C:\\Program Files (x86)\\Battle.net",
            "Services": {"LastLoginRegion": "US"},
        },
        "Games": {"battle_net": {"ServerUid": "battle.net"}},
    }
    config.update(overrides)
    return config


def _prefix(
    root: Path, name: str, config: dict[str, Any] | None, *, mtime: float,
) -> Path:
    """A prefix holding ``config`` at a pinned mtime. ``None`` writes no file."""
    prefix = root / name
    (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
    if config is not None:
        write_file(prefix, CONFIG, json.dumps(config).encode(), mtime=mtime)
    return prefix


def _read(prefix: Path) -> dict[str, Any]:
    return json.loads((prefix / "drive_c" / CONFIG).read_text())


# --------------------------------------------------------------------------
# what travels
# --------------------------------------------------------------------------


def test_the_launcher_language_travels(tmp_path: Path) -> None:
    """The reported bug, in one assertion.

    Note where it lives: ``<install-hash>.Client.Language``, not
    ``Client.Language``. A merge that only understood the top-level ``Client``
    section would pass every other test here and fix nothing.
    """
    source = _prefix(tmp_path, "auth", _config(**{
        HASH: {"Client": {"Language": "deDE"}},
    }), mtime=2000.0)
    target = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, source, target) is True
    assert _read(target)[HASH]["Client"]["Language"] == "deDE"


def test_settings_the_source_does_not_mention_are_left_alone(tmp_path: Path) -> None:
    """Additive, like the session copy. The source is newer, not complete."""
    source = _prefix(tmp_path, "auth", _config(**{
        HASH: {"Client": {"Language": "frFR"}},
    }), mtime=2000.0)
    target = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, source, target) is True
    kept = _read(target)
    assert kept[HASH]["Client"]["Language"] == "frFR"
    # Present only in the target: the source's HASH section replaced nothing.
    assert kept[HASH]["Client"]["LoginSettings"] == {"AllowedRegions": ""}
    assert kept[HASH]["Path"] == "C:\\Program Files (x86)\\Battle.net"


def test_the_hardware_acceleration_tweak_travels(tmp_path: Path) -> None:
    """It is a setting, not per-prefix state.

    ``prefix/tweaks.py`` writes it into a prefix that has none, and it must be
    free to spread from there: the client only records a setting once it differs
    from default, which is why an allowlist of setting names cannot work.
    """
    config = _config()
    config["Client"]["HardwareAcceleration"] = "false"
    source = _prefix(tmp_path, "auth", config, mtime=2000.0)
    target = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, source, target) is True
    assert _read(target)["Client"]["HardwareAcceleration"] == "false"


# --------------------------------------------------------------------------
# what must not travel
# --------------------------------------------------------------------------


def test_the_per_prefix_install_path_stays_put(tmp_path: Path) -> None:
    """A game prefix may sit on storage the other prefixes cannot see."""
    source = _config()
    source["Client"]["Install"] = {"DefaultInstallPath": "C:/Program Files (x86)"}
    target = _config()
    target["Client"]["Install"] = {"DefaultInstallPath": "D:/Games"}

    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", target, mtime=1000.0)

    wp.merge(SPEC, src, dst)
    assert _read(dst)["Client"]["Install"]["DefaultInstallPath"] == "D:/Games"


def test_per_game_state_stays_put(tmp_path: Path) -> None:
    """``Games`` covers its whole subtree without naming a game."""
    source = _config()
    source["Games"] = {"d1": {"LastPlayed": "111", "Resumable": "false"}}
    target = _config()
    target["Games"] = {"d1": {"LastPlayed": "999"}}

    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", target, mtime=1000.0)

    wp.merge(SPEC, src, dst)
    games = _read(dst)["Games"]
    assert games["d1"]["LastPlayed"] == "999"
    assert "Resumable" not in games["d1"]


def test_the_identity_is_never_written(tmp_path: Path) -> None:
    """It is what a copy is *checked against*.

    Carrying ``GaClientId`` would mean the identity guard verifying prefixes
    against a value this pass had itself installed, which is no guard at all.
    """
    source = _config()
    source["Client"]["GaClientId"] = "GUID-SOURCE"
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    wp.merge(SPEC, src, dst)
    assert _read(dst)["Client"]["GaClientId"] == "GUID-A"


def test_sign_in_state_stays_put(tmp_path: Path) -> None:
    """Otherwise one prefix's sign-out travels to every other prefix."""
    source = _config()
    del source["Client"]["SavedAccountNames"]
    source["Client"]["AutoLogin"] = "false"
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    wp.merge(SPEC, src, dst)
    client = _read(dst)["Client"]
    assert client["AutoLogin"] == "true"
    assert client["SavedAccountNames"] == "player@example.com"


def test_client_build_state_stays_put(tmp_path: Path) -> None:
    """The client self-updates per prefix, so the build is a per-prefix fact."""
    source = _config()
    source["Client"]["Version"]["LastBuildVersion"] = "99999"
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    wp.merge(SPEC, src, dst)
    assert _read(dst)["Client"]["Version"]["LastBuildVersion"] == "17651"


def test_the_install_sections_own_keys_stay_put(tmp_path: Path) -> None:
    """``*.Path`` and ``*.Services`` reach into a section named at runtime."""
    source = _config()
    source[HASH]["Path"] = "C:\\Elsewhere\\Battle.net"
    source[HASH]["Services"] = {"LastLoginRegion": "EU"}
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    wp.merge(SPEC, src, dst)
    section = _read(dst)[HASH]
    assert section["Path"] == "C:\\Program Files (x86)\\Battle.net"
    assert section["Services"] == {"LastLoginRegion": "US"}


def test_a_wildcard_matches_exactly_one_section(tmp_path: Path) -> None:
    """``*.Path`` must not swallow a ``Path`` nested deeper.

    The pattern is two segments, so it matches ``<hash>.Path`` and stops. A
    prefix-match over any depth would quietly exclude settings nobody listed.
    """
    source = _config()
    source[HASH]["Client"]["Path"] = "kept"
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, src, dst) is True
    assert _read(dst)[HASH]["Client"]["Path"] == "kept"


def test_a_fully_excluded_section_is_not_created(tmp_path: Path) -> None:
    """A prefix that had no ``Games`` must not gain an empty one."""
    source = _config()
    source["Games"] = {"d1": {"LastPlayed": "111"}}
    target = _config()
    del target["Games"]
    src = _prefix(tmp_path, "auth", source, mtime=2000.0)
    dst = _prefix(tmp_path, "game", target, mtime=1000.0)

    wp.merge(SPEC, src, dst)
    assert "Games" not in _read(dst)


# --------------------------------------------------------------------------
# when it refuses
# --------------------------------------------------------------------------


def test_a_busy_target_is_refused(tmp_path: Path) -> None:
    """The client rewrites this file from memory when it exits.

    Writing underneath a live one is discarded with every log line still saying
    success, which is the failure mode ``wine_registry.registry_is_writable``
    exists for. Same hazard, same answer.
    """
    src = _prefix(tmp_path, "auth", _config(**{
        HASH: {"Client": {"Language": "deDE"}},
    }), mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, src, dst, target_busy=True) is False
    assert _read(dst)[HASH]["Client"]["Language"] == "enUS"


def test_an_older_source_is_refused(tmp_path: Path) -> None:
    """The guard that stops a missed capture from reverting a local change.

    The launcher can be SIGKILLed (the Steam stop button takes that path), so
    the capture leg is best-effort. Without this, the next launch would push
    auth's stale settings back over the change the user just made.
    """
    src = _prefix(tmp_path, "auth", _config(), mtime=1000.0)
    dst = _prefix(tmp_path, "game", _config(**{
        HASH: {"Client": {"Language": "deDE"}},
    }), mtime=2000.0)

    assert wp.merge(SPEC, src, dst) is False
    assert _read(dst)[HASH]["Client"]["Language"] == "deDE"


def test_an_equal_mtime_is_refused(tmp_path: Path) -> None:
    """Strictly newer, so a repeated pass over an unchanged pair is a no-op."""
    src = _prefix(tmp_path, "auth", _config(), mtime=1000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, src, dst) is False


def test_merging_a_prefix_into_itself_is_a_noop(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, "auth", _config(), mtime=1000.0)
    assert wp.merge(SPEC, prefix, prefix) is False


def test_a_target_without_a_config_takes_a_filtered_copy(tmp_path: Path) -> None:
    """A prefix Wine has initialised but the client has never run in.

    The excluded keys are exactly the ones the client regenerates for itself,
    so seeding the rest is right rather than merely harmless.
    """
    src = _prefix(tmp_path, "auth", _config(), mtime=2000.0)
    dst = _prefix(tmp_path, "game", None, mtime=0.0)

    assert wp.merge(SPEC, src, dst) is True
    written = _read(dst)
    assert written[HASH]["Client"]["Language"] == "enUS"
    assert "GaClientId" not in written["Client"]
    assert "Games" not in written


def test_a_truncated_source_is_a_noop(tmp_path: Path) -> None:
    """A client killed mid-write must not blank the destination's settings."""
    src = _prefix(tmp_path, "auth", None, mtime=0.0)
    write_file(src, CONFIG, b"", mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, src, dst) is False
    assert _read(dst)[HASH]["Client"]["Language"] == "enUS"


def test_a_source_holding_a_json_list_is_a_noop(tmp_path: Path) -> None:
    src = _prefix(tmp_path, "auth", None, mtime=0.0)
    write_file(src, CONFIG, b'["not", "a", "config"]', mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(SPEC, src, dst) is False


def test_a_prefix_wine_has_never_initialised_is_a_noop(tmp_path: Path) -> None:
    """No ``drive_c`` means no path to write to, not an exception."""
    src = _prefix(tmp_path, "auth", _config(), mtime=2000.0)
    assert wp.merge(SPEC, src, tmp_path / "nothing-here") is False


def test_a_store_without_a_prefs_row_never_writes(tmp_path: Path) -> None:
    """Opting in is a row in the spec; absence must be inert, not a crash."""
    bare = ws.SessionSpec(store="nowhere", files=())
    src = _prefix(tmp_path, "auth", _config(), mtime=2000.0)
    dst = _prefix(tmp_path, "game", _config(), mtime=1000.0)

    assert wp.merge(bare, src, dst) is False
    assert wp.config_path(bare, src) is None
    assert wp.read_prefs(bare, src) is None


def test_read_prefs_reads_the_measured_file(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, "auth", _config(), mtime=1000.0)
    prefs = wp.read_prefs(SPEC, prefix)
    assert prefs is not None
    assert prefs[HASH]["Client"]["Language"] == "enUS"


# --------------------------------------------------------------------------
# wiring: settings ride along with the session pass
# --------------------------------------------------------------------------


def test_inject_carries_settings_even_when_the_session_is_skipped(
    tmp_path: Path,
) -> None:
    """The reason settings are not gated on the session's ordering rule.

    ``inject`` refuses a session copy when the target already holds one at least
    as new, and a game prefix routinely does: it rotated the token last. That is
    the *normal* path, so gating settings on it would have left the language
    behind in exactly the case the bug was reported for.
    """
    auth = make_session(tmp_path / "auth", mtime=1000.0)
    game = make_session(tmp_path / "game", mtime=5000.0)
    write_file(auth, CONFIG, json.dumps(_config(**{
        HASH: {"Client": {"Language": "deDE"}},
    })).encode(), mtime=9000.0)
    write_file(game, CONFIG, json.dumps(_config()).encode(), mtime=1000.0)

    # The session itself is not copied: the target's is newer.
    assert ws.inject(SPEC, auth, game) is False
    assert _read(game)[HASH]["Client"]["Language"] == "deDE"


def test_capture_carries_settings_back_to_auth(tmp_path: Path) -> None:
    """The leg that makes a change reach the *other* games.

    A setting changed inside one game's client lands in auth here, and every
    later launch injects from auth.
    """
    auth = make_session(tmp_path / "auth", mtime=1000.0)
    game = make_session(tmp_path / "game", mtime=5000.0)
    write_file(auth, CONFIG, json.dumps(_config()).encode(), mtime=1000.0)
    write_file(game, CONFIG, json.dumps(_config(**{
        HASH: {"Client": {"Language": "esES"}},
    })).encode(), mtime=9000.0)

    assert ws.capture(SPEC, game, auth) is True
    assert _read(auth)[HASH]["Client"]["Language"] == "esES"


def test_capture_from_a_signed_out_prefix_still_carries_settings(
    tmp_path: Path,
) -> None:
    """Deliberate: sign-in state is excluded, so there is nothing unsafe left.

    A prefix whose client signed out has no session to give and must never
    overwrite auth's - but the language the user set in it is still the most
    recent thing anyone knows about the language.
    """
    auth = make_session(tmp_path / "auth", mtime=1000.0)
    game = make_session(tmp_path / "game", mtime=5000.0, registry=False)
    write_file(auth, CONFIG, json.dumps(_config()).encode(), mtime=1000.0)
    write_file(game, CONFIG, json.dumps(_config(**{
        HASH: {"Client": {"Language": "ptBR"}},
    })).encode(), mtime=9000.0)

    assert ws.capture(SPEC, game, auth) is False
    assert _read(auth)[HASH]["Client"]["Language"] == "ptBR"


def test_an_identity_mismatch_blocks_the_settings_too(tmp_path: Path) -> None:
    """A prefix that is not one of ours has nothing to tell us about settings."""
    auth = make_session(tmp_path / "auth", mtime=1000.0)
    game = make_session(tmp_path / "game", mtime=5000.0)
    stranger = _config()
    stranger["Client"]["GaClientId"] = "GUID-STRANGER"
    stranger[HASH]["Client"]["Language"] = "koKR"
    write_file(auth, CONFIG, json.dumps(_config()).encode(), mtime=1000.0)
    write_file(game, CONFIG, json.dumps(stranger).encode(), mtime=9000.0)

    assert ws.capture(SPEC, game, auth) is False
    assert _read(auth)[HASH]["Client"]["Language"] == "enUS"


@pytest.mark.parametrize(
    ("trail", "expected"),
    [
        (("Games",), True),
        (("Games", "d1", "LastPlayed"), True),
        (("Client", "GaClientId"), True),
        (("Client",), False),
        ((HASH, "Path"), True),
        ((HASH, "Client", "Language"), False),
        ((HASH, "Client", "Path"), False),
    ],
)
def test_exclusion_covers_a_subtree_but_not_a_parent(
    trail: tuple[str, ...], expected: bool,
) -> None:
    """A pattern matches itself and everything under it, never above it."""
    prefs = SPEC.prefs
    assert prefs is not None
    assert wp._excluded(trail, prefs.exclude) is expected
