"""UD-101 / UD-041: Epic games must launch in the user's language.

``-epiclocale=<code>`` is how the Epic Games Launcher tells a game which
language to run in; legendary reproduces it in the ``egl_parameters`` our
launcher forwards. The launcher used to build it from a **hardcoded**
``"en"``::

    "--language", os.environ.get("EPIC_LANG", "en")

and nothing anywhere ever set ``EPIC_LANG``, so every Epic game launched
in English regardless of the user's Unifideck language. Reported as
"selected language not applied to Epic games" (Darksiders 2 forced to
English despite choosing French) and "games without in-game language
options start in English".

It also made the Selective Downloads work look broken: a title installed
with the Italian pack had the Italian audio on disk and still came up in
English, because nothing told it to use Italian.

Two layers, matching legendary's own precedence
(``config.get(app_name, 'language', fallback=<--language arg>)``):

1. a per-game choice recorded at install time wins
   (:func:`write_app_language`), so the language follows the pack that
   was actually downloaded;
2. otherwise the launcher passes the user's global Unifideck language.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.compat import epic as compat_epic
from unifideck.launcher.proton.handlers.epic import (
    _build_legendary_argv,
    _resolve_epic_language,
)
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.stores.epic.legendary import legendary_config_dir, write_app_language


@pytest.fixture(autouse=True)
def _isolate_legendary_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Never touch the developer's real legendary config."""
    monkeypatch.setenv("LEGENDARY_CONFIG_DIR", str(tmp_path / "legendary"))


def _plan(plugin_dir: Path | None = None) -> ProtonLaunchPlan:
    """A minimal launch plan (same shape as test_epic_wrapper_env)."""
    return ProtonLaunchPlan(
        context=types.SimpleNamespace(
            game_id="abc123", store="epic",
            exe_path=Path("/install/abc123.exe"),
            work_dir=Path("/install"),
            plugin_dir=plugin_dir or Path("/plugin"),
        ),
        state=types.SimpleNamespace(wrappers=[], game_args=[], umu_id=None),
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=Path("/plugin/bin/umu/umu/umu-run"),
        prefix_path=Path("/tmp/prefix"),  # noqa: S108
        env={},
        on_process_start=None,
    )


def _patch_resolved_language(
    monkeypatch: pytest.MonkeyPatch, locale_tag: str,
) -> None:
    """Make the launcher's language lookup return ``locale_tag``."""
    import unifideck.launcher.proton.language_setup as ls
    monkeypatch.setattr(
        ls, "get_unifideck_language", lambda _config=None: locale_tag,
    )
    # The real ConfigManager wants a config.json on disk; the value under
    # test is what get_unifideck_language returns, not how it's loaded.
    import unifideck.config.config_manager as cm
    monkeypatch.setattr(cm, "ConfigManager", lambda *_a, **_kw: object())


# --------------------------------------------------------------------------
# _resolve_epic_language
# --------------------------------------------------------------------------
def test_uses_the_users_language_not_hardcoded_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EPIC_LANG", raising=False)
    _patch_resolved_language(monkeypatch, "it-IT")
    # The whole bug: this used to be "en" no matter what.
    assert _resolve_epic_language(_plan()) == "it"


def test_locale_is_reduced_to_legendarys_two_letter_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EPIC_LANG", raising=False)
    for locale_tag, expected in (
        ("fr-FR", "fr"), ("pt-BR", "pt"), ("zh-Hans", "zh"), ("de", "de"),
    ):
        _patch_resolved_language(monkeypatch, locale_tag)
        assert _resolve_epic_language(_plan()) == expected


def test_epic_lang_env_is_still_an_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EPIC_LANG", "ja")
    _patch_resolved_language(monkeypatch, "it-IT")
    assert _resolve_epic_language(_plan()) == "ja"


def test_falls_back_to_english_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EPIC_LANG", raising=False)
    import unifideck.launcher.proton.language_setup as ls

    def boom(_config: Any = None) -> str:
        raise RuntimeError("no config")

    monkeypatch.setattr(ls, "get_unifideck_language", boom)
    # A launch must never be blocked by a language lookup.
    assert _resolve_epic_language(_plan()) == "en"


def test_argv_carries_the_resolved_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EPIC_LANG", raising=False)
    monkeypatch.setattr(compat_epic, "detect_offline", lambda: False)
    _patch_resolved_language(monkeypatch, "fr-FR")

    argv = _build_legendary_argv(_plan(), "/plugin/bin/legendary")

    assert argv[argv.index("--language") + 1] == "fr"


