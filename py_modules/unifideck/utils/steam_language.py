"""utils/steam_language.py — the UI language the user picked *in Steam*.

Sits between "explicit Unifideck preference" and "POSIX locale" in
:func:`utils.locale.get_unifideck_locale`, because on a Steam Deck the
POSIX locale is not the user's language.

SteamOS ships ``LANG=en_US.UTF-8`` and never changes it: the language a
Deck user actually picks lives in Steam's own settings and is not
exported to the environment. So ``ui.locale = "auto"`` — the shipped
default — resolved to English for every user whose Steam is in anything
else, and every store CLI we drive (``legendary --language``,
``gogdl --lang``) inherited that. Confirmed on a device whose Steam runs
in Spanish: ``LANG`` was ``en_US.UTF-8`` and Epic titles launched with
``-epiclocale=en``.

Steam records it in ``registry.vdf`` as a lowercase English word
(``spanish``, ``koreana``, ``schinese``), which is Steam's own API
language code, not a BCP-47 tag — hence the map below. It is the only
table here: which tags are *supported* still comes from
``i18n.locales``, so this module cannot drift from the shipped locale
list. A Steam language with no matching entry there resolves to None and
the caller falls through to POSIX exactly as before.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# registry.vdf lives under the ``~/.steam`` root (a symlink), NOT under the
# resolved steam_root, and moves under Flatpak. Same probe list as
# ``steam.current_user._REGISTRY_CANDIDATES`` — duplicated rather than
# imported because ``utils`` is a lower layer than ``steam`` and must not
# depend on it.
_REGISTRY_CANDIDATES = (
    "~/.steam/registry.vdf",
    "~/.steam/steam/registry.vdf",
    "~/.var/app/com.valvesoftware.Steam/.steam/registry.vdf",
)
# Steam writes the key as ``language``; match case-insensitively because
# the casing has varied across client versions.
_LANGUAGE_RE = re.compile(r'"language"\s+"([^"]*)"', re.IGNORECASE)

# Steam API language code → BCP-47. Only codes that can map onto a real
# locale are listed; anything else falls through to POSIX. ``latam`` is
# Steam's Latin-American Spanish, folded onto es-ES because that is the
# only Spanish the plugin ships — a deliberate approximation, and better
# than dropping a Spanish-speaking user into English.
_STEAM_LANGUAGE_TAGS: dict[str, str] = {
    "english": "en-US",
    "french": "fr-FR",
    "german": "de-DE",
    "spanish": "es-ES",
    "latam": "es-ES",
    "italian": "it-IT",
    "brazilian": "pt-BR",
    "portuguese": "pt-BR",
    "dutch": "nl-NL",
    "polish": "pl-PL",
    "russian": "ru-RU",
    "ukrainian": "uk-UA",
    "turkish": "tr-TR",
    "japanese": "ja-JP",
    "koreana": "ko-KR",
    "schinese": "zh-CN",
    "tchinese": "zh-TW",
    "arabic": "ar-SA",
}


def read_steam_language() -> str | None:
    """Steam's configured UI language as its own code, or None.

    Pure read: no validation against the supported locales, so a caller
    can log what Steam actually said even when it maps to nothing.
    """
    for candidate in _REGISTRY_CANDIDATES:
        path = Path(candidate).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = _LANGUAGE_RE.search(text)
        if match and match.group(1).strip():
            return match.group(1).strip().lower()
    return None


def detect_steam_locale(lc: Any) -> str | None:
    """Map Steam's UI language onto a supported ``i18n.locales`` tag.

    Returns None — so the caller falls through to the POSIX locale —
    when Steam's setting is unreadable, unknown to the map, or maps to a
    tag this build does not ship.

    ``lc`` may legitimately be None: ``scripts/locale_config.py`` is not
    packaged into the plugin — neither ``build-plugin.sh`` nor the Decky
    CLI stages ``scripts/`` — so on an installed plugin there is no
    canonical list to validate against, and that is the normal case, not
    an edge one. The mapping is then trusted as-is, exactly as tier 1
    trusts an unvalidated saved preference. Returning None here instead
    is what made this tier dead on every real install.
    """
    steam_language = read_steam_language()
    if not steam_language:
        return None
    tag = _STEAM_LANGUAGE_TAGS.get(steam_language)
    if not tag:
        logger.debug(
            "[locale] Steam language '%s' has no BCP-47 mapping",
            steam_language,
        )
        return None
    if lc is None:
        logger.debug(
            "[locale] Steam language '%s' → %s (unvalidated: no "
            "i18n.locales available)", steam_language, tag,
        )
        return tag
    if lc.get(tag) is None:
        logger.debug(
            "[locale] Steam language '%s' maps to %s, which this build "
            "does not ship", steam_language, tag,
        )
        return None
    logger.debug("[locale] Steam language '%s' → %s", steam_language, tag)
    return tag
