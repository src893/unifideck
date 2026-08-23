"""Seeding the vendor client's UI language from the plugin's own.

The reported bug, and the reason this file is separate from
``test_wrapper_prefs``: a Deck with the plugin set to German ran Battle.net,
its games and its installs entirely in English. Nothing was overwriting the
choice — the seed had simply retired itself before the choice was made.

Measured on-device 2026-08-22/23, and the timestamps are the whole story. The
old empty marker was stamped at 23:40:52.989, in the same millisecond the
launcher resolved ``en-US``; the user picked German at 23:54:37, fourteen
minutes later. From then on ``ensure_locale_seeded`` returned on its first
line, so the launcher never even resolved a locale again — the log has no
``[locale] resolved`` line at all, which made the bug look like the resolver.

So the assertions that matter here are about *when the seed runs a second
time*. A marker that records only "seeded" cannot tell "seeded German" from
"seeded English", and that ambiguity was the defect. The marker now holds the
tag, and the tests below pin both directions: a changed plugin locale reaches
the client, and an unchanged one does not touch it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher import wrapper_locale as wl
from unifideck.launcher import wrapper_session as ws

from _wine_session import CONFIG, make_session, write_file

SPEC = ws.SPECS["battlenet"]

# The client keys its per-installation settings by a hash of the install path.
HASH = "5a61123b37cafce1"

MARKER = ".unifideck_battlenet_locale.v2"
LEGACY_MARKER = ".unifideck_battlenet_locale_seeded.v1"


def _config(language: str | None = "enUS") -> dict[str, Any]:
    """The measured ``Battle.net.config``. ``None`` omits the language key."""
    client: dict[str, Any] = {"LoginSettings": {"AllowedRegions": ""}}
    if language is not None:
        client["Language"] = language
    return {
        "Client": {"GaClientId": "GUID-A", "AutoLogin": "true"},
        HASH: {
            "Client": client,
            "Path": "C:\\Program Files (x86)\\Battle.net",
            "Services": {"LastLoginRegion": "US"},
        },
        "Games": {"battle_net": {"ServerUid": "battle.net"}},
    }


def _prefix(
    root: Path, name: str, config: dict[str, Any] | None, *, mtime: float = 1000.0,
) -> Path:
    prefix = root / name
    (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
    if config is not None:
        write_file(prefix, CONFIG, json.dumps(config).encode(), mtime=mtime)
    return prefix


def _language(prefix: Path) -> str | None:
    config = json.loads((prefix / "drive_c" / CONFIG).read_text())
    return config[HASH]["Client"].get("Language")


def _marker(prefix: Path) -> str | None:
    path = prefix / MARKER
    return path.read_text(encoding="utf-8") if path.exists() else None


def _resolved_locale(monkeypatch: pytest.MonkeyPatch, tag: str | None) -> None:
    """Pin what the global resolver answers.

    Stated rather than inferred: two of the predecessor tests depended on the
    test machine itself being en-US, and passed for that reason alone.
    """
    monkeypatch.setattr(wl, "_RESOLVE_ATTEMPTED", True)
    monkeypatch.setattr(wl, "_RESOLVED_LOCALE", tag)


# --------------------------------------------------------------------------
# the seed itself
# --------------------------------------------------------------------------


def test_the_plugin_language_is_written_into_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A German plugin locale gives a German client."""
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert _language(auth) == "deDE"
    assert _marker(auth) == "de-DE"


def test_an_unchanged_locale_does_not_rewrite_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every launch after the first is a marker read and nothing else."""
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert wl.ensure_locale_seeded(SPEC, auth) is False
    assert _language(auth) == "deDE"


def test_changing_the_plugin_language_reseeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported bug, stated as a test.

    The old marker was a bit, so the second call here did nothing and the
    client stayed at the language resolved on the very first launch.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())
    assert wl.ensure_locale_seeded(SPEC, auth) is True

    _resolved_locale(monkeypatch, "fr-FR")
    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert _language(auth) == "frFR"
    assert _marker(auth) == "fr-FR"


def test_english_is_seeded_like_any_other_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching *back* to English has to work too.

    ``enUS`` used to be special-cased as "the client default, nothing to do",
    which meant a user who moved from German back to English kept a German
    client.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())
    wl.ensure_locale_seeded(SPEC, auth)

    _resolved_locale(monkeypatch, "en-US")
    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert _language(auth) == "enUS"


def test_a_missing_language_key_is_written_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that never carried the key was never seeded.

    ``config.get("Language")`` is ``None`` there, and ``None != "enUS"`` took
    the "the user set this themselves, leave it" branch — so the one prefix
    that most needed a language got none.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config(language=None))

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert _language(auth) == "deDE"


def test_a_language_already_correct_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No write, but the marker is recorded so the next launch is a read."""
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config(language="deDE"))

    assert wl.ensure_locale_seeded(SPEC, auth) is False
    assert _marker(auth) == "de-DE"


def test_a_language_picked_in_the_client_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the one-shot marker was protecting, kept.

    Once a tag has been seeded, a change made inside the vendor client is not
    reverted on the next launch — only a change to the *plugin's* language
    moves it again.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())
    wl.ensure_locale_seeded(SPEC, auth)

    # The user picks French in the client; a capture carries it back to auth.
    config = _config(language="frFR")
    (auth / "drive_c" / CONFIG).write_text(json.dumps(config))

    assert wl.ensure_locale_seeded(SPEC, auth) is False
    assert _language(auth) == "frFR"


# --------------------------------------------------------------------------
# what the client cannot serve
# --------------------------------------------------------------------------


