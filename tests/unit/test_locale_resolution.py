"""Locale resolution: the user's choice, then Steam's language, then POSIX.

The bug these cover: SteamOS ships ``LANG=en_US.UTF-8`` and never updates
it, so with the shipped default (``ui.locale = "auto"``) resolution fell
through to English for every Deck user whose Steam is in another
language — and every store CLI inherited it.

Each test pins the POSIX locale to a language the user did NOT choose,
so a fallback can never masquerade as success. It is pinned by patching
``locale.getlocale`` rather than the ``LANG`` environment variable:
Python reads the process locale set at startup, so changing the env
mid-test would have no effect and the assertion would prove nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.config.config_manager import ConfigManager
from unifideck.utils.locale import get_unifideck_locale
from unifideck.utils.steam_language import (
    detect_steam_locale,
    read_steam_language,
)

_REGISTRY_TEMPLATE = """"Registry"
{{
\t"HKCU"
\t{{
\t\t"Software"
\t\t{{
\t\t\t"Valve"
\t\t\t{{
\t\t\t\t"Steam"
\t\t\t\t{{
\t\t\t\t\t"language"\t\t"{language}"
\t\t\t\t\t"AutoLoginUser"\t\t"someone"
\t\t\t\t}}
\t\t\t}}
\t\t}}
\t}}
}}
"""


@pytest.fixture(autouse=True)
def _english_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A machine in English with no Steam settings, as SteamOS ships."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _set_posix_locale(monkeypatch, "en_US")


def _set_posix_locale(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Pin what ``_detect_system_locale`` sees as the machine's locale."""
    import unifideck.utils.locale as locale_module
    monkeypatch.setattr(
        locale_module._locale, "getlocale", lambda: (value, "UTF-8"),
    )


def _write_registry(tmp_path: Path, language: str) -> None:
    """Lay down a registry.vdf carrying Steam's UI language."""
    registry = tmp_path / ".steam" / "registry.vdf"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        _REGISTRY_TEMPLATE.format(language=language), encoding="utf-8",
    )


def _config(tmp_path: Path, user_locale: str | None) -> ConfigManager:
    """A ConfigManager over the real defaults plus an optional override."""
    defaults = Path(__file__).resolve().parents[2] / "defaults" / "config.json"
    user_path = tmp_path / "user_config.json"
    if user_locale is not None:
        user_path.write_text(
            json.dumps({"ui": {"locale": user_locale}}), encoding="utf-8",
        )
    return ConfigManager(str(defaults), user_path=str(user_path))


# ── read_steam_language ────────────────────────────────────────────────
def test_reads_the_language_key_from_registry_vdf(tmp_path: Path) -> None:
    _write_registry(tmp_path, "spanish")
    assert read_steam_language() == "spanish"


def test_returns_none_when_there_is_no_registry(tmp_path: Path) -> None:
    assert read_steam_language() is None


def test_returns_none_when_the_key_is_empty(tmp_path: Path) -> None:
    _write_registry(tmp_path, "")
    assert read_steam_language() is None


# ── detect_steam_locale ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("steam_language", "expected"),
    [
        ("spanish", "es-ES"),
        ("latam", "es-ES"),      # folded: es-ES is the only Spanish shipped
        ("koreana", "ko-KR"),    # Steam's own spelling
        ("schinese", "zh-CN"),
        ("brazilian", "pt-BR"),
        ("english", "en-US"),
    ],
)
def test_maps_steam_language_codes_to_supported_tags(
    tmp_path: Path, steam_language: str, expected: str,
) -> None:
    _write_registry(tmp_path, steam_language)
    lc = _config(tmp_path, None)
    from unifideck.utils.locale import get_locale_config
    assert detect_steam_locale(get_locale_config(lc)) == expected


def test_unknown_steam_language_falls_through(tmp_path: Path) -> None:
    """Steam ships languages this plugin does not — they must not crash."""
    _write_registry(tmp_path, "vietnamese")
    from unifideck.utils.locale import get_locale_config
    assert detect_steam_locale(get_locale_config(_config(tmp_path, None))) is None


def test_maps_without_a_canonical_list_to_validate_against(
    tmp_path: Path,
) -> None:
    """``lc`` is None on every installed plugin — see the degraded-mode
    block below. The mapping must still apply, unvalidated."""
    _write_registry(tmp_path, "spanish")
    assert detect_steam_locale(None) == "es-ES"


# ── the full chain ─────────────────────────────────────────────────────
def test_steam_language_wins_over_the_posix_locale(tmp_path: Path) -> None:
    """The whole point: LANG says English, Steam says Spanish."""
    _write_registry(tmp_path, "spanish")
    assert get_unifideck_locale(_config(tmp_path, None)) == "es-ES"


def test_the_users_explicit_choice_still_wins_over_steam(
    tmp_path: Path,
) -> None:
    """Steam is a better guess than LANG, never better than an answer."""
    _write_registry(tmp_path, "german")
    assert get_unifideck_locale(_config(tmp_path, "es-ES")) == "es-ES"


