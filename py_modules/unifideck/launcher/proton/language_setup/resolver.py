from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
LOCALE_MAP: dict[str, tuple[str, str, str, str]] = {
    "en-US": ("00000409", "ENU", "en-US", "United States"),
    "de-DE": ("00000407", "DEU", "de-DE", "Germany"),
    "fr-FR": ("0000040c", "FRA", "fr-FR", "France"),
    "es-ES": ("00000c0a", "ESN", "es-ES", "Spain"),
    "it-IT": ("00000410", "ITA", "it-IT", "Italy"),
    "pt-BR": ("00000416", "PTB", "pt-BR", "Brazil"),
    "ru-RU": ("00000419", "RUS", "ru-RU", "Russia"),
    "pl-PL": ("00000415", "PLK", "pl-PL", "Poland"),
    "zh-CN": ("00000804", "CHS", "zh-CN", "China"),
    # Without its own row, ``smart_match_locale`` fell back to the "zh" prefix
    # and handed a Traditional Chinese user Simplified.
    "zh-TW": ("00000404", "CHT", "zh-TW", "Taiwan"),
    "ja-JP": ("00000411", "JPN", "ja-JP", "Japan"),
    "ko-KR": ("00000412", "KOR", "ko-KR", "Korea"),
    "nl-NL": ("00000413", "NLD", "nl-NL", "Netherlands"),
    "tr-TR": ("0000041f", "TRK", "tr-TR", "Turkey"),
    "uk-UA": ("00000422", "UKR", "uk-UA", "Ukraine"),
    "ar-SA": ("00000401", "ARA", "ar-SA", "Saudi Arabia"),
}
UBISOFT_LANG_MAP: dict[str, str] = {
    "en-US": "en", "de-DE": "de", "fr-FR": "fr", "es-ES": "es",
    "it-IT": "it", "pt-BR": "pt", "ru-RU": "ru", "pl-PL": "pl",
    "zh-CN": "zh", "ja-JP": "ja", "ko-KR": "ko", "nl-NL": "nl",
    "tr-TR": "tr",
}
GOG_DISPLAY_NAMES: dict[str, str] = {
    "en-US": "English",
    "fr-FR": "French",
    "de-DE": "German",
    "es-ES": "Spanish",
    "it-IT": "Italian",
    "pt-BR": "Portuguese (Brazil)",
    "ru-RU": "Russian",
    "pl-PL": "Polish",
    "zh-CN": "Chinese (Simplified)",
    "zh-Hans": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "zh-Hant": "Chinese (Traditional)",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
    "nl-NL": "Dutch",
    "tr-TR": "Turkish",
}
_DEFAULT_LANGUAGE = "en-US"
def get_unifideck_language(config: ConfigManager | None = None) -> str:
    """Get unifideck language."""
    if config is None:
        logger.debug(
            "[language_setup] no ConfigManager provided, using %s",
            _DEFAULT_LANGUAGE,
        )
        return _DEFAULT_LANGUAGE
    try:
        from unifideck.utils.locale import get_unifideck_locale
        return get_unifideck_locale(config)
    except Exception as err:
        logger.warning(
            "[language_setup] locale resolver failed: %s, "
            "falling back to %s", err, _DEFAULT_LANGUAGE,
        )
        return _DEFAULT_LANGUAGE