# --------------------------------------------------------------------------
# write_app_language — the per-game record legendary reads first
# --------------------------------------------------------------------------
def _config_text() -> str:
    return (legendary_config_dir() / "config.ini").read_text()


def test_records_the_language_under_the_app_section() -> None:
    write_app_language("fa4240e5", "it-IT")
    text = _config_text()
    assert "[fa4240e5]" in text
    # Normalized to the ISO base code legendary documents for --language.
    assert "language = it" in text


def test_preserves_existing_keys_and_legendary_comments() -> None:
    cfg = legendary_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    original = (
        "[Legendary]\n"
        "; Disables the automatic update check\n"
        "disable_update_check = false\n"
        "\n"
        "[fa4240e5]\n"
        "install_tags = ,it-IT\n"
    )
    (cfg / "config.ini").write_text(original)

    write_app_language("fa4240e5", "it-IT")

    text = _config_text()
    # The install tags legendary needs to know what's on disk must survive…
    assert "install_tags = ,it-IT" in text
    # …and so must legendary's own ';' help comments, which a default
    # ConfigParser would silently strip on rewrite.
    assert "; Disables the automatic update check" in text
    assert "language = it" in text


def test_creates_the_config_when_absent() -> None:
    assert not (legendary_config_dir() / "config.ini").exists()
    write_app_language("fa4240e5", "fr-FR")
    assert "language = fr" in _config_text()


def test_ignores_an_unusable_language() -> None:
    # An unrecognised label must not write a junk locale that would
    # override the user's real language at every launch.
    write_app_language("fa4240e5", "not-a-language")
    write_app_language("fa4240e5", "")
    write_app_language("", "it-IT")
    assert not (legendary_config_dir() / "config.ini").exists()


def test_rewriting_replaces_rather_than_duplicates() -> None:
    write_app_language("fa4240e5", "it-IT")
    write_app_language("fa4240e5", "de-DE")
    text = _config_text()
    assert text.count("language = ") == 1
    assert "language = de" in text


# --------------------------------------------------------------------------
# The language the user PICKED has to reach the game
#
# _resolve_epic_language built ConfigManager with no ``user_path``, so the
# merged view never included the user's own config file: it saw only
# defaults/config.json and resolved from the machine instead. On a Steam Deck
# — LANG=en_US.UTF-8, always — that meant English for everyone.
#
# The tests above stub ConfigManager out entirely, so they cannot see this.
# These drive the real one with the machine pinned to a language the user did
# NOT choose, so a fallback can never masquerade as success.
# --------------------------------------------------------------------------
def _real_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_locale: str | None,
) -> Path:
    """Lay down defaults + user config; return the plugin dir. Pins LANG."""
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    repo_defaults = Path(__file__).resolve().parents[2] / "defaults/config.json"
    # The Decky CLI layout: defaults/config.json flattened to the install
    # root. This is what a plugin installed from a CLI-built zip looks
    # like, and the hardcoded nested path found nothing in it.
    (plugin_dir / "config.json").write_text(
        repo_defaults.read_text(encoding="utf-8"), encoding="utf-8",
    )
    user_cfg = tmp_path / "user_config.json"
    if user_locale is not None:
        user_cfg.write_text(
            json.dumps({"ui": {"locale": user_locale}}), encoding="utf-8",
        )
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))
    monkeypatch.setenv("HOME", str(tmp_path))       # no registry.vdf here
    # Pin what the resolver sees as the machine's locale. Patching
    # ``getlocale`` rather than ``LANG``: Python reads the process locale
    # set at startup, so setting the env mid-test proves nothing.
    import unifideck.utils.locale as locale_module
    monkeypatch.setattr(
        locale_module._locale, "getlocale", lambda: ("en_US", "UTF-8"),
    )
    monkeypatch.delenv("EPIC_LANG", raising=False)
    return plugin_dir


def test_honours_the_language_picked_in_the_unifideck_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = _real_config(tmp_path, monkeypatch, "es-ES")
    assert _resolve_epic_language(_plan(plugin_dir)) == "es"


def test_falls_back_to_the_machine_when_the_user_picked_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = _real_config(tmp_path, monkeypatch, None)
    assert _resolve_epic_language(_plan(plugin_dir)) == "en"