def test_posix_applies_when_steam_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registry.vdf (non-Steam host, Flatpak elsewhere) — POSIX wins."""
    _set_posix_locale(monkeypatch, "it_IT")
    assert get_unifideck_locale(_config(tmp_path, None)) == "it-IT"


# ── degraded mode: what an installed plugin actually runs ───────────────
# ``get_locale_config`` returns None on an installed plugin, and that is the
# designed behaviour rather than a fault: ``scripts/`` is build tooling that
# the Decky CLI does not package, and ``locale_config.py`` is the one module
# in it that runtime code imports. ``_import_locale_config`` says as much —
# None "signals no schema validation available and the caller falls through
# to its default behaviour". Tier 1 honoured that; the tiers below returned
# None instead, which sent the whole chain to the hardcoded en-US at the
# bottom. These tests pin the degraded path, because it is the normal one in
# production.
def _degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make get_locale_config return None, as on an installed plugin."""
    import unifideck.utils.locale as locale_module
    monkeypatch.setattr(
        locale_module, "get_locale_config", lambda _config: None,
    )


def test_steam_language_still_applies_without_the_canonical_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_registry(tmp_path, "spanish")
    _degraded(monkeypatch)
    assert get_unifideck_locale(_config(tmp_path, None)) == "es-ES"


def test_posix_still_applies_without_the_canonical_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Steam setting, no locale list: the machine's language, not en-US."""
    _set_posix_locale(monkeypatch, "it_IT")
    _degraded(monkeypatch)
    assert get_unifideck_locale(_config(tmp_path, None)) == "it-IT"


def test_explicit_choice_wins_in_degraded_mode_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_registry(tmp_path, "german")
    _degraded(monkeypatch)
    assert get_unifideck_locale(_config(tmp_path, "es-ES")) == "es-ES"


# ── the log has to say WHERE the language came from ─────────────────────
# The chain stayed broken for a long time because it was unobservable: the
# logs showed which language was used, never which tier chose it, so
# "English again" looked the same whether the user's choice was ignored,
# Steam was unreadable, or the machine really was English.
@pytest.fixture(autouse=True)
def _forget_last_reported() -> None:
    """The INFO line is deduplicated; tests must not inherit each other's."""
    import unifideck.utils.locale as locale_module
    locale_module._last_reported = None


def test_reports_which_tier_chose_the_language(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    _write_registry(tmp_path, "spanish")
    with caplog.at_level("INFO", logger="unifideck.utils.locale"):
        assert get_unifideck_locale(_config(tmp_path, None)) == "es-ES"
    assert "resolved es-ES (source: Steam UI language)" in caplog.text


def test_reports_the_user_preference_tier_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    _write_registry(tmp_path, "german")
    with caplog.at_level("INFO", logger="unifideck.utils.locale"):
        assert get_unifideck_locale(_config(tmp_path, "es-ES")) == "es-ES"
    assert "resolved es-ES (source: user preference)" in caplog.text


def test_the_line_is_not_repeated_for_an_unchanged_outcome(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """It runs on every store request — one line per outcome, not per call."""
    _write_registry(tmp_path, "spanish")
    config = _config(tmp_path, None)
    with caplog.at_level("INFO", logger="unifideck.utils.locale"):
        for _ in range(5):
            get_unifideck_locale(config)
    assert caplog.text.count("resolved es-ES") == 1


# ── the packaged layout ────────────────────────────────────────────────
# A Decky CLI build flattens defaults/config.json to the install root:
# build.rs::zip_path strips the "defaults" prefix off every entry.
# bootstrap.boot has always handled both layouts; the launcher hardcoded
# the nested one, so on a CLI-built install — anyone who did not build
# locally — its ConfigManager found no defaults at all.
from unifideck.config.defaults_path import resolve_defaults_config_path


def test_finds_the_defaults_in_the_source_layout(tmp_path: Path) -> None:
    nested = tmp_path / "defaults" / "config.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}", encoding="utf-8")
    assert resolve_defaults_config_path(tmp_path) == str(nested)


def test_finds_the_defaults_in_the_decky_cli_layout(tmp_path: Path) -> None:
    flattened = tmp_path / "config.json"
    flattened.write_text("{}", encoding="utf-8")
    assert resolve_defaults_config_path(tmp_path) == str(flattened)


def test_prefers_the_source_layout_when_both_exist(tmp_path: Path) -> None:
    nested = tmp_path / "defaults" / "config.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert resolve_defaults_config_path(tmp_path) == str(nested)


def test_returns_the_nested_path_when_neither_exists(tmp_path: Path) -> None:
    """ConfigManager treats a missing defaults file as degraded mode, so a
    path it can log is more useful than None."""
    assert resolve_defaults_config_path(tmp_path) == str(
        tmp_path / "defaults" / "config.json",
    )