def test_a_locale_the_client_does_not_ship_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nl-NL`` is a plugin language and not a Battle.net one.

    Read out of ``battle.net.dll`` on 2026-08-23: the client ships 22 locales
    and Dutch is not among them. Approximating is worse than English.
    """
    _resolved_locale(monkeypatch, "nl-NL")
    auth = _prefix(tmp_path, "auth", _config())

    assert wl.ensure_locale_seeded(SPEC, auth) is False
    assert _language(auth) == "enUS"
    # Recorded anyway, so this costs one read rather than a config parse.
    assert _marker(auth) == "nl-NL"


@pytest.mark.parametrize(("tag", "code"), [("ar-SA", "arSA"), ("tr-TR", "trTR")])
def test_locales_the_client_does_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str, code: str,
) -> None:
    """Both were missing from the map and fell back to English for no reason.

    ``strings battle.net.dll`` lists ``arSA`` and ``trTR`` among the 22 the
    client's own log reports loading from ``languages.xml``.
    """
    _resolved_locale(monkeypatch, tag)
    auth = _prefix(tmp_path, "auth", _config())

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert _language(auth) == code


def test_nothing_is_recorded_when_no_locale_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure to resolve must not be recorded as a decision."""
    _resolved_locale(monkeypatch, None)
    auth = _prefix(tmp_path, "auth", _config())

    assert wl.ensure_locale_seeded(SPEC, auth) is False
    assert _marker(auth) is None


# --------------------------------------------------------------------------
# the v1 marker
# --------------------------------------------------------------------------


def test_a_v1_marker_is_retired_and_reseeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-heal for every Deck already carrying the old marker.

    Its file is empty, so there is no way to know which language it stands
    for. Retiring it buys one corrective seed from the current plugin locale,
    which is what unsticks the reported device.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())
    (auth / LEGACY_MARKER).write_text("")

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert not (auth / LEGACY_MARKER).exists()
    assert _language(auth) == "deDE"
    assert _marker(auth) == "de-DE"


def test_the_v1_marker_is_retired_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", _config())
    (auth / LEGACY_MARKER).write_text("")

    assert wl.ensure_locale_seeded(SPEC, auth) is True
    assert wl.ensure_locale_seeded(SPEC, auth) is False


# --------------------------------------------------------------------------
# prefixes with nothing to seed
# --------------------------------------------------------------------------


def test_a_prefix_wine_has_never_initialised_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next launch will have a config; this one is not a failure."""
    _resolved_locale(monkeypatch, "de-DE")
    assert wl.ensure_locale_seeded(SPEC, tmp_path / "fresh") is False


def test_a_config_without_an_install_section_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The section name is a hash and is discovered, never assumed."""
    _resolved_locale(monkeypatch, "de-DE")
    auth = _prefix(tmp_path, "auth", {"Client": {}, "Games": {}})

    assert wl.ensure_locale_seeded(SPEC, auth) is False


def test_a_store_without_a_prefs_row_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved_locale(monkeypatch, "de-DE")
    bore = ws.SessionSpec(store="bore", files=())
    assert wl.ensure_locale_seeded(bore, tmp_path) is False


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_the_seed_runs_during_inject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing part: seed on auth, then let the merge carry it.

    This is the propagation measured on-device — editing auth alone reached
    the template and then a game prefix — reproduced in the small.
    """
    _resolved_locale(monkeypatch, "de-DE")
    auth = make_session(tmp_path / "auth", mtime=1000.0)
    game = make_session(tmp_path / "game", mtime=500.0)
    write_file(auth, CONFIG, json.dumps(_config()).encode(), mtime=1000.0)
    write_file(game, CONFIG, json.dumps(_config()).encode(), mtime=500.0)

    assert ws.inject(SPEC, auth, game) is True
    assert _language(auth) == "deDE"
    assert _language(game) == "deDE"


# --------------------------------------------------------------------------
# the locale comes from the global resolver
# --------------------------------------------------------------------------


def _launcher_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **ui: str,
) -> None:
    """Point the launcher's standalone config loader at a throwaway file."""
    from unifideck.config.config_manager import ConfigManager

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ui": ui}), encoding="utf-8")

    monkeypatch.setattr(
        "unifideck.launcher.bootstrap._load_standalone_config",
        lambda: ConfigManager(defaults_path=str(path), user_path=str(path)),
    )
    monkeypatch.setattr(wl, "_RESOLVE_ATTEMPTED", False)
    monkeypatch.setattr(wl, "_RESOLVED_LOCALE", None)


def test_the_locale_comes_from_the_global_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One source of truth, and no file for it to go missing from.

    The locale used to be copied into ``wrapper_prefixes.json`` and read back.
    On 2026-08-22 that file was absent on a working install, so every caller
    got ``None``: the seed never ran and the bootstrapper fell back to English.
    """
    _launcher_config(tmp_path, monkeypatch, locale="de-DE")

    assert wl.plugin_locale() == "de-DE"
    assert wl.bootstrapper_locale("battlenet") == "deDE"


def test_nothing_anywhere_still_yields_a_usable_installer_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad value here stalls the installer on its language screen."""

    def _boom() -> None:
        raise OSError("config unreadable")

    monkeypatch.setattr(
        "unifideck.launcher.bootstrap._load_standalone_config", _boom,
    )
    monkeypatch.setattr(wl, "_RESOLVE_ATTEMPTED", False)
    monkeypatch.setattr(wl, "_RESOLVED_LOCALE", None)

    assert wl.plugin_locale() is None
    assert wl.bootstrapper_locale("battlenet") == "enUS"
