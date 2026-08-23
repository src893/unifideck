from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .registry_io import _apply_windows_locale
from .resolver import get_unifideck_language

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
def apply_prefix_language(
    prefix_path: str, config: ConfigManager | None = None,
) -> bool:
    """Put the user's language into a prefix's Windows locale. Any store.

    This replaced three per-store wrappers that were the same six lines with
    different log prefixes. A prefix's ``Control Panel\\International`` is a
    property of the *prefix*, not of the store the game came from — every
    Windows game that asks ``GetUserDefaultLocaleName`` reads it, and so does
    number and date formatting — so it belongs on the one path every launch
    takes rather than in whichever handlers happened to grow it.

    Distinct from the *game-level* language mechanisms, which stay per-store
    because they configure the game rather than the prefix: Battle.net's
    ``Client.Language`` (``launcher/wrapper_locale``), Ubisoft's UPC
    ``Language`` key (``language_setup/ubisoft``), Epic's ``-epiclocale``, and
    the ``--lang`` gogdl bakes into ``goggame-*.info`` at install time.

    Returns False when the write was refused or did not survive; see
    :func:`~.registry_io._apply_windows_locale`. Never raises for a locale
    reason — the caller in ``proton.dispatch`` must not fail a launch over a
    language preference.
    """
    language = get_unifideck_language(config)
    logger.info(
        "[language_setup] applying %s to prefix=%s", language, prefix_path,
    )
    return _apply_windows_locale(prefix_path, language)
